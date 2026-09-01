"""
Implementación JSON atómica y persistente del Event Store (Hito J.5).

Garantiza:
- Atomic write (.tmp -> os.replace)
- Sanitización recursiva de datos sensibles
- Idempotencia estricta por event_id y por idempotency_key
- Idempotencia y trazabilidad de entrega por (event_id, handler_id)
- Manejo de corrupción y resiliencia ante reinicios
- Cero dependencias de infraestructura externa/SaaS/brokers
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List, Tuple
from types import MappingProxyType

from src.domain.events.models import (
    EventRecord,
    EventType,
    DeliveryRecord,
    DeliveryStatus,
)
from src.domain.events.ports import EventStorePort


class JsonEventStoreError(Exception):
    """Excepción base para errores en el almacén de eventos JSON."""
    pass


class CorruptedEventStoreDataError(JsonEventStoreError):
    """Se lanza cuando los datos de un evento o entrega están corruptos."""
    pass


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
}


def _encode_json_value(val: Any) -> Any:
    """Serializa estructuras de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_event_record(data: Dict[str, Any]) -> EventRecord:
    """Reconstruye una instancia de EventRecord a partir de un dict JSON."""
    try:
        event_id = data["event_id"]
        event_type = EventType(data["event_type"])
        subject_type = data["subject_type"]
        subject_id = data["subject_id"]

        occurred_at = datetime.fromisoformat(data["occurred_at"])
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)

        recorded_at = datetime.fromisoformat(data["recorded_at"])
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)

        correlation_id = data["correlation_id"]
        causation_id = data.get("causation_id")
        provenance = data.get("provenance", "SYSTEM")
        idempotency_key = data.get("idempotency_key", "")
        schema_version = data.get("schema_version", "1.0.0")
        payload_reference = data.get("payload_reference")
        payload = data.get("payload", {})
        metadata = data.get("metadata", {})

        return EventRecord(
            event_id=event_id,
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            provenance=provenance,
            idempotency_key=idempotency_key,
            schema_version=schema_version,
            payload_reference=payload_reference,
            payload=payload,
            metadata=metadata,
        )
    except Exception as exc:
        raise CorruptedEventStoreDataError(f"Error decodificando EventRecord: {exc}") from exc


def _decode_delivery_record(data: Dict[str, Any]) -> DeliveryRecord:
    """Reconstruye una instancia de DeliveryRecord a partir de un dict JSON."""
    try:
        delivery_id = data["delivery_id"]
        event_id = data["event_id"]
        handler_id = data["handler_id"]
        status = DeliveryStatus(data["status"])
        attempt_count = int(data.get("attempt_count", 1))

        first_attempted_at = datetime.fromisoformat(data["first_attempted_at"])
        if first_attempted_at.tzinfo is None:
            first_attempted_at = first_attempted_at.replace(tzinfo=timezone.utc)

        last_attempted_at = datetime.fromisoformat(data["last_attempted_at"])
        if last_attempted_at.tzinfo is None:
            last_attempted_at = last_attempted_at.replace(tzinfo=timezone.utc)

        error_message = data.get("error_message")
        execution_duration_ms = data.get("execution_duration_ms")
        metadata = data.get("metadata", {})

        return DeliveryRecord(
            delivery_id=delivery_id,
            event_id=event_id,
            handler_id=handler_id,
            status=status,
            attempt_count=attempt_count,
            first_attempted_at=first_attempted_at,
            last_attempted_at=last_attempted_at,
            error_message=error_message,
            execution_duration_ms=execution_duration_ms,
            metadata=metadata,
        )
    except Exception as exc:
        raise CorruptedEventStoreDataError(f"Error decodificando DeliveryRecord: {exc}") from exc


