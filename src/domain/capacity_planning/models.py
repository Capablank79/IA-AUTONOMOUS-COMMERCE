"""
Modelos de dominio para Capacity Planning (Hito P.10 — Production / Operations).

Define:
- ResourceDimension: Dimensiones de recursos reales medibles.
- CapacityRisk: Niveles deterministas de riesgo de saturación (LOW, MODERATE, HIGH, CRITICAL, UNKNOWN).
- CapacityConfidence: Grado de certidumbre basado en cobertura de datos (HIGH, MEDIUM, LOW, INSUFFICIENT_DATA).
- CapacityRecommendationType: Recomendaciones estructuradas no ejecutivas (NO_ACTION, REVIEW_CAPACITY, SCALE_SOON, SCALE_IMMEDIATELY, INVESTIGATE_UNKNOWN_CAPACITY).
- ForecastHorizon: Horizontes temporales estándar (1h, 24h, 7d).
- CapacityScope: Alcance (PLATFORM, ENVIRONMENT, TENANT).
- CapacityTrend: Dirección y gradiente de demanda determinista.
- CapacityForecast: Proyección de demanda en horizonte específico con evidencia.
- CapacityResourceLimits: Definición inmutable de capacidad máxima conocida y umbrales de seguridad.
- CapacityEvaluation: Evaluación granular de una dimensión con utilización, headroom, picos y riesgo.
- CapacityRecommendation: Recomendación con trazabilidad y evidencia explicable.
- CapacitySnapshot: Vista consolidada de planificación de capacidad con checksum SHA-256.

Principios P.10:
1. Responde a: "¿Tiene la plataforma capacidad suficiente para soportar la demanda actual y futura, y cuándo debería ampliarse antes de degradarse?".
2. MONITORING != CAPACITY: P.7 mide lo ocurrido; P.10 proyecta tendencias, márgenes y riesgos.
3. QUOTA != CAPACITY: O.7 quota es política comercial/SaaS; P.10 es capacidad técnica real.
4. UNKNOWN != SAFE: La ausencia de métricas o telemetría resulta en UNKNOWN e INSUFFICIENT_DATA.
5. RECOMMENDATION != ACTION: Las recomendaciones informan; no ejecutan autoscaling ni aprovisionamiento de nube.
6. Aislamiento absoluto por ApplicationEnvironment (DEV nunca afecta a PROD).
7. Agregación segura Platform vs Tenant (sin cross-tenant data leak).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.monitoring.models import (
    resolve_environment,
)
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class CapacityPlanningError(Exception):
    """Excepción base para Capacity Planning."""
    pass


class CapacityPlanningIntegrityError(CapacityPlanningError):
    """Se lanza ante corrupción o inconsistencia de checksum en Capacity Planning."""
    pass


class CapacityPlanningConfigurationError(CapacityPlanningError):
    """Se lanza ante configuraciones o límites inválidos de capacidad."""
    pass


class ResourceDimension(str, Enum):
    """Dimensiones canónicas de recursos técnicos medibles."""
    REQUEST_THROUGHPUT = "REQUEST_THROUGHPUT"
    REQUEST_CONCURRENCY = "REQUEST_CONCURRENCY"
    DATABASE_CONNECTIONS = "DATABASE_CONNECTIONS"
    DATABASE_QUERY_LATENCY = "DATABASE_QUERY_LATENCY"
    AI_PROVIDER_THROUGHPUT = "AI_PROVIDER_THROUGHPUT"
    TOKEN_THROUGHPUT = "TOKEN_THROUGHPUT"
    STORAGE_GROWTH = "STORAGE_GROWTH"
    BACKUP_SIZE = "BACKUP_SIZE"
    ERROR_PRESSURE = "ERROR_PRESSURE"


class CapacityRisk(str, Enum):
    """Niveles de riesgo de saturación / degradación técnica."""
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"

    @property
    def severity_rank(self) -> int:
        order = {
            CapacityRisk.LOW: 1,
            CapacityRisk.MODERATE: 2,
            CapacityRisk.HIGH: 3,
            CapacityRisk.CRITICAL: 4,
            CapacityRisk.UNKNOWN: 5,
        }
        return order[self]


class CapacityConfidence(str, Enum):
    """Grado de confianza en el análisis y forecast basado en la calidad de datos."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class CapacityRecommendationType(str, Enum):
    """Tipos canónicos de recomendación estructurada de capacidad."""
    NO_ACTION = "NO_ACTION"
    REVIEW_CAPACITY = "REVIEW_CAPACITY"
    SCALE_SOON = "SCALE_SOON"
    SCALE_IMMEDIATELY = "SCALE_IMMEDIATELY"
    INVESTIGATE_UNKNOWN_CAPACITY = "INVESTIGATE_UNKNOWN_CAPACITY"


