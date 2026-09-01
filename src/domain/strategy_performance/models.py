from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Mapping, Any, Tuple
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class StrategyPerformanceStatus(str, Enum):
    """
    Estado del cálculo/suficiencia de Strategy Performance (Task I.6).
    """
    SUFFICIENT_DATA = "SUFFICIENT_DATA"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNKNOWN = "UNKNOWN"
    DATA_QUALITY_WARNING = "DATA_QUALITY_WARNING"


@dataclass(frozen=True)
class StrategyTemporalPeriod:
    """
    Período temporal observable explícito para agregación de performance de estrategias.
    """
    period_type: str  # e.g., "POINT_IN_TIME", "DAILY", "WEEKLY", "MONTHLY", "LIFETIME"
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    def __post_init__(self):
        if not self.period_type or not isinstance(self.period_type, str):
            raise ValueError("StrategyTemporalPeriod.period_type must be a non-empty string")


@dataclass(frozen=True)
class ObservedStrategyMetrics:
    """
    Métricas de Estrategia puramente OBSERVADAS (registradas directamente desde decisiones, acciones y outcomes).
    Cero suposiciones o imputaciones.
    """
    total_decisions_observed: int = 0
    total_actions_executed: int = 0
    total_outcomes_observed: int = 0
    success_count: int = 0
    failure_count: int = 0
    partial_count: int = 0
    cancelled_count: int = 0
    unknown_count: int = 0
    observed_profit: Optional[Decimal] = None
    observed_revenue: Optional[Decimal] = None
    observed_cancellations: int = 0
    observed_returns: int = 0
    currency: str = "CLP"

    def __post_init__(self):
        if self.total_decisions_observed < 0:
            raise ValueError("total_decisions_observed cannot be negative")
        if self.total_actions_executed < 0:
            raise ValueError("total_actions_executed cannot be negative")
        if self.total_outcomes_observed < 0:
            raise ValueError("total_outcomes_observed cannot be negative")
        if self.success_count < 0:
            raise ValueError("success_count cannot be negative")
        if self.failure_count < 0:
            raise ValueError("failure_count cannot be negative")
        if self.partial_count < 0:
            raise ValueError("partial_count cannot be negative")
        if self.cancelled_count < 0:
            raise ValueError("cancelled_count cannot be negative")
        if self.unknown_count < 0:
            raise ValueError("unknown_count cannot be negative")
        if self.observed_cancellations < 0:
            raise ValueError("observed_cancellations cannot be negative")
        if self.observed_returns < 0:
            raise ValueError("observed_returns cannot be negative")


@dataclass(frozen=True)
class DerivedStrategyMetrics:
    """
    Métricas de Estrategia DERIVADAS a partir de observaciones válidas.
    Cero invención: si falta denominador/numerador, la métrica derivada correspondiente es None.
    """
    success_rate: Optional[float] = None
    outcome_success_rate: Optional[float] = None
    failure_rate: Optional[float] = None
    cancellation_rate: Optional[float] = None
    return_rate: Optional[float] = None
    average_realized_profit: Optional[Decimal] = None
    average_margin_percentage: Optional[float] = None
    average_realized_revenue: Optional[Decimal] = None


@dataclass(frozen=True)
class StrategyPerformanceRecord:
    """
    Registro de dominio inmutable para medir el desempeño comercial observable de estrategias (Task I.6).

    Reglas de Dominio:
    - Dominio puro: Sin dependencias de DB, HTTP, JSON, SQL, SDKs ni APIs externas.
    - Preserva identidad canónica de la estrategia (`strategy_id`).
    - Diferencia explícitamente entre datos OBSERVADOS, DERIVADOS y CONTEXTO.
    - Maneja de manera segura la insuficiencia de datos (INSUFFICIENT_DATA).
    - Preserva muestra explícita (`sample_count`, `decision_sample_count`, `action_sample_count`, `outcome_sample_count`).
    - Vincula trazabilidad causal (`decision_ids`, `action_ids`, `result_ids`, `outcome_ids`, `mission_ids`, `product_ids`, `supplier_ids`).
    - Integra contexto opcional de Prediction vs Actual / Decision Calibration sin recalcular.
    - Preserva proveniencia, auditoría y sanitización de datos sensibles.
    """
    performance_id: str
    strategy_id: str
    period: StrategyTemporalPeriod
    status: StrategyPerformanceStatus = StrategyPerformanceStatus.UNKNOWN

    # Muestras
    sample_count: int = 0
    decision_sample_count: int = 0
    action_sample_count: int = 0
    outcome_sample_count: int = 0

    # Métricas
    observed_metrics: ObservedStrategyMetrics = field(default_factory=ObservedStrategyMetrics)
    derived_metrics: DerivedStrategyMetrics = field(default_factory=DerivedStrategyMetrics)

    # Trazabilidad / Enlaces Causales
    decision_ids: Tuple[str, ...] = field(default_factory=tuple)
    action_ids: Tuple[str, ...] = field(default_factory=tuple)
    result_ids: Tuple[str, ...] = field(default_factory=tuple)
    outcome_ids: Tuple[str, ...] = field(default_factory=tuple)
    mission_ids: Tuple[str, ...] = field(default_factory=tuple)
    product_ids: Tuple[str, ...] = field(default_factory=tuple)
    supplier_ids: Tuple[str, ...] = field(default_factory=tuple)

    # Contexto de calibración/predicción opcional
    calibration_context_id: Optional[str] = None
    contextual_prediction_error: Optional[float] = None

    # Procedencia y Auditoría
    evidence_reference: Optional[str] = None
    confidence: Confidence = Confidence.MEDIUM
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    calculated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.performance_id or not isinstance(self.performance_id, str):
            raise ValueError("StrategyPerformanceRecord.performance_id must be a non-empty string")
        if not self.strategy_id or not isinstance(self.strategy_id, str):
            raise ValueError("StrategyPerformanceRecord.strategy_id must be a non-empty string")

        if (
            self.sample_count < 0
            or self.decision_sample_count < 0
            or self.action_sample_count < 0
            or self.outcome_sample_count < 0
        ):
            raise ValueError("Sample counts cannot be negative")

        if not isinstance(self.decision_ids, tuple):
            object.__setattr__(self, "decision_ids", tuple(self.decision_ids))
        if not isinstance(self.action_ids, tuple):
            object.__setattr__(self, "action_ids", tuple(self.action_ids))
        if not isinstance(self.result_ids, tuple):
            object.__setattr__(self, "result_ids", tuple(self.result_ids))
        if not isinstance(self.outcome_ids, tuple):
            object.__setattr__(self, "outcome_ids", tuple(self.outcome_ids))
        if not isinstance(self.mission_ids, tuple):
            object.__setattr__(self, "mission_ids", tuple(self.mission_ids))
        if not isinstance(self.product_ids, tuple):
            object.__setattr__(self, "product_ids", tuple(self.product_ids))
        if not isinstance(self.supplier_ids, tuple):
            object.__setattr__(self, "supplier_ids", tuple(self.supplier_ids))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