class JsonEventStore(EventStorePort):
    """
    Almacén de eventos persistente en JSON con soporte atómico y recuperación ante fallos.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.events_dir = self.base_dir / "events"
        self.deliveries_dir = self.base_dir / "deliveries"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.deliveries_dir.mkdir(parents=True, exist_ok=True)

    def _event_file_path(self, event_id: str) -> Path:
        # Sanitizar caracteres en event_id para nombre de archivo seguro
        safe_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in event_id)
        return self.events_dir / f"event_{safe_id}.json"

    def _delivery_file_path(self, event_id: str, handler_id: str) -> Path:
        safe_event_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in event_id)
        safe_handler_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in handler_id)
        return self.deliveries_dir / f"deliv_{safe_event_id}_{safe_handler_id}.json"

    def _atomic_write_json(self, target_path: Path, data_dict: Dict[str, Any]) -> None:
        tmp_path = target_path.with_suffix(".tmp")
        encoded_data = _encode_json_value(data_dict)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(encoded_data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)

    def append(self, event: EventRecord) -> EventRecord:
        """
        Persiste un evento de forma inmutable y atómica.
        Deduplica por event_id o por idempotency_key.
        """
        # Verificar si ya existe por event_id
        existing = self.get_by_id(event.event_id)
        if existing:
            return existing

        # Verificar si ya existe por idempotency_key
        existing_key = self.get_by_idempotency_key(event.idempotency_key)
        if existing_key:
            return existing_key

        file_path = self._event_file_path(event.event_id)
        raw_dict = {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "subject_type": event.subject_type,
            "subject_id": event.subject_id,
            "occurred_at": event.occurred_at.isoformat(),
            "recorded_at": event.recorded_at.isoformat(),
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "provenance": event.provenance,
            "idempotency_key": event.idempotency_key,
            "schema_version": event.schema_version,
            "payload_reference": event.payload_reference,
            "payload": dict(event.payload),
            "metadata": dict(event.metadata),
        }
        self._atomic_write_json(file_path, raw_dict)
        return self.get_by_id(event.event_id) or event

    def get_by_id(self, event_id: str) -> Optional[EventRecord]:
        file_path = self._event_file_path(event_id)
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _decode_event_record(data)
        except CorruptedEventStoreDataError:
            raise
        except Exception as exc:
            raise CorruptedEventStoreDataError(f"Error leyendo archivo de evento {file_path}: {exc}") from exc

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[EventRecord]:
        if not idempotency_key:
            return None
        for file_path in self.events_dir.glob("event_*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("idempotency_key") == idempotency_key:
                    return _decode_event_record(data)
            except Exception:
                continue
        return None

    def list_events(
        self,
        event_type: Optional[EventType] = None,
        subject_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[EventRecord]:
        events: List[EventRecord] = []
        for file_path in self.events_dir.glob("event_*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ev = _decode_event_record(data)
                if event_type is not None and ev.event_type != event_type:
                    continue
                if subject_id is not None and ev.subject_id != subject_id:
                    continue
                if correlation_id is not None and ev.correlation_id != correlation_id:
                    continue
                events.append(ev)
            except Exception:
                continue

        # Ordenar cronológicamente por occurred_at
        events.sort(key=lambda x: x.occurred_at)
        return events[:limit]

    def record_delivery(self, delivery: DeliveryRecord) -> DeliveryRecord:
        file_path = self._delivery_file_path(delivery.event_id, delivery.handler_id)
        raw_dict = {
            "delivery_id": delivery.delivery_id,
            "event_id": delivery.event_id,
            "handler_id": delivery.handler_id,
            "status": delivery.status.value,
            "attempt_count": delivery.attempt_count,
            "first_attempted_at": delivery.first_attempted_at.isoformat(),
            "last_attempted_at": delivery.last_attempted_at.isoformat(),
            "error_message": delivery.error_message,
            "execution_duration_ms": delivery.execution_duration_ms,
            "metadata": dict(delivery.metadata),
        }
        self._atomic_write_json(file_path, raw_dict)
        return self.get_delivery(delivery.event_id, delivery.handler_id) or delivery

    def get_delivery(self, event_id: str, handler_id: str) -> Optional[DeliveryRecord]:
        file_path = self._delivery_file_path(event_id, handler_id)
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _decode_delivery_record(data)
        except CorruptedEventStoreDataError:
            raise
        except Exception as exc:
            raise CorruptedEventStoreDataError(f"Error leyendo archivo de entrega {file_path}: {exc}") from exc

    def list_deliveries_by_event(self, event_id: str) -> List[DeliveryRecord]:
        deliveries: List[DeliveryRecord] = []
        safe_event_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in event_id)
        pattern = f"deliv_{safe_event_id}_*.json"
        for file_path in self.deliveries_dir.glob(pattern):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                deliveries.append(_decode_delivery_record(data))
            except Exception:
                continue
        return deliveries