class ForecastHorizon(str, Enum):
    """Horizontes estándar de proyección determinista."""
    HORIZON_1H = "1h"
    HORIZON_24H = "24h"
    HORIZON_7D = "7d"

    @property
    def seconds(self) -> int:
        if self == ForecastHorizon.HORIZON_1H:
            return 3600
        elif self == ForecastHorizon.HORIZON_24H:
            return 86400
        elif self == ForecastHorizon.HORIZON_7D:
            return 604800
        return 3600


class CapacityScope(str, Enum):
    """Alcance de la evaluación de capacidad."""
    PLATFORM = "PLATFORM"
    ENVIRONMENT = "ENVIRONMENT"
    TENANT = "TENANT"


@dataclass(frozen=True)
class CapacityResourceLimits:
    """
    Especificación inmutable de la capacidad máxima conocida y umbrales de seguridad para un recurso.
    Si max_capacity es None, la capacidad máxima es UNKNOWN.
    """
    dimension: ResourceDimension
    max_capacity: Optional[float]
    unit: str
    target_headroom_ratio: float = 0.20  # 20% margen de seguridad por defecto
    warning_utilization_ratio: float = 0.70  # 70% umbral de advertencia
    critical_utilization_ratio: float = 0.85  # 85% umbral crítico
    custom_labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, ResourceDimension):
            object.__setattr__(self, "dimension", ResourceDimension(self.dimension))
        if self.max_capacity is not None and self.max_capacity <= 0:
            raise CapacityPlanningConfigurationError(f"max_capacity must be positive if specified, got {self.max_capacity}")
        if not (0.0 <= self.target_headroom_ratio <= 1.0):
            raise CapacityPlanningConfigurationError("target_headroom_ratio must be between 0.0 and 1.0")
        if not (0.0 <= self.warning_utilization_ratio <= 1.0):
            raise CapacityPlanningConfigurationError("warning_utilization_ratio must be between 0.0 and 1.0")
        if not (0.0 <= self.critical_utilization_ratio <= 1.0):
            raise CapacityPlanningConfigurationError("critical_utilization_ratio must be between 0.0 and 1.0")
        if self.warning_utilization_ratio > self.critical_utilization_ratio:
            raise CapacityPlanningConfigurationError("warning_utilization_ratio cannot exceed critical_utilization_ratio")
        if not isinstance(self.custom_labels, MappingProxyType):
            object.__setattr__(self, "custom_labels", MappingProxyType(dict(self.custom_labels)))

    @property
    def is_capacity_known(self) -> bool:
        return self.max_capacity is not None


@dataclass(frozen=True)
class CapacityTrend:
    """Tendencia determinista de la demanda observada."""
    direction: str  # "GROWING", "STABLE", "DECLINING", "UNKNOWN"
    growth_rate_per_hour: Optional[float]
    slope: Optional[float]  # pendiente lineal por segundo
    baseline_demand: Optional[float]
    sample_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction": self.direction,
            "growth_rate_per_hour": self.growth_rate_per_hour,
            "slope": self.slope,
            "baseline_demand": self.baseline_demand,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True)
