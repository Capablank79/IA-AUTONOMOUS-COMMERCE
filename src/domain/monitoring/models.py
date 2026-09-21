"""
Modelos de dominio para Monitoreo de Producción y Métricas Operacionales (Hito P.7 — Production / Operations).

Define:
- MonitoringScope: Alcance (PLATFORM, ENVIRONMENT, TENANT).
- MetricType: Taxonomía de métricas operacionales de producción.
- MetricUnit: Unidades canónicas de métricas.
- MetricWindow: Definición y especificación de ventanas temporales (5m, 1h, 24h, custom).
- MetricSample: Muestra puntual e inmutable de telemetría / métrica técnica.
- MetricSeries: Serie temporal inmutable agregada en una ventana temporal.
- MonitoringMetric: Métrica agregada inmutable con valor, agregaciones cuantitativas y metadata.
- ProductionMonitoringSnapshot: Vista consolidada y segura del estado técnico y operacional de producción.

Principios P.7:
1. Responde a: "¿Puede un operador observar continuamente el comportamiento técnico de producción mediante métricas agregadas y series temporales confiables?".
2. Aislamiento estricto por Environment (DEV, STAGING, PROD) y Tenant (cuando aplique).
3. UNKNOWN != ZERO: Si no hay muestras para una métrica en la ventana evaluada, su valor debe ser None/UNKNOWN, nunca 0 o 0ms falseados.
4. Route Cardinality Control: Normalización estricta a templates parametrizados (e.g. /admin/tenants/{tenant_id}), nunca IDs/UUIDs crudos.
5. Inmutabilidad y determinismo: frozen=True, MappingProxyType, y percentiles exactos (avg, p50, p95).
6. Resiliencia & Failure Safety: El fallo de monitoreo nunca afecta el path de ejecución ni el servicio principal.
7. Cero PII, secretos, tokens ni payloads en telemetría o metadata de métricas.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name


def resolve_environment(env_input: Union[ApplicationEnvironment, str]) -> ApplicationEnvironment:
    if isinstance(env_input, ApplicationEnvironment):
        return env_input
    return normalize_environment_name(str(env_input))
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class MonitoringError(Exception):
    """Excepción base para Production Monitoring."""
    pass


class MonitoringIntegrityError(MonitoringError):
    """Se lanza cuando se detecta corrupción o violación de integridad en métricas o snapshots."""
    pass


class MonitoringConfigurationError(MonitoringError):
    """Se lanza ante configuraciones inválidas de monitoreo o etiquetas no permitidas."""
    pass


class MonitoringScope(str, Enum):
    """Alcance de la observación técnica de monitoreo."""
    PLATFORM = "PLATFORM"
    ENVIRONMENT = "ENVIRONMENT"
    TENANT = "TENANT"


class MetricType(str, Enum):
    """Taxonomía canónica de métricas técnicas de monitoreo operacional."""
    REQUEST_COUNT = "REQUEST_COUNT"
    SUCCESS_COUNT = "SUCCESS_COUNT"
    ERROR_COUNT = "ERROR_COUNT"
    ERROR_RATE = "ERROR_RATE"
    LATENCY_MS = "LATENCY_MS"
    READINESS_FAILURE_COUNT = "READINESS_FAILURE_COUNT"
    LIVENESS_FAILURE_COUNT = "LIVENESS_FAILURE_COUNT"
    DB_AVAILABILITY = "DB_AVAILABILITY"
    DB_QUERY_LATENCY = "DB_QUERY_LATENCY"
    MODEL_REQUEST_COUNT = "MODEL_REQUEST_COUNT"
    MODEL_ERROR_COUNT = "MODEL_ERROR_COUNT"
    TOKEN_USAGE = "TOKEN_USAGE"
    AI_COST = "AI_COST"
    QUOTA_DENIAL_COUNT = "QUOTA_DENIAL_COUNT"
    BACKUP_STATUS = "BACKUP_STATUS"
    DR_STATUS = "DR_STATUS"


class MetricUnit(str, Enum):
    """Unidades canónicas de medida para métricas."""
    COUNT = "COUNT"
    MILLISECONDS = "MILLISECONDS"
    PERCENT = "PERCENT"
    RATIO = "RATIO"
    TOKENS = "TOKENS"
    CURRENCY = "CURRENCY"
    STATUS = "STATUS"
    BOOLEAN = "BOOLEAN"


class MetricWindow(str, Enum):
    """Ventanas temporales estándar soportadas para agregación."""
    WINDOW_5M = "5m"
    WINDOW_1H = "1h"
    WINDOW_24H = "24h"

    @property
    def duration_seconds(self) -> int:
        if self == MetricWindow.WINDOW_5M:
            return 300
        elif self == MetricWindow.WINDOW_1H:
            return 3600
        elif self == MetricWindow.WINDOW_24H:
            return 86400
        return 300


# Etiquetas prohibidas por alto riesgo de explosión de cardinalidad o fuga de privacidad
DISALLOWED_HIGH_CARDINALITY_LABELS = frozenset({
    "request_id",
    "session_id",
    "invoice_id",
    "prompt_hash",
    "raw_user_id",
    "user_id",
    "token",
    "authorization",
    "password",
    "secret",
    "email",
    "ip_address",
})


def validate_metric_labels(labels: Mapping[str, str]) -> None:
    """Valida que los labels no contengan claves de alta cardinalidad ni datos sensibles."""
    for key in labels.keys():
        normalized_key = key.strip().lower()
        if normalized_key in DISALLOWED_HIGH_CARDINALITY_LABELS:
            raise MonitoringConfigurationError(
                f"Disallowed high-cardinality or sensitive label key: '{key}'"
            )


def sanitize_route_template(path: str) -> str:
    """
    Normaliza una ruta concreta a un template seguro con baja cardinalidad.
    Convierte UUIDs, hashes, y segmentos numéricos a parámetros seguros.
    """
    if not path:
        return "/"

    # Strip query parameters if present
    path = path.split("?")[0]

    # Normalizar UUIDs
    path = re.sub(
        r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "/{id}",
        path,
    )
    # Normalizar IDs numéricos
    path = re.sub(r"/\d+", "/{id}", path)

    # Normalizar hashes hex largos (ej. commit hashes, tokens)
    path = re.sub(r"/[0-9a-fA-F]{16,}", "/{hash}", path)

    return path


def compute_monitoring_checksum(payload: Dict[str, Any]) -> str:
    """Calcula determinísticamente el checksum SHA-256 para estructuras de monitoreo."""
    def _json_serial(obj: Any) -> Any:
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (set, tuple)):
            return list(obj)
        raise TypeError(f"Type {type(obj)} not serializable")

    clean_payload = {k: v for k, v in payload.items() if k != "checksum"}
    encoded = json.dumps(clean_payload, sort_keys=True, default=_json_serial).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def calculate_percentile(sorted_values: Sequence[float], percentile: float) -> Optional[float]:
    """Calcula un percentil determinista (0.0 a 1.0) sobre una lista ordenada de números."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]

    k = (len(sorted_values) - 1) * percentile
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    d0 = sorted_values[int(f)] * (c - k)
    d1 = sorted_values[int(c)] * (k - f)
    return d0 + d1


