"""
Modelos de dominio para Usage Metering SaaS (Hito O.6 — Usage Metering).

Define:
- UsageRequestStatus: Estados canónicos de peticiones medidas (SUCCESS, FAILED, CACHED, BLOCKED, UNKNOWN).
- UsagePeriodType: Granularidad temporal de consulta (HOUR, DAY, MONTH, CUSTOM).
- UsagePeriod: Rango temporal UTC determinista e inmutable.
- UsageEvent: Hecho atómico e inmutable de consumo de inferencia / IA acotado estrictamente a un Tenant.
- UsageQuery: Consulta de filtrado multidimensional acotada a un Tenant.
- DimensionUsageSummary: Agregado parcial de métricas por dimensión específica (modelo, usuario, etc.).
- UsageAggregate: Proyección agregada y determinista de eventos de uso por período y dimensiones.
- Excepciones de dominio para Usage Metering.

Principios O.6:
1. Responde a: "¿Cuánto uso de IA ha consumido cada tenant, usuario, modelo y período?".
2. Inmutabilidad estricta (frozen=True, MappingProxyType, tuplas).
3. Append-only facts: Un UsageEvent histórico nunca se altera.
4. Tenant Isolation estricto: Todo evento y toda agregación está acotada a un tenant explícito (sin cross-tenant leaks).
5. Fact vs. Aggregate: Separación nítida entre hechos atómicos (UsageEvent) y agregados calculados (UsageAggregate).
6. REUSE > EXTEND > CREATE: Reutiliza hechos estructurados de O.5, tipos de K.3, ClockPort K.7, N.1 Identity y N.9 Data Sanitization.
7. No False Metering:
   - UNKNOWN tokens != 0.
   - estimated_cost != actual_cost.
   - Cache hit => request_count=1, provider_call=0, sin inventar tokens de proveedor.
   - Failed request => registrada como fallo sin tokens falsos.
8. Límites de responsabilidad:
   - CERO lógica de cuotas o throttling (O.7).
   - CERO lógica de facturación / pricing tiers / invoices (O.8 / O.9).
   - CERO prompts, completions, CoT o secretos.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union
import uuid

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.caching.models import CacheLookupStatus


class UsageMeteringError(Exception):
    """Excepción base para errores del dominio de Usage Metering."""
    pass


class UsageEventConflictError(UsageMeteringError):
    """Se lanza cuando un evento con el mismo ID o source_reference tiene datos contradictorios."""
    pass


class UsageEventIntegrityError(UsageMeteringError):
    """Se lanza cuando se detecta corrupción de datos o alteración de checksum en un UsageEvent."""
    pass


class UsageQueryError(UsageMeteringError):
    """Se lanza cuando una consulta de uso es inválida o viola aislamiento de tenant."""
    pass


class UsageRequestStatus(str, Enum):
    """Estados canónicos de una invocación de IA para medición."""
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CACHED = "CACHED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class UsagePeriodType(str, Enum):
    """Granularidad de períodos de agregación."""
    HOUR = "HOUR"
    DAY = "DAY"
    MONTH = "MONTH"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True)
class UsagePeriod:
    """
    Rango temporal UTC determinista e inmutable para agregaciones de consumo.
    Boundary canónico: [start_time, end_time) semi-abierto.
    """
    start_time: datetime
    end_time: datetime
    period_type: UsagePeriodType = UsagePeriodType.CUSTOM

    def __post_init__(self):
        if self.start_time.tzinfo is None:
            object.__setattr__(self, "start_time", self.start_time.replace(tzinfo=timezone.utc))
        if self.end_time.tzinfo is None:
            object.__setattr__(self, "end_time", self.end_time.replace(tzinfo=timezone.utc))

        if self.start_time > self.end_time:
            raise ValueError(f"start_time ({self.start_time}) cannot be after end_time ({self.end_time})")

    def contains(self, dt: datetime) -> bool:
        """Determina si un timestamp UTC cae dentro del intervalo semi-abierto [start_time, end_time)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return self.start_time <= dt < self.end_time