class CapacityForecast:
    """Proyección determinista en un horizonte temporal."""
    horizon: ForecastHorizon
    projected_demand: Optional[float]
    projected_peak_demand: Optional[float]
    projected_utilization_ratio: Optional[float]
    projected_headroom: Optional[float]
    projected_headroom_ratio: Optional[float]
    risk: CapacityRisk
    confidence: CapacityConfidence

    def __post_init__(self) -> None:
        if not isinstance(self.horizon, ForecastHorizon):
            object.__setattr__(self, "horizon", ForecastHorizon(self.horizon))
        if not isinstance(self.risk, CapacityRisk):
            object.__setattr__(self, "risk", CapacityRisk(self.risk))
        if not isinstance(self.confidence, CapacityConfidence):
            object.__setattr__(self, "confidence", CapacityConfidence(self.confidence))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "horizon": self.horizon.value,
            "projected_demand": self.projected_demand,
            "projected_peak_demand": self.projected_peak_demand,
            "projected_utilization_ratio": self.projected_utilization_ratio,
            "projected_headroom": self.projected_headroom,
            "projected_headroom_ratio": self.projected_headroom_ratio,
            "risk": self.risk.value,
            "confidence": self.confidence.value,
        }


@dataclass(frozen=True)
class CapacityRecommendation:
    """Recomendación inmutable de planificación con evidencia verificable."""
    dimension: ResourceDimension
    recommendation_type: CapacityRecommendationType
    current_demand: Optional[float]
    known_capacity: Optional[float]
    current_utilization_ratio: Optional[float]
    current_headroom: Optional[float]
    risk: CapacityRisk
    confidence: CapacityConfidence
    reason: str
    suggested_capacity: Optional[float] = None
    forecast_horizon_analyzed: Optional[ForecastHorizon] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, ResourceDimension):
            object.__setattr__(self, "dimension", ResourceDimension(self.dimension))
        if not isinstance(self.recommendation_type, CapacityRecommendationType):
            object.__setattr__(self, "recommendation_type", CapacityRecommendationType(self.recommendation_type))
        if not isinstance(self.risk, CapacityRisk):
            object.__setattr__(self, "risk", CapacityRisk(self.risk))
        if not isinstance(self.confidence, CapacityConfidence):
            object.__setattr__(self, "confidence", CapacityConfidence(self.confidence))
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "recommendation_type": self.recommendation_type.value,
            "current_demand": self.current_demand,
            "known_capacity": self.known_capacity,
            "current_utilization_ratio": self.current_utilization_ratio,
            "current_headroom": self.current_headroom,
            "risk": self.risk.value,
            "confidence": self.confidence.value,
            "reason": self.reason,
            "suggested_capacity": self.suggested_capacity,
            "forecast_horizon_analyzed": self.forecast_horizon_analyzed.value if self.forecast_horizon_analyzed else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class CapacityEvaluation:
    """
    Evaluación completa y trazable de capacidad para una dimensión de recurso específica.
    """
    dimension: ResourceDimension
    current_demand: Optional[float]
    peak_demand: Optional[float]
    p95_demand: Optional[float]
    known_capacity: Optional[float]
    current_utilization_ratio: Optional[float]
    current_headroom: Optional[float]
    current_headroom_ratio: Optional[float]
    trend: CapacityTrend
    forecasts: Mapping[str, CapacityForecast]
    overall_risk: CapacityRisk
    confidence: CapacityConfidence
    recommendation: CapacityRecommendation
    sample_count: int
    evaluated_at: datetime
    data_quality_issues: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, ResourceDimension):
            object.__setattr__(self, "dimension", ResourceDimension(self.dimension))
        if not isinstance(self.overall_risk, CapacityRisk):
            object.__setattr__(self, "overall_risk", CapacityRisk(self.overall_risk))
        if not isinstance(self.confidence, CapacityConfidence):
            object.__setattr__(self, "confidence", CapacityConfidence(self.confidence))
        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))
        if not isinstance(self.data_quality_issues, tuple):
            object.__setattr__(self, "data_quality_issues", tuple(self.data_quality_issues))
        if not isinstance(self.forecasts, MappingProxyType):
            object.__setattr__(self, "forecasts", MappingProxyType(dict(self.forecasts)))

    @property
    def is_capacity_unknown(self) -> bool:
        return self.known_capacity is None

    @property
    def is_demand_unknown(self) -> bool:
        return self.current_demand is None or self.sample_count == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "current_demand": self.current_demand,
            "peak_demand": self.peak_demand,
            "p95_demand": self.p95_demand,
            "known_capacity": self.known_capacity,
            "current_utilization_ratio": self.current_utilization_ratio,
            "current_headroom": self.current_headroom,
            "current_headroom_ratio": self.current_headroom_ratio,
            "trend": self.trend.to_dict(),
            "forecasts": {k: v.to_dict() for k, v in self.forecasts.items()},
            "overall_risk": self.overall_risk.value,
            "confidence": self.confidence.value,
            "recommendation": self.recommendation.to_dict(),
            "sample_count": self.sample_count,
            "evaluated_at": self.evaluated_at.isoformat(),
            "data_quality_issues": list(self.data_quality_issues),
        }


