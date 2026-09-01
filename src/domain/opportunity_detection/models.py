from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, List, Dict

from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType
from src.domain.market_monitoring.models import MarketObservation, NormalizedPrice


class OpportunityType(str, Enum):
    """
    Tipos de oportunidad comercial identificables a partir de observaciones.
    """
    PRICE_ARBITRAGE = "PRICE_ARBITRAGE"
    HIGH_DEMAND_LOW_COMPETITION = "HIGH_DEMAND_LOW_COMPETITION"
    SUPPLY_SHORTAGE = "SUPPLY_SHORTAGE"
    COMPETITOR_OUT_OF_STOCK = "COMPETITOR_OUT_OF_STOCK"
    UNMET_DEMAND = "UNMET_DEMAND"
    TRENDING_PRODUCT = "TRENDING_PRODUCT"
    GENERAL_COMMERCIAL = "GENERAL_COMMERCIAL"


class OpportunityStatus(str, Enum):
    """
    Estado del registro de oportunidad.
    """
    DETECTED = "DETECTED"
    VALID = "VALID"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"
    DISCARDED = "DISCARDED"


@dataclass(frozen=True)
class ObservedOpportunityMetrics:
    """
    Métricas directamente observadas en las observaciones de mercado.
    Separación estricta: sólo contiene datos no inferidos que vienen de MarketObservation.
    UNKNOWN != 0 (se usa None / UNKNOWN para representar ausencia).
    """
    observed_price: Optional[NormalizedPrice] = None
    observed_sold_quantity: Optional[int] = None
    observed_stock: Optional[int] = None
    observed_competitor_count: Optional[int] = None
    lowest_competitor_price: Optional[NormalizedPrice] = None
    buy_box_winner_price: Optional[NormalizedPrice] = None
    observations_count: int = 1

    def __post_init__(self):
        if self.observed_sold_quantity is not None and self.observed_sold_quantity < 0:
            raise ValueError("observed_sold_quantity cannot be negative")
        if self.observed_stock is not None and self.observed_stock < 0:
            raise ValueError("observed_stock cannot be negative")
        if self.observed_competitor_count is not None and self.observed_competitor_count < 0:
            raise ValueError("observed_competitor_count cannot be negative")
        if self.observations_count < 1:
            raise ValueError("observations_count must be at least 1")


@dataclass(frozen=True)
class DerivedOpportunityMetrics:
    """
    Métricas calculadas determinísticamente por Opportunity Detection a partir de métricas observadas
    y reglas de dominio.
    Toda métrica derivada es determinista, explicable y reproducible.
    """
    price_gap_amount: Optional[Decimal] = None
    price_gap_ratio: Optional[Decimal] = None
    potential_margin_ratio: Optional[Decimal] = None
    competition_density: Optional[str] = None  # LOW, MEDIUM, HIGH, UNKNOWN
    demand_intensity: Optional[str] = None     # HIGH, MEDIUM, LOW, UNKNOWN
    opportunity_score: Optional[Decimal] = None  # Escala determinista 0.00 - 100.00
    scoring_rationale: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.scoring_rationale, tuple):
            object.__setattr__(self, "scoring_rationale", tuple(self.scoring_rationale))
        if self.opportunity_score is not None:
            if self.opportunity_score < Decimal("0") or self.opportunity_score > Decimal("100"):
                raise ValueError("opportunity_score must be between 0.0 and 100.0")


@dataclass(frozen=True)
class OpportunityRecord:
    """
    Entidad inmutable de Dominio para el Registro de Oportunidad Comercial (Hito J.3).
    Representa una oportunidad estructurada, determinista, trazable y persistente.
    NO crea DecisionRecord ni ejecuta acciones comerciales.
    """
    opportunity_id: str
    canonical_product_id: str
    marketplace: Marketplace
    detected_at: datetime
    opportunity_type: OpportunityType
    status: OpportunityStatus
    confidence: Confidence
    source_observation_ids: Tuple[str, ...]
    observed_metrics: ObservedOpportunityMetrics
    derived_metrics: DerivedOpportunityMetrics
    category: Optional[str] = None
    title: Optional[str] = None
    product_sku: Optional[str] = None
    product_memory_id_ref: Optional[str] = None
    supplier_memory_id_ref: Optional[str] = None
    provenance: str = "LIVE"
    correlation_id: str = "default-correlation"
    idempotency_key: str = ""
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    unknown_fields: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.opportunity_id or not isinstance(self.opportunity_id, str):
            raise ValueError("opportunity_id must be a non-empty string")
        if not self.canonical_product_id or not isinstance(self.canonical_product_id, str):
            raise ValueError("canonical_product_id must be a non-empty string")
        if not self.source_observation_ids:
            raise ValueError("source_observation_ids must contain at least one observation ID")
        if not isinstance(self.source_observation_ids, tuple):
            object.__setattr__(self, "source_observation_ids", tuple(self.source_observation_ids))
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))
        if not isinstance(self.unknown_fields, tuple):
            object.__setattr__(self, "unknown_fields", tuple(self.unknown_fields))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class OpportunityDetectionCriteria:
    """
    Criterios de configuración determinista para la detección de oportunidades comerciales.
    """
    min_confidence: Confidence = Confidence.LOW
    min_score: Decimal = Decimal("30.0")
    min_observations_required: int = 1
    require_valid_price: bool = True
    max_acceptable_competition: Optional[int] = None
    min_sold_quantity: Optional[int] = None
