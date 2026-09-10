"""
Adaptadores de Repositorio para Usage Metering SaaS (Hito O.6 — Usage Metering).

Define:
- InMemoryUsageEventRepository: Repositorio en memoria thread-safe particionado por tenant para tests y operaciones volátiles.
- JsonUsageEventRepository: Repositorio JSON thread-safe particionado en disco `base_dir / "tenants" / {tenant_id} / "usage" / events / ...`.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union

from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier
from src.domain.caching.models import CacheLookupStatus
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageQuery,
    UsageRequestStatus,
    UsageEventConflictError,
    UsageEventIntegrityError,
)
from src.domain.usage_metering.ports import UsageEventRepositoryPort


class InMemoryUsageEventRepository(UsageEventRepositoryPort):
    """
    Repositorio de eventos de uso en memoria, particionado estrictamente por tenant_id.
    Thread-safe con RLock por tenant.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # Estructura: Dict[tenant_id, Dict[usage_event_id, UsageEvent]]
        self._events_by_tenant: Dict[str, Dict[str, UsageEvent]] = {}
        # Mapeo secundario para idempotencia por source_reference: Dict[tenant_id, Dict[source_ref, usage_event_id]]
        self._source_ref_by_tenant: Dict[str, Dict[str, str]] = {}

    def append_event(self, context: TenantContext, event: UsageEvent) -> UsageEvent:
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, event.tenant_id, operation_name="append_event")

        # Validar integridad del evento
        if not event.verify_integrity():
            raise UsageEventIntegrityError(f"UsageEvent checksum verification failed for ID: {event.usage_event_id}")

        with self._lock:
            tenant_id = context.tenant_id
            if tenant_id not in self._events_by_tenant:
                self._events_by_tenant[tenant_id] = {}
                self._source_ref_by_tenant[tenant_id] = {}

            tenant_events = self._events_by_tenant[tenant_id]
            tenant_source_refs = self._source_ref_by_tenant[tenant_id]

            # 1. Verificar si ya existe por usage_event_id
            if event.usage_event_id in tenant_events:
                existing = tenant_events[event.usage_event_id]
                if existing.checksum == event.checksum:
                    return existing
                raise UsageEventConflictError(
                    f"Conflict: Event with ID {event.usage_event_id} already exists with differing payload."
                )

            # 2. Verificar si ya existe por source_reference (idempotencia cruzada)
            if event.source_reference and event.source_reference in tenant_source_refs:
                existing_id = tenant_source_refs[event.source_reference]
                existing = tenant_events[existing_id]
                if existing.checksum == event.checksum:
                    return existing
                raise UsageEventConflictError(
                    f"Conflict: Event with source_reference {event.source_reference} already exists with differing payload."
                )

            # 3. Guardar evento
            tenant_events[event.usage_event_id] = event
            if event.source_reference:
                tenant_source_refs[event.source_reference] = event.usage_event_id

            return event

    def get_event_by_id(self, context: TenantContext, usage_event_id: str) -> Optional[UsageEvent]:
        CrossTenantGuard.ensure_tenant_context(context)
        with self._lock:
            tenant_id = context.tenant_id
            tenant_events = self._events_by_tenant.get(tenant_id, {})
            event = tenant_events.get(usage_event_id)
            if event is None:
                return None
            CrossTenantGuard.assert_same_tenant(context, event.tenant_id, operation_name="get_event_by_id")
            if not event.verify_integrity():
                raise UsageEventIntegrityError(f"Corrupted UsageEvent detected: {usage_event_id}")
            return event

    def find_by_query(self, context: TenantContext, query: UsageQuery) -> List[UsageEvent]:
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, query.tenant_id, operation_name="find_by_query")

        with self._lock:
            tenant_id = context.tenant_id
            tenant_events = self._events_by_tenant.get(tenant_id, {})
            results: List[UsageEvent] = []
            for event in tenant_events.values():
                if not event.verify_integrity():
                    raise UsageEventIntegrityError(f"Corrupted UsageEvent detected: {event.usage_event_id}")
                if query.matches(event):
                    results.append(event)
            # Ordenar deterministamente por occurred_at, luego usage_event_id
            results.sort(key=lambda e: (e.occurred_at, e.usage_event_id))
            return results

    def count_by_query(self, context: TenantContext, query: UsageQuery) -> int:
        return len(self.find_by_query(context, query))