@dataclass(frozen=True)
class UsageEvent:
    """
    Hecho atómico e inmutable de consumo de inferencia / IA acotado a un Tenant.

    Garantías:
    - Inmutable (frozen=True, safe mappings).
    - Cero contenido de prompts, completions, tokens de sesión o secretos.
    - Preserva UNKNOWN tokens y UNKNOWN actual cost de forma explícita sin forzar 0.
    - Verificación criptográfica de integridad SHA-256.
    """
    usage_event_id: str
    tenant_id: str
    occurred_at: datetime
    request_status: UsageRequestStatus

    # Identidad y organización opcionales (N.1 / O.2)
    identity_id: Optional[str] = None
    organization_id: Optional[str] = None
    session_id: Optional[str] = None

    # Dimensiones de proveedor y modelo (M.1 / O.5)
    provider: Optional[str] = None
    model: Optional[str] = None
    task_type: Optional[str] = None
    cache_status: CacheLookupStatus = CacheLookupStatus.MISS

    # Métricas de tokens (None = UNKNOWN / absent)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    # Métricas de costo en Decimal (None = UNKNOWN / absent)
    estimated_cost: Optional[Decimal] = None
    actual_cost: Optional[Decimal] = None

    # Trazabilidad e idempotencia
    correlation_id: Optional[str] = None
    source_reference: Optional[str] = None

    # Metadatos sanitizados
    details: Mapping[str, Any] = field(default_factory=dict)

    # Checksum de integridad
    checksum: str = field(init=False)

    def __post_init__(self):
        # 1. Validaciones de identificadores
        validate_safe_identifier(self.usage_event_id, field_name="usage_event_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.identity_id is not None:
            validate_safe_identifier(self.identity_id, field_name="identity_id")
        if self.organization_id is not None:
            validate_safe_identifier(self.organization_id, field_name="organization_id")

        # 2. Timezone UTC
        if self.occurred_at.tzinfo is None:
            object.__setattr__(self, "occurred_at", self.occurred_at.replace(tzinfo=timezone.utc))

        # 3. Normalizaciones de provider / model (lowercase stripped)
        if self.provider is not None:
            object.__setattr__(self, "provider", self.provider.strip().lower())
        if self.model is not None:
            object.__setattr__(self, "model", self.model.strip().lower())
        if self.task_type is not None:
            object.__setattr__(self, "task_type", self.task_type.strip().lower())

        # 4. Validar tokens
        if self.input_tokens is not None:
            if self.input_tokens < 0:
                raise ValueError("input_tokens must be non-negative")
        if self.output_tokens is not None:
            if self.output_tokens < 0:
                raise ValueError("output_tokens must be non-negative")
        if self.total_tokens is not None:
            if self.total_tokens < 0:
                raise ValueError("total_tokens must be non-negative")

        # Derivación determinista de total_tokens si no fue provisto explícitamente
        if self.total_tokens is None and self.input_tokens is not None and self.output_tokens is not None:
            object.__setattr__(self, "total_tokens", self.input_tokens + self.output_tokens)

        # 5. Validar costes
        if self.estimated_cost is not None:
            if not isinstance(self.estimated_cost, Decimal):
                object.__setattr__(self, "estimated_cost", Decimal(str(self.estimated_cost)))
            if self.estimated_cost < Decimal("0.00"):
                raise ValueError("estimated_cost must be non-negative")

        if self.actual_cost is not None:
            if not isinstance(self.actual_cost, Decimal):
                object.__setattr__(self, "actual_cost", Decimal(str(self.actual_cost)))
            if self.actual_cost < Decimal("0.00"):
                raise ValueError("actual_cost must be non-negative")

        # 6. Sanitizar metadatos
        sanitized = sanitize_security_data(dict(self.details))
        object.__setattr__(self, "details", deep_freeze(sanitized))

        # 7. Checksum canónico SHA-256
        payload = self._build_canonical_payload()
        canonical_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        object.__setattr__(self, "checksum", hashlib.sha256(canonical_str.encode("utf-8")).hexdigest())

    def _build_canonical_payload(self) -> Dict[str, Any]:
        return {
            "usage_event_id": self.usage_event_id,
            "tenant_id": self.tenant_id,
            "occurred_at": self.occurred_at.isoformat(),
            "request_status": self.request_status.value if isinstance(self.request_status, UsageRequestStatus) else str(self.request_status),
            "identity_id": self.identity_id,
            "organization_id": self.organization_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "task_type": self.task_type,
            "cache_status": self.cache_status.value if isinstance(self.cache_status, CacheLookupStatus) else str(self.cache_status),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost": str(self.estimated_cost) if self.estimated_cost is not None else None,
            "actual_cost": str(self.actual_cost) if self.actual_cost is not None else None,
            "correlation_id": self.correlation_id,
            "source_reference": self.source_reference,
        }

    def verify_integrity(self) -> bool:
        """Verifica que el checksum coincida con el payload canónico actual."""
        payload = self._build_canonical_payload()
        canonical_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        expected = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
        return self.checksum == expected


@dataclass(frozen=True)
class UsageQuery:
    """
    Filtro de consulta para agregación y listado de eventos de uso.
    Obligatorio: tenant_id.
    """
    tenant_id: str
    identity_id: Optional[str] = None
    organization_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    task_type: Optional[str] = None
    period: Optional[UsagePeriod] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.identity_id is not None:
            validate_safe_identifier(self.identity_id, field_name="identity_id")
        if self.organization_id is not None:
            validate_safe_identifier(self.organization_id, field_name="organization_id")

        if self.provider is not None:
            object.__setattr__(self, "provider", self.provider.strip().lower())
        if self.model is not None:
            object.__setattr__(self, "model", self.model.strip().lower())
        if self.task_type is not None:
            object.__setattr__(self, "task_type", self.task_type.strip().lower())

        # Si vienen start_time y end_time pero no period, construir UsagePeriod
        if self.period is None and (self.start_time is not None or self.end_time is not None):
            st = self.start_time or datetime.min.replace(tzinfo=timezone.utc)
            et = self.end_time or datetime.max.replace(tzinfo=timezone.utc)
            object.__setattr__(self, "period", UsagePeriod(start_time=st, end_time=et))

    def matches(self, event: UsageEvent) -> bool:
        """Determina si un UsageEvent cumple con los filtros especificados."""
        if event.tenant_id != self.tenant_id:
            return False
        if self.identity_id is not None and event.identity_id != self.identity_id:
            return False
        if self.organization_id is not None and event.organization_id != self.organization_id:
            return False
        if self.provider is not None and event.provider != self.provider:
            return False
        if self.model is not None and event.model != self.model:
            return False
        if self.task_type is not None and event.task_type != self.task_type:
            return False
        if self.period is not None and not self.period.contains(event.occurred_at):
            return False
        return True


@dataclass(frozen=True)
class DimensionUsageSummary:
    """Métricas agregadas para una dimensión específica (ej. un modelo o un usuario)."""
    dimension_key: str
    dimension_value: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    cached_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    unknown_token_events_count: int = 0
    total_estimated_cost: Decimal = Decimal("0.00")
    total_actual_cost: Decimal = Decimal("0.00")
    unknown_actual_cost_events_count: int = 0


@dataclass(frozen=True)
class UsageAggregate:
    """
    Resultado agregado, inmutable y determinista de métricas de uso para un tenant y filtros dados.
    """
    tenant_id: str
    period: Optional[UsagePeriod]

    # Contadores de solicitudes
    total_requests: int
    successful_requests: int
    failed_requests: int
    cached_requests: int

    # Conteo de tokens
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    unknown_token_events_count: int

    # Totales de costo
    total_estimated_cost: Decimal
    total_actual_cost: Decimal
    unknown_actual_cost_events_count: int

    # Desgloses multidimensionales
    breakdown_by_model: Mapping[str, DimensionUsageSummary] = field(default_factory=dict)
    breakdown_by_identity: Mapping[str, DimensionUsageSummary] = field(default_factory=dict)
    breakdown_by_provider: Mapping[str, DimensionUsageSummary] = field(default_factory=dict)
    breakdown_by_task_type: Mapping[str, DimensionUsageSummary] = field(default_factory=dict)

    # Total de eventos procesados
    events_count: int = 0
    checksum: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "breakdown_by_model", deep_freeze(dict(self.breakdown_by_model)))
        object.__setattr__(self, "breakdown_by_identity", deep_freeze(dict(self.breakdown_by_identity)))
        object.__setattr__(self, "breakdown_by_provider", deep_freeze(dict(self.breakdown_by_provider)))
        object.__setattr__(self, "breakdown_by_task_type", deep_freeze(dict(self.breakdown_by_task_type)))

        payload = {
            "tenant_id": self.tenant_id,
            "period_start": self.period.start_time.isoformat() if self.period else None,
            "period_end": self.period.end_time.isoformat() if self.period else None,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "cached_requests": self.cached_requests,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.output_tokens if hasattr(self, "output_tokens") else self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "unknown_token_events_count": self.unknown_token_events_count,
            "total_estimated_cost": str(self.total_estimated_cost),
            "total_actual_cost": str(self.total_actual_cost),
            "unknown_actual_cost_events_count": self.unknown_actual_cost_events_count,
            "events_count": self.events_count,
        }
        canonical_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        object.__setattr__(self, "checksum", hashlib.sha256(canonical_str.encode("utf-8")).hexdigest())
