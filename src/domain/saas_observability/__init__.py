"""
Módulo de dominio para SaaS Observability (Hito O.12 — SaaS / Platformization).
"""

from .models import (
    ObservabilityError,
    ObservabilityIntegrityError,
    ObservabilityAccessError,
    ObservabilityScope,
    MetricType,
    MetricUnit,
    TenantHealthStatus,
    AlertSeverity,
    AlertStatus,
    OperationalAlertType,
    ObservabilityMetric,
    OperationalAlert,
    TenantOperationalSnapshot,
    compute_observability_checksum,
)
from .ports import (
    OperationalAlertRepositoryPort,
    TenantObservabilityServicePort,
)

__all__ = [
    "ObservabilityError",
    "ObservabilityIntegrityError",
    "ObservabilityAccessError",
    "ObservabilityScope",
    "MetricType",
    "MetricUnit",
    "TenantHealthStatus",
    "AlertSeverity",
    "AlertStatus",
    "OperationalAlertType",
    "ObservabilityMetric",
    "OperationalAlert",
    "TenantOperationalSnapshot",
    "compute_observability_checksum",
    "OperationalAlertRepositoryPort",
    "TenantObservabilityServicePort",
]