class JsonUsageEventRepository(UsageEventRepositoryPort):
    """
    Repositorio JSON persistente particionado estrictamente por Tenant.
    Ruta física: `base_storage_dir / "tenants" / {tenant_id} / "usage" / "events" / "{event_id}.json"`
    Thread-safe con RLock.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_tenant_usage_dir(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        usage_dir = self.tenants_dir / tenant_id / "usage" / "events"
        usage_dir.mkdir(parents=True, exist_ok=True)
        return usage_dir

    def _get_event_file_path(self, tenant_id: str, usage_event_id: str) -> Path:
        validate_safe_identifier(usage_event_id, field_name="usage_event_id")
        return self._get_tenant_usage_dir(tenant_id) / f"{usage_event_id}.json"

    def _serialize_event(self, event: UsageEvent) -> Dict[str, Any]:
        return {
            "usage_event_id": event.usage_event_id,
            "tenant_id": event.tenant_id,
            "occurred_at": event.occurred_at.isoformat(),
            "request_status": event.request_status.value if isinstance(event.request_status, UsageRequestStatus) else str(event.request_status),
            "identity_id": event.identity_id,
            "organization_id": event.organization_id,
            "session_id": event.session_id,
            "provider": event.provider,
            "model": event.model,
            "task_type": event.task_type,
            "cache_status": event.cache_status.value if isinstance(event.cache_status, CacheLookupStatus) else str(event.cache_status),
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "total_tokens": event.total_tokens,
            "estimated_cost": str(event.estimated_cost) if event.estimated_cost is not None else None,
            "actual_cost": str(event.actual_cost) if event.actual_cost is not None else None,
            "correlation_id": event.correlation_id,
            "source_reference": event.source_reference,
            "details": dict(event.details),
            "checksum": event.checksum,
        }

    def _deserialize_event(self, data: Dict[str, Any]) -> UsageEvent:
        event = UsageEvent(
            usage_event_id=data["usage_event_id"],
            tenant_id=data["tenant_id"],
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            request_status=UsageRequestStatus(data["request_status"]),
            identity_id=data.get("identity_id"),
            organization_id=data.get("organization_id"),
            session_id=data.get("session_id"),
            provider=data.get("provider"),
            model=data.get("model"),
            task_type=data.get("task_type"),
            cache_status=CacheLookupStatus(data["cache_status"]) if "cache_status" in data else CacheLookupStatus.MISS,
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            total_tokens=data.get("total_tokens"),
            estimated_cost=Decimal(data["estimated_cost"]) if data.get("estimated_cost") is not None else None,
            actual_cost=Decimal(data["actual_cost"]) if data.get("actual_cost") is not None else None,
            correlation_id=data.get("correlation_id"),
            source_reference=data.get("source_reference"),
            details=data.get("details", {}),
        )
        if "checksum" in data and event.checksum != data["checksum"]:
            raise UsageEventIntegrityError(f"Checksum mismatch for stored event: {data.get('usage_event_id')}")
        return event

    def append_event(self, context: TenantContext, event: UsageEvent) -> UsageEvent:
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, event.tenant_id, operation_name="append_event")

        if not event.verify_integrity():
            raise UsageEventIntegrityError(f"UsageEvent checksum verification failed for ID: {event.usage_event_id}")

        file_path = self._get_event_file_path(context.tenant_id, event.usage_event_id)

        with self._lock:
            # 1. Comprobar si ya existe por ID
            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    existing = self._deserialize_event(data)
                    if existing.checksum == event.checksum:
                        return existing
                    raise UsageEventConflictError(
                        f"Conflict: Event with ID {event.usage_event_id} already exists with differing payload."
                    )
                except (json.JSONDecodeError, KeyError) as e:
                    raise UsageEventIntegrityError(f"Corrupted event file on disk: {file_path}") from e

            # 2. Comprobar si ya existe por source_reference
            if event.source_reference:
                usage_dir = self._get_tenant_usage_dir(context.tenant_id)
                for existing_file in usage_dir.glob("*.json"):
                    try:
                        with open(existing_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if data.get("source_reference") == event.source_reference:
                            existing = self._deserialize_event(data)
                            if existing.checksum == event.checksum:
                                return existing
                            raise UsageEventConflictError(
                                f"Conflict: Event with source_reference {event.source_reference} already exists with differing payload."
                            )
                    except (json.JSONDecodeError, KeyError):
                        continue

            # 3. Guardar nuevo archivo atómicamente
            temp_file = file_path.with_suffix(".tmp")
            payload_dict = self._serialize_event(event)
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload_dict, f, indent=2, ensure_ascii=False)
            temp_file.replace(file_path)

            return event

    def get_event_by_id(self, context: TenantContext, usage_event_id: str) -> Optional[UsageEvent]:
        CrossTenantGuard.ensure_tenant_context(context)
        file_path = self._get_event_file_path(context.tenant_id, usage_event_id)

        with self._lock:
            if not file_path.exists():
                return None
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                event = self._deserialize_event(data)
                CrossTenantGuard.assert_same_tenant(context, event.tenant_id, operation_name="get_event_by_id")
                if not event.verify_integrity():
                    raise UsageEventIntegrityError(f"Corrupted UsageEvent detected: {usage_event_id}")
                return event
            except (json.JSONDecodeError, KeyError) as e:
                raise UsageEventIntegrityError(f"Corrupted event file: {file_path}") from e

    def find_by_query(self, context: TenantContext, query: UsageQuery) -> List[UsageEvent]:
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, query.tenant_id, operation_name="find_by_query")

        usage_dir = self._get_tenant_usage_dir(context.tenant_id)
        results: List[UsageEvent] = []

        with self._lock:
            for file_path in usage_dir.glob("*.json"):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    event = self._deserialize_event(data)
                    CrossTenantGuard.assert_same_tenant(context, event.tenant_id, operation_name="find_by_query")
                    if not event.verify_integrity():
                        raise UsageEventIntegrityError(f"Corrupted UsageEvent detected: {event.usage_event_id}")
                    if query.matches(event):
                        results.append(event)
                except (json.JSONDecodeError, KeyError) as e:
                    raise UsageEventIntegrityError(f"Corrupted event file: {file_path}") from e

        results.sort(key=lambda e: (e.occurred_at, e.usage_event_id))
        return results

    def count_by_query(self, context: TenantContext, query: UsageQuery) -> int:
        return len(self.find_by_query(context, query))