def compute_capacity_checksum(
    environment: ApplicationEnvironment,
    scope: CapacityScope,
    evaluated_at: datetime,
    evaluations: Mapping[ResourceDimension, CapacityEvaluation],
    tenant_id: Optional[str] = None,
) -> str:
    """Calcula checksum criptográfico determinista SHA-256 para el snapshot de capacidad."""
    payload = {
        "environment": environment.value,
        "scope": scope.value,
        "tenant_id": tenant_id,
        "evaluated_at": evaluated_at.isoformat(),
        "evaluations": {
            dim.value: eval_item.to_dict()
            for dim, eval_item in sorted(evaluations.items(), key=lambda x: x[0].value)
        },
    }
    dumped = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapacitySnapshot:
    """
    Snapshot inmutable de planificación de capacidad para un entorno y alcance específico.
    """
    environment: ApplicationEnvironment
    scope: CapacityScope
    evaluated_at: datetime
    evaluations: Mapping[ResourceDimension, CapacityEvaluation]
    recommendations: Tuple[CapacityRecommendation, ...]
    overall_risk: CapacityRisk
    overall_confidence: CapacityConfidence
    tenant_id: Optional[str] = None
    checksum: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.environment, ApplicationEnvironment):
            object.__setattr__(self, "environment", resolve_environment(self.environment))
        if not isinstance(self.scope, CapacityScope):
            object.__setattr__(self, "scope", CapacityScope(self.scope))
        if not isinstance(self.overall_risk, CapacityRisk):
            object.__setattr__(self, "overall_risk", CapacityRisk(self.overall_risk))
        if not isinstance(self.overall_confidence, CapacityConfidence):
            object.__setattr__(self, "overall_confidence", CapacityConfidence(self.overall_confidence))
        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))
        if self.tenant_id is not None:
            validate_safe_identifier(self.tenant_id, "tenant_id")
        if not isinstance(self.recommendations, tuple):
            object.__setattr__(self, "recommendations", tuple(self.recommendations))
        if not isinstance(self.evaluations, MappingProxyType):
            object.__setattr__(self, "evaluations", MappingProxyType(dict(self.evaluations)))

        expected_checksum = compute_capacity_checksum(
            environment=self.environment,
            scope=self.scope,
            evaluated_at=self.evaluated_at,
            evaluations=self.evaluations,
            tenant_id=self.tenant_id,
        )
        if self.checksum and self.checksum != expected_checksum:
            raise CapacityPlanningIntegrityError(
                f"Capacity snapshot checksum mismatch: expected {expected_checksum}, got {self.checksum}"
            )
        if not self.checksum:
            object.__setattr__(self, "checksum", expected_checksum)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "environment": self.environment.value,
            "scope": self.scope.value,
            "tenant_id": self.tenant_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "overall_risk": self.overall_risk.value,
            "overall_confidence": self.overall_confidence.value,
            "evaluations": {k.value: v.to_dict() for k, v in self.evaluations.items()},
            "recommendations": [r.to_dict() for r in self.recommendations],
            "checksum": self.checksum,
        }
