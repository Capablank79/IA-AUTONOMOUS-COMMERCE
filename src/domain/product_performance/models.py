from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Mapping, Any, Tuple
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class PerformanceStatus(str, Enum):
    """
    Estado del cálculo/suficiencia de Product Performance (Task I.4).
    """
    SUFFICIENT_DATA = "SUFFICIENT_DATA"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNKNOWN = "UNKNOWN"
    DATA_QUALITY_WARNING = "DATA_QUALITY_WARNING"


@dataclass(frozen=True)
class TemporalPeriod:
    """
    Período temporal observable explícito para agregación de performance.
    """
    period_type: str  # e.g., "POINT_IN_TIME", "DAILY", "WEEKLY", "MONTHLY", "LIFETIME"
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    def __post_init__(self):
        if not self.period_type or not isinstance(self.period_type, str):
            raise ValueError("TemporalPeriod.period_type must be a non-empty string")


@dataclass(frozen=True)
class ObservedProductMetrics:
    """
    Métricas de Producto puramente OBSERVADAS (registradas directamente de la memoria/outcomes).
    Cero suposiciones o imputaciones.
    """
    observed_sales_units: Optional[int] = None
    observed_revenue: Optional[Decimal] = None
    observed_cancellations_units: Optional[int] = None
    observed_returns_units: Optional[int] = None
    observed_stock_level: Optional[int] = None
    observed_price: Optional[Decimal] = None
    observed_cost: Optional[Decimal] = None
    currency: str = "CLP"

    def __post_init__(self):
        if self.observed_sales_units is not None and self.observed_sales_units < 0:
            raise ValueError("observed_sales_units cannot be negative")
        if self.observed_cancellations_units is not None and self.observed_cancellations_units < 0:
            raise ValueError("observed_cancellations_units cannot be negative")
        if self.observed_returns_units is not None and self.observed_returns_units < 0:
            raise ValueError("observed_returns_units cannot be negative")
        if self.observed_stock_level is not None and self.observed_stock_level < 0:
            raise ValueError("observed_stock_level cannot be negative")


@dataclass(frozen=True)
class DerivedProductMetrics:
    """
    Métricas de Producto DERIVADAS a partir de observaciones válidas.
    Cero invención: si falta denominador/numerador, la métrica derivada correspondiente es None.
    """
    gross_margin_amount: Optional[Decimal] = None
    gross_margin_percentage: Optional[float] = None
    cancellation_rate: Optional[float] = None
    return_rate: Optional[float] = None
    outcome_success_rate: Optional[float] = None
    average_selling_price: Optional[Decimal] = None


@dataclass(frozen=True)
class ProductPerformanceRecord:
    """
    Registro de dominio inmutable para medir el desempeño comercial observable de productos (Task I.4).

    Reglas de Dominio:
    - Dominio puro: Sin dependencias de DB, HTTP, JSON, SQL, SDKs ni APIs externas.
    - Preserva identidad canónica del producto (`product_id` / `sku`).
    - Diferencia explícitamente entre datos OBSERVADOS, DERIVADOS y CONTEXTO.
    - Maneja de manera segura la insuficiencia de datos (INSUFFICIENT_DATA).
    - Preserva muestra explícita (`sample_count`, `outcome_sample_count`, `observation_sample_count`).
    - Vincula trazabilidad causal (`product_memory_ids`, `outcome_ids`, `mission_ids`, `decision_ids`).
    - Integra contexto opcional de Prediction vs Actual / Decision Calibration sin recalcular.
    """
    performance_id: str
    product_id: str
    sku: str
    period: TemporalPeriod
    status: PerformanceStatus = PerformanceStatus.UNKNOWN

    # Muestras
    sample_count: int = 0
    observation_sample_count: int = 0
    outcome_sample_count: int = 0

    # Métricas
    observed_metrics: ObservedProductMetrics = field(default_factory=ObservedProductMetrics)
    derived_metrics: DerivedProductMetrics = field(default_factory=DerivedProductMetrics)

    # Trazabilidad / Enlaces Causales
    product_memory_ids: Tuple[str, ...] = field(default_factory=tuple)
    outcome_ids: Tuple[str, ...] = field(default_factory=tuple)
    mission_ids: Tuple[str, ...] = field(default_factory=tuple)
    decision_ids: Tuple[str, ...] = field(default_factory=tuple)

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
            raise ValueError("ProductPerformanceRecord.performance_id must be a non-empty string")
        if not self.product_id or not isinstance(self.product_id, str):
            raise ValueError("ProductPerformanceRecord.product_id must be a non-empty string")
        if not self.sku or not isinstance(self.sku, str):
            raise ValueError("ProductPerformanceRecord.sku must be a non-empty string")

        if self.sample_count < 0 or self.observation_sample_count < 0 or self.outcome_sample_count < 0:
            raise ValueError("Sample counts cannot be negative")

        if not isinstance(self.product_memory_ids, tuple):
            object.__setattr__(self, "product_memory_ids", tuple(self.product_memory_ids))
        if not isinstance(self.outcome_ids, tuple):
            object.__setattr__(self, "outcome_ids", tuple(self.outcome_ids))
        if not isinstance(self.mission_ids, tuple):
            object.__setattr__(self, "mission_ids", tuple(self.mission_ids))
        if not isinstance(self.decision_ids, tuple):
            object.__setattr__(self, "decision_ids", tuple(self.decision_ids))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