@dataclass(frozen=True)
class MetricSample:
    """Muestra individual e inmutable de telemetría técnica."""
    metric_type: MetricType
    value: Union[int, float, Decimal, str, bool]
    timestamp: datetime
    environment: ApplicationEnvironment
    scope: MonitoringScope = MonitoringScope.PLATFORM
    tenant_id: Optional[str] = None
    unit: MetricUnit = MetricUnit.COUNT
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metric_type, MetricType):
            object.__setattr__(self, "metric_type", MetricType(self.metric_type))
        if not isinstance(self.environment, ApplicationEnvironment):
            object.__setattr__(self, "environment", resolve_environment(self.environment))
        if not isinstance(self.scope, MonitoringScope):
            object.__setattr__(self, "scope", MonitoringScope(self.scope))
        if not isinstance(self.unit, MetricUnit):
            object.__setattr__(self, "unit", MetricUnit(self.unit))
        if self.timestamp.tzinfo is None:
            object.__setattr__(self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc))
        if self.tenant_id is not None:
            validate_safe_identifier(self.tenant_id, "tenant_id")

        validate_metric_labels(self.labels)
        if not isinstance(self.labels, MappingProxyType):
            object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_type": self.metric_type.value,
            "value": float(self.value) if isinstance(self.value, Decimal) else self.value,
            "timestamp": self.timestamp.isoformat(),
            "environment": self.environment.value,
            "scope": self.scope.value,
            "tenant_id": self.tenant_id,
            "unit": self.unit.value,
            "labels": dict(self.labels),
        }


