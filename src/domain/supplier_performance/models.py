from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Mapping, Any, Tuple
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class SupplierPerformanceStatus(str, Enum):
    """
    Estado del cálculo/suficiencia de Supplier Performance (Task I.5).
    """
    SUFFICIENT_DATA = "SUFFICIENT_DATA"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNKNOWN = "UNKNOWN"
    DATA_QUALITY_WARNING = "DATA_QUALITY_WARNING"


@dataclass(frozen=True)
class SupplierTemporalPeriod:
    """
    Período temporal observable explícito para agregación de desempeño de proveedores.
    """
    period_type: str  # e.g., "POINT_IN_TIME", "DAILY", "WEEKLY", "MONTHLY", "LIFETIME"
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    def __post_init__(self):
        if not self.period_type or not isinstance(self.period_type, str):
            raise ValueError("SupplierTemporalPeriod.period_type must be a non-empty string")


@dataclass(frozen=True)
class ObservedSupplierMetrics:
    """
    Métricas de Proveedor puramente OBSERVADAS (registradas directamente de la memoria/cotizaciones/outcomes).
    Cero suposiciones o imputaciones.
    """
    total_quotes_observed: int = 0
    total_accepted_quotes: int = 0
    total_orders_placed: int = 0
    total_fulfilled_orders: int = 0
    total_delivered_on_time: int = 0
    total_cancelled_orders: int = 0
    total_defective_returns: int = 0
    
    observed_lead_times_days: Tuple[int, ...] = field(default_factory=tuple)
    observed_quoted_costs: Tuple[Decimal, ...] = field(default_factory=tuple)
    observed_moqs: Tuple[int, ...] = field(default_factory=tuple)
    currency: str = "CLP"

    def __post_init__(self):
        if self.total_quotes_observed < 0 or self.total_accepted_quotes < 0:
            raise ValueError("Quote counts cannot be negative")
        if self.total_orders_placed < 0 or self.total_fulfilled_orders < 0:
            raise ValueError("Order counts cannot be negative")
        if self.total_delivered_on_time < 0 or self.total_cancelled_orders < 0 or self.total_defective_returns < 0:
            raise ValueError("Delivery/cancellation/return counts cannot be negative")

        if not isinstance(self.observed_lead_times_days, tuple):
            object.__setattr__(self, "observed_lead_times_days", tuple(self.observed_lead_times_days))
        if not isinstance(self.observed_quoted_costs, tuple):
            object.__setattr__(self, "observed_quoted_costs", tuple(self.observed_quoted_costs))
        if not isinstance(self.observed_moqs, tuple):
            object.__setattr__(self, "observed_moqs", tuple(self.observed_moqs))


@dataclass(frozen=True)
class DerivedSupplierMetrics:
    """
    Métricas de Proveedor DERIVADAS a partir de observaciones válidas.
    Cero invención: si falta denominador/numerador o sample == 0, la métrica derivada correspondiente es None.
    """
    quote_acceptance_rate: Optional[float] = None
    average_quoted_cost: Optional[Decimal] = None
    average_moq: Optional[float] = None
    average_lead_time_days: Optional[float] = None
    delivery_on_time_rate: Optional[float] = None
    fulfillment_rate: Optional[float] = None
    cancellation_rate: Optional[float] = None
    defect_return_rate: Optional[float] = None
    outcome_success_rate: Optional[float] = None


@dataclass(frozen=True)
class SupplierPerformanceRecord:
    """
    Registro de dominio inmutable para medir el desempeño comercial y operativo de proveedores (Task I.5).

    Reglas de Dominio:
    - Dominio puro: Sin dependencias de DB, HTTP, JSON, SQL, SDKs ni APIs externas.
    - Preserva identidad canónica del proveedor (`supplier_id`).
    - Diferencia explícitamente entre datos OBSERVADOS, DERIVADOS y CONTEXTO.
    - Maneja de manera segura la insuficiencia de datos (INSUFFICIENT_DATA / UNKNOWN).
    - Preserva muestra explícita (`sample_count`, `quote_sample_count`, `outcome_sample_count`).
    - Vincula trazabilidad causal (`supplier_memory_ids`, `outcome_ids`, `mission_ids`, `decision_ids`, `action_ids`).
    - No asume que menor costo = mejor proveedor ni duplica Product Performance.
    """
    performance_id: str
    supplier_id: str
    period: SupplierTemporalPeriod
    status: SupplierPerformanceStatus = SupplierPerformanceStatus.UNKNOWN

    # Muestras
    sample_count: int = 0
    quote_sample_count: int = 0
    outcome_sample_count: int = 0

    # Métricas
    observed_metrics: ObservedSupplierMetrics = field(default_factory=ObservedSupplierMetrics)
    derived_metrics: DerivedSupplierMetrics = field(default_factory=DerivedSupplierMetrics)

    # Trazabilidad / Enlaces Causales
    supplier_memory_ids: Tuple[str, ...] = field(default_factory=tuple)
    outcome_ids: Tuple[str, ...] = field(default_factory=tuple)
    mission_ids: Tuple[str, ...] = field(default_factory=tuple)
    decision_ids: Tuple[str, ...] = field(default_factory=tuple)
    action_ids: Tuple[str, ...] = field(default_factory=tuple)

    # Contexto opcional de calibración/predicción si aplica
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
            raise ValueError("SupplierPerformanceRecord.performance_id must be a non-empty string")
        if not self.supplier_id or not isinstance(self.supplier_id, str):
            raise ValueError("SupplierPerformanceRecord.supplier_id must be a non-empty string")

        if self.sample_count < 0 or self.quote_sample_count < 0 or self.outcome_sample_count < 0:
            raise ValueError("Sample counts cannot be negative")

        if not isinstance(self.supplier_memory_ids, tuple):
            object.__setattr__(self, "supplier_memory_ids", tuple(self.supplier_memory_ids))
        if not isinstance(self.outcome_ids, tuple):
            object.__setattr__(self, "outcome_ids", tuple(self.outcome_ids))
        if not isinstance(self.mission_ids, tuple):
            object.__setattr__(self, "mission_ids", tuple(self.mission_ids))
        if not isinstance(self.decision_ids, tuple):
            object.__setattr__(self, "decision_ids", tuple(self.decision_ids))
        if not isinstance(self.action_ids, tuple):
            object.__setattr__(self, "action_ids", tuple(self.action_ids))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