@dataclass(frozen=True)
class MonitoringMetric:
    """
    Métrica agregada inmutable en una ventana temporal para un entorno específico.
    Preserva UNKNOWN != ZERO (value=None cuando sample_count == 0).
    """
    metric_type: MetricType
    value: Optional[Union[int, float, Decimal, str, bool]]
    unit: MetricUnit
    sample_count: int
    environment: ApplicationEnvironment
    evaluated_at: datetime
    window_seconds: int
    scope: MonitoringScope = MonitoringScope.PLATFORM
    tenant_id: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    avg_value: Optional[float] = None
    p50_value: Optional[float] = None
    p95_value: Optional[float] = None
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metric_type, MetricType):
            object.__setattr__(self, "metric_type", MetricType(self.metric_type))
        if not isinstance(self.unit, MetricUnit):
            object.__setattr__(self, "unit", MetricUnit(self.unit))
        if not isinstance(self.environment, ApplicationEnvironment):
            object.__setattr__(self, "environment", resolve_environment(self.environment))
        if not isinstance(self.scope, MonitoringScope):
            object.__setattr__(self, "scope", MonitoringScope(self.scope))
        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))
        if self.tenant_id is not None:
            validate_safe_identifier(self.tenant_id, "tenant_id")

        validate_metric_labels(self.labels)
        if not isinstance(self.labels, MappingProxyType):
            object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))

    @property
    def is_unknown(self) -> bool:
        return self.value is None or self.sample_count == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_type": self.metric_type.value,
            "value": float(self.value) if isinstance(self.value, Decimal) else self.value,
            "unit": self.unit.value,
            "sample_count": self.sample_count,
            "environment": self.environment.value,
            "evaluated_at": self.evaluated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "scope": self.scope.value,
            "tenant_id": self.tenant_id,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "avg_value": self.avg_value,
            "p50_value": self.p50_value,
            "p95_value": self.p95_value,
            "labels": dict(self.labels),
        }


@dataclass(frozen=True)
class MetricSeries:
    """Serie temporal inmutable con muestras agrupadas o ordenadas en un intervalo."""
    metric_type: MetricType
    environment: ApplicationEnvironment
    scope: MonitoringScope
    samples: Tuple[MetricSample, ...]
    start_time: datetime
    end_time: datetime
    tenant_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.metric_type, MetricType):
            object.__setattr__(self, "metric_type", MetricType(self.metric_type))
        if not isinstance(self.environment, ApplicationEnvironment):
            object.__setattr__(self, "environment", resolve_environment(self.environment))
        if not isinstance(self.scope, MonitoringScope):
            object.__setattr__(self, "scope", MonitoringScope(self.scope))
        if not isinstance(self.samples, tuple):
            object.__setattr__(self, "samples", tuple(self.samples))
        if self.start_time.tzinfo is None:
            object.__setattr__(self, "start_time", self.start_time.replace(tzinfo=timezone.utc))
        if self.end_time.tzinfo is None:
            object.__setattr__(self, "end_time", self.end_time.replace(tzinfo=timezone.utc))
        if self.tenant_id is not None:
            validate_safe_identifier(self.tenant_id, "tenant_id")

    @property
    def count(self) -> int:
        return len(self.samples)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_type": self.metric_type.value,
            "environment": self.environment.value,
            "scope": self.scope.value,
            "tenant_id": self.tenant_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "count": self.count,
            "samples": [s.to_dict() for s in self.samples],
        }


@dataclass(frozen=True)
class ProductionMonitoringSnapshot:
    """
    Snapshot consolidado, inmutable y determinista del monitoreo operacional de un entorno.
    """
    environment: ApplicationEnvironment
    window_seconds: int
    evaluated_at: datetime
    metrics: Mapping[str, MonitoringMetric]
    collector_status: str = "healthy"
    last_sample_at: Optional[datetime] = None
    checksum: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.environment, ApplicationEnvironment):
            object.__setattr__(self, "environment", resolve_environment(self.environment))
        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))
        if self.last_sample_at and self.last_sample_at.tzinfo is None:
            object.__setattr__(self, "last_sample_at", self.last_sample_at.replace(tzinfo=timezone.utc))
        if not isinstance(self.metrics, MappingProxyType):
            object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

        if not self.checksum:
            payload = {
                "environment": self.environment.value,
                "window_seconds": self.window_seconds,
                "evaluated_at": self.evaluated_at,
                "collector_status": self.collector_status,
                "last_sample_at": self.last_sample_at,
                "metrics": {k: m.to_dict() for k, m in sorted(self.metrics.items())},
            }
            object.__setattr__(self, "checksum", compute_monitoring_checksum(payload))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "environment": self.environment.value,
            "window_seconds": self.window_seconds,
            "evaluated_at": self.evaluated_at.isoformat(),
            "collector_status": self.collector_status,
            "last_sample_at": self.last_sample_at.isoformat() if self.last_sample_at else None,
            "metrics": {k: m.to_dict() for k, m in self.metrics.items()},
            "checksum": self.checksum,
        }
