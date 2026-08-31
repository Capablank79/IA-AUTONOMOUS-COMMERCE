from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Tuple, Mapping, Any
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence, SignalType


class SupplierStatus(str, Enum):
    RESEARCH = "RESEARCH"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"


class ProductMatchGrade(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    CLOSE_MATCH = "CLOSE_MATCH"
    VARIANT = "VARIANT"
    UNCERTAIN_MATCH = "UNCERTAIN_MATCH"
    NO_MATCH = "NO_MATCH"


class SupplierReadiness(str, Enum):
    DISCOVERED = "DISCOVERED"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"
    EVALUATED = "EVALUATED"
    READY_FOR_ECONOMICS = "READY_FOR_ECONOMICS"
    REJECTED = "REJECTED"


class EvidenceProvenanceType(str, Enum):
    LIVE = "LIVE"
    FIXTURE = "FIXTURE"
    MOCK = "MOCK"
    FAKE = "FAKE"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"


class MOQType(str, Enum):
    ORDER = "ORDER"
    SKU = "SKU"
    VARIANT = "VARIANT"
    UNKNOWN = "UNKNOWN"


class QuoteFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    UNKNOWN_FRESHNESS = "UNKNOWN_FRESHNESS"


class QuoteComparabilityStatus(str, Enum):
    COMPARABLE = "COMPARABLE"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    PARTIALLY_COMPARABLE = "PARTIALLY_COMPARABLE"


class QuoteConflictStatus(str, Enum):
    NO_CONFLICT = "NO_CONFLICT"
    UNRESOLVED = "UNRESOLVED"
    RESOLVED_BY_NEWER_EVIDENCE = "RESOLVED_BY_NEWER_EVIDENCE"
    RESOLVED_BY_HIGHER_CONFIDENCE = "RESOLVED_BY_HIGHER_CONFIDENCE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class ShippingMethod(str, Enum):
    STANDARD = "STANDARD"
    EXPRESS = "EXPRESS"
    SAME_DAY = "SAME_DAY"
    FREIGHT = "FREIGHT"
    PICKUP = "PICKUP"
    UNKNOWN = "UNKNOWN"


class ShippingComparabilityStatus(str, Enum):
    COMPARABLE = "COMPARABLE"
    NOT_COMPARABLE_ZONE = "NOT_COMPARABLE_ZONE"
    NOT_COMPARABLE_UNKNOWN_COST = "NOT_COMPARABLE_UNKNOWN_COST"
    NOT_COMPARABLE_METHOD = "NOT_COMPARABLE_METHOD"
    PARTIALLY_COMPARABLE = "PARTIALLY_COMPARABLE"


class SLAStatus(str, Enum):
    COMPLIANT = "COMPLIANT"
    PARTIALLY_COMPLIANT = "PARTIALLY_COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    UNKNOWN = "UNKNOWN"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class PerformanceTrend(str, Enum):
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    DETERIORATING = "DETERIORATING"
    VOLATILE = "VOLATILE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


class SupplierRejectionReason(str, Enum):
    NO_PRODUCT_MATCH = "NO_PRODUCT_MATCH"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    EXCESSIVE_MOQ = "EXCESSIVE_MOQ"
    EXCESSIVE_PRICE = "EXCESSIVE_PRICE"
    UNRELIABLE_SOURCE = "UNRELIABLE_SOURCE"
    CRITICAL_UNCERTAINTY = "CRITICAL_UNCERTAINTY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    HIGH_OPERATIONAL_RISK = "HIGH_OPERATIONAL_RISK"
    LOGISTICS_INCOMPATIBILITY = "LOGISTICS_INCOMPATIBILITY"
    POOR_SLA_COMPLIANCE = "POOR_SLA_COMPLIANCE"
    OTHER = "OTHER"


class SupplierRecommendationDecision(str, Enum):
    """
    Taxonomía formal e inmutable para las decisiones finales de recomendación de proveedores (C-04).
    """
    RECOMMEND = "RECOMMEND"
    RECOMMEND_WITH_CONDITIONS = "RECOMMEND_WITH_CONDITIONS"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"
    NO_RECOMMENDATION = "NO_RECOMMENDATION"
    REJECT = "REJECT"


class ContingencyTrigger(str, Enum):
    """
    Causales deterministas que invalidan al proveedor primario y activan un fallback.
    """
    STOCK_UNAVAILABLE = "STOCK_UNAVAILABLE"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    CRITICAL_RISK = "CRITICAL_RISK"
    POOR_SLA_COMPLIANCE = "POOR_SLA_COMPLIANCE"
    LEAD_TIME_EXCEEDED = "LEAD_TIME_EXCEEDED"
    SHIPPING_INCOMPATIBLE = "SHIPPING_INCOMPATIBLE"
    PRODUCT_MISMATCH = "PRODUCT_MISMATCH"
    EVIDENCE_DETERIORATED = "EVIDENCE_DETERIORATED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"



@dataclass(frozen=True)
class SupplierLocation:
    country: str
    city: Optional[str] = None
    region: Optional[str] = None

    def __post_init__(self):
        if not self.country:
            raise ValueError("country cannot be empty")


@dataclass(frozen=True)
class SupplierContact:
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None


@dataclass(frozen=True)
class SupplierProductReference:
    sku: Optional[str] = None
    title: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    category: Optional[str] = None
    source_product_id: Optional[str] = None
    source_url: Optional[str] = None


@dataclass(frozen=True)
class ProductMatch:
    grade: ProductMatchGrade
    confidence: Confidence
    matched_fields: Tuple[str, ...] = field(default_factory=tuple)
    discrepancies: Tuple[str, ...] = field(default_factory=tuple)
    details: str = ""

    def __post_init__(self):
        if not isinstance(self.matched_fields, tuple):
            object.__setattr__(self, "matched_fields", tuple(self.matched_fields))
        if not isinstance(self.discrepancies, tuple):
            object.__setattr__(self, "discrepancies", tuple(self.discrepancies))


@dataclass(frozen=True)
class PriceTier:
    """
    Representa un escalón de precio por volumen.
    Ejemplo: min_quantity=10, max_quantity=49, unit_price=20000.
    Si max_quantity es None, representa N o más (ej: 100+).
    """
    min_quantity: int
    unit_price: Decimal
    max_quantity: Optional[int] = None
    currency: str = "CLP"

    def __post_init__(self):
        if self.min_quantity < 1:
            raise ValueError("min_quantity must be at least 1")
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise ValueError("max_quantity cannot be less than min_quantity")
        if self.unit_price <= 0:
            raise ValueError("unit_price must be greater than zero")
        if not self.currency:
            raise ValueError("currency cannot be empty")


@dataclass(frozen=True)
class MOQInfo:
    """
    Representación explícita de MOQ (Minimum Order Quantity).
    Distingue MOQ conocido de desconocido y tipo de MOQ (SKU, VARIANT, ORDER).
    NO asume MOQ = 1 si no se observa.
    """
    quantity: Optional[int]
    moq_type: MOQType = MOQType.SKU
    notes: Optional[str] = None

    def __post_init__(self):
        if self.quantity is not None and self.quantity < 1:
            raise ValueError("MOQ quantity must be at least 1 when specified")

    @property
    def is_known(self) -> bool:
        return self.quantity is not None


@dataclass(frozen=True)
class CommercialQuote:
    """
    Representación rica e inmutable de una cotización comercial para comparación.
    Conserva procedencia, frescura, confianza y mantiene unknowns explícitos.
    """
    quote_id: str
    supplier_id: str
    sku: str
    unit_price: Optional[Decimal]
    currency: str
    moq: MOQInfo
    price_tiers: Tuple[PriceTier, ...] = field(default_factory=tuple)
    shipping_cost: Optional[Decimal] = None
    lead_time_days: Optional[int] = None
    stock_available: Optional[bool] = None
    available_quantity: Optional[int] = None
    commercial_conditions: Mapping[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.UNKNOWN
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    source: str = "INTERNAL_CATALOG"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until: Optional[datetime] = None
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not self.quote_id:
            raise ValueError("quote_id must be valid")
        if not self.supplier_id:
            raise ValueError("supplier_id must be valid")
        if not self.sku:
            raise ValueError("sku must be valid")
        if self.unit_price is not None and self.unit_price <= 0:
            raise ValueError("unit_price must be greater than zero")
        if self.shipping_cost is not None and self.shipping_cost < 0:
            raise ValueError("shipping_cost cannot be negative")
        if self.lead_time_days is not None and self.lead_time_days < 0:
            raise ValueError("lead_time_days cannot be negative")
        if self.available_quantity is not None and self.available_quantity < 0:
            raise ValueError("available_quantity cannot be negative")
        if not isinstance(self.price_tiers, tuple):
            object.__setattr__(self, "price_tiers", tuple(self.price_tiers))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.commercial_conditions, MappingProxyType):
            object.__setattr__(self, "commercial_conditions", MappingProxyType(dict(self.commercial_conditions)))

    def get_unit_price_for_quantity(self, quantity: int) -> Optional[Decimal]:
        """
        Determina determinísticamente el precio unitario correspondiente a una cantidad.
        Si la cotización tiene price_tiers, evalúa el tier aplicable.
        Si no hay tiers aplicables o no hay precio base, retorna unit_price base o None.
        """
        if quantity < 1:
            raise ValueError("quantity must be at least 1")

        if self.price_tiers:
            # Buscar el tier que coincida
            for tier in sorted(self.price_tiers, key=lambda t: t.min_quantity, reverse=True):
                if quantity >= tier.min_quantity:
                    if tier.max_quantity is None or quantity <= tier.max_quantity:
                        return tier.unit_price
        return self.unit_price

    @property
    def freshness(self) -> QuoteFreshness:
        now = datetime.now(timezone.utc)
        if self.valid_until is not None:
            # Normalizar aware/naive
            valid_dt = self.valid_until if self.valid_until.tzinfo else self.valid_until.replace(tzinfo=timezone.utc)
            if now > valid_dt:
                return QuoteFreshness.EXPIRED
        if self.observed_at is None:
            return QuoteFreshness.UNKNOWN_FRESHNESS
        obs_dt = self.observed_at if self.observed_at.tzinfo else self.observed_at.replace(tzinfo=timezone.utc)
        age_days = (now - obs_dt).total_seconds() / 86400.0
        if age_days > 90:
            return QuoteFreshness.EXPIRED
        if age_days > 30:
            return QuoteFreshness.STALE
        return QuoteFreshness.FRESH


@dataclass(frozen=True)
class ConfirmedQuote:
    """
    Representa una cotización comercial confirmada por el proveedor.
    Completa la evidencia cuando los datos de mercado son parciales.
    """
    quote_id: str
    wholesale_price: Decimal
    shipping_cost: Decimal
    lead_time_days: int
    currency: str = "CLP"

    def __post_init__(self):
        if not self.quote_id:
            raise ValueError("quote_id must be valid")
        if self.wholesale_price <= 0:
            raise ValueError("wholesale_price must be greater than zero")
        if self.shipping_cost < 0:
            raise ValueError("shipping_cost cannot be negative")
        if self.lead_time_days < 0:
            raise ValueError("lead_time_days cannot be negative")


@dataclass(frozen=True)
class SupplierData:
    """
    Representa los datos base de un proveedor, extraídos de la fuente de verdad.
    """
    supplier_id: str
    name: str
    country: str
    status: str
    location: Optional[SupplierLocation] = None
    contact: Optional[SupplierContact] = None
    source: str = "LOCAL_CATALOG"
    source_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.supplier_id:
            raise ValueError("supplier_id must be valid")
        if not self.name:
            raise ValueError("name must be valid")


@dataclass(frozen=True)
class SupplierEvidence:
    """
    Evidencia inmutable del mercado de proveedores con procedencia, frescura y señales.
    """
    supplier_id: str
    sku: str
    wholesale_price: Optional[Decimal] = None
    currency: str = "CLP"
    minimum_order_quantity: Optional[int] = 1
    stock_available: Optional[bool] = None
    shipping_cost: Optional[Decimal] = None
    lead_time_days: Optional[int] = None
    confidence: Confidence = Confidence.UNKNOWN
    signal_type: SignalType = SignalType.OBSERVED
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    source: str = "INTERNAL_CATALOG"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: Mapping[str, Any] = field(default_factory=dict)
    quote: Optional[ConfirmedQuote] = None
    commercial_quote: Optional[CommercialQuote] = None

    def __post_init__(self):
        if not self.supplier_id:
            raise ValueError("supplier_id must be valid")
        if not self.sku:
            raise ValueError("sku must be valid")
        if self.wholesale_price is not None and self.wholesale_price <= 0:
            raise ValueError("wholesale_price must be greater than zero")
        if self.minimum_order_quantity is not None and self.minimum_order_quantity < 1:
            raise ValueError("minimum_order_quantity must be at least 1")
        if self.shipping_cost is not None and self.shipping_cost < 0:
            raise ValueError("shipping_cost cannot be negative")
        if self.lead_time_days is not None and self.lead_time_days < 0:
            raise ValueError("lead_time_days cannot be negative")
        if not isinstance(self.raw_payload, MappingProxyType):
            object.__setattr__(self, "raw_payload", MappingProxyType(dict(self.raw_payload)))


@dataclass(frozen=True)
class Supplier:
    """
    Entidad inmutable de Dominio que representa a un proveedor identificado y normalizado.
    """
    supplier_id: str
    name: str
    source: str
    source_type: EvidenceProvenanceType
    location: Optional[SupplierLocation] = None
    contact: Optional[SupplierContact] = None
    status: SupplierStatus = SupplierStatus.RESEARCH
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    product_reference: Optional[SupplierProductReference] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.supplier_id:
            raise ValueError("supplier_id must be valid")
        if not self.name:
            raise ValueError("name must be valid")
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class SupplierScoreBreakdown:
    match_score: Decimal
    price_score: Decimal
    availability_score: Decimal
    lead_time_score: Decimal
    reliability_score: Decimal
    total_score: Decimal
    explanation: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.explanation, tuple):
            object.__setattr__(self, "explanation", tuple(self.explanation))


@dataclass(frozen=True)
class SupplierCandidate:
    """
    Representa un candidato a proveedor descubierto para una oportunidad específica.
    """
    supplier: Supplier
    evidence: SupplierEvidence
    product_match: ProductMatch
    readiness: SupplierReadiness = SupplierReadiness.DISCOVERED
    score_breakdown: Optional[SupplierScoreBreakdown] = None
    rank: Optional[int] = None
    risks: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    rejection_reason: Optional[SupplierRejectionReason] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.risks, tuple):
            object.__setattr__(self, "risks", tuple(self.risks))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))

    @property
    def score(self) -> Optional[Decimal]:
        if self.score_breakdown is not None:
            return self.score_breakdown.total_score
        return None


@dataclass(frozen=True)
class BestKnownSupplier:
    """
    Representa el mejor candidato a proveedor conocido preservado en el estado del loop.
    Reconstruible e inmutable.
    """
    supplier_id: str
    name: str
    source: str
    source_type: EvidenceProvenanceType
    sku: str
    score: Decimal
    confidence: Confidence
    product_match_grade: ProductMatchGrade
    readiness: SupplierReadiness
    iteration: int
    why_best: str
    evidence_snapshot: SupplierEvidence
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class QuoteConflict:
    """
    Representa un conflicto detectado entre dos cotizaciones para el mismo proveedor/SKU.
    """
    quote_a_id: str
    quote_b_id: str
    supplier_id: str
    sku: str
    conflict_type: str
    description: str
    resolution_status: QuoteConflictStatus = QuoteConflictStatus.UNRESOLVED
    resolved_quote_id: Optional[str] = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class QuoteScenarioEvaluation:
    """
    Evaluación de una cotización bajo un escenario específico de cantidad (ej: QTY=1, QTY=MOQ, QTY=50).
    """
    scenario_quantity: int
    unit_price: Optional[Decimal]
    currency: str
    total_goods_cost: Optional[Decimal]
    shipping_cost: Optional[Decimal]
    total_estimated_landed_subtotal: Optional[Decimal]
    is_moq_satisfied: bool
    is_comparable: bool
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    def __post_init__(self):
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))


@dataclass(frozen=True)
class SupplierQuoteComparisonItem:
    """
    Elemento comparativo detallado de un proveedor y su cotización.
    """
    supplier: Supplier
    quote: CommercialQuote
    product_match: ProductMatch
    comparability_status: QuoteComparabilityStatus
    commercial_score: Optional[Decimal]
    score_breakdown: Optional[SupplierScoreBreakdown]
    rank: Optional[int]
    scenario_evaluations: Mapping[int, QuoteScenarioEvaluation] = field(default_factory=dict)
    knowns: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    risks: Tuple[str, ...] = field(default_factory=tuple)
    advantages: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.knowns, tuple):
            object.__setattr__(self, "knowns", tuple(self.knowns))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.risks, tuple):
            object.__setattr__(self, "risks", tuple(self.risks))
        if not isinstance(self.advantages, tuple):
            object.__setattr__(self, "advantages", tuple(self.advantages))
        if not isinstance(self.scenario_evaluations, MappingProxyType):
            object.__setattr__(self, "scenario_evaluations", MappingProxyType(dict(self.scenario_evaluations)))


@dataclass(frozen=True)
class BestCommercialCandidate:
    """
    Representa el mejor candidato comercial preliminar (NO recomendación definitiva de compra).
    """
    supplier_id: str
    supplier_name: str
    quote_id: str
    sku: str
    currency: str
    unit_price: Optional[Decimal]
    moq: Optional[int]
    lead_time_days: Optional[int]
    shipping_cost: Optional[Decimal]
    commercial_score: Decimal
    confidence: Confidence
    freshness: QuoteFreshness
    provenance_type: EvidenceProvenanceType
    why_best: str
    key_advantages: Tuple[str, ...] = field(default_factory=tuple)
    remaining_unknowns: Tuple[str, ...] = field(default_factory=tuple)
    iteration: int = 1
    selected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.key_advantages, tuple):
            object.__setattr__(self, "key_advantages", tuple(self.key_advantages))
        if not isinstance(self.remaining_unknowns, tuple):
            object.__setattr__(self, "remaining_unknowns", tuple(self.remaining_unknowns))


@dataclass(frozen=True)
class QuoteComparisonResult:
    """
    Resultado global determinista de la comparación de cotizaciones comerciales (C-02).
    """
    target_product_title: str
    target_sku: Optional[str]
    analysis_quantities: Tuple[int, ...]
    items: Tuple[SupplierQuoteComparisonItem, ...]
    ranked_items: Tuple[SupplierQuoteComparisonItem, ...]
    best_commercial_candidate: Optional[BestCommercialCandidate]
    conflicts: Tuple[QuoteConflict, ...] = field(default_factory=tuple)
    non_comparable_reasons: Tuple[str, ...] = field(default_factory=tuple)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.analysis_quantities, tuple):
            object.__setattr__(self, "analysis_quantities", tuple(self.analysis_quantities))
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))
        if not isinstance(self.ranked_items, tuple):
            object.__setattr__(self, "ranked_items", tuple(self.ranked_items))
        if not isinstance(self.conflicts, tuple):
            object.__setattr__(self, "conflicts", tuple(self.conflicts))
        if not isinstance(self.non_comparable_reasons, tuple):
            object.__setattr__(self, "non_comparable_reasons", tuple(self.non_comparable_reasons))


# ==============================================================================
# MODELOS DE DOMINIO PARA C-03: RISK, RELIABILITY, LOGISTICS & PERFORMANCE
# ==============================================================================

@dataclass(frozen=True)
class LeadTimeProfile:
    """
    Representación rica y explícita de Lead Time (C.8).
    Distingue valores observados, rangos y estadísticas calculadas determinísticamente
    cuando existe suficiente historial. NO inventa distribuciones.
    """
    observed_days: Optional[int]
    min_days: Optional[int] = None
    max_days: Optional[int] = None
    historical_avg_days: Optional[float] = None
    historical_variance_days: Optional[float] = None
    on_time_rate: Optional[float] = None  # 0.0 - 1.0 si hay historial
    confidence: Confidence = Confidence.UNKNOWN
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    source: str = "CATALOG_OBSERVATION"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.observed_days is not None and self.observed_days < 0:
            raise ValueError("observed_days cannot be negative")
        if self.min_days is not None and self.min_days < 0:
            raise ValueError("min_days cannot be negative")
        if self.max_days is not None and self.max_days < 0:
            raise ValueError("max_days cannot be negative")
        if self.min_days is not None and self.max_days is not None and self.min_days > self.max_days:
            raise ValueError("min_days cannot exceed max_days")
        if self.on_time_rate is not None and not (0.0 <= self.on_time_rate <= 1.0):
            raise ValueError("on_time_rate must be between 0.0 and 1.0")
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))

    @property
    def is_known(self) -> bool:
        return self.observed_days is not None or self.min_days is not None or self.historical_avg_days is not None


@dataclass(frozen=True)
class ShippingOption:
    """
    Representación estructurada de una opción de envío y flete (C.9).
    Separa costo, origen, destino, método y transportista.
    """
    shipping_cost: Optional[Decimal]
    currency: str = "CLP"
    origin_zone: Optional[str] = None
    destination_zone: Optional[str] = None
    method: ShippingMethod = ShippingMethod.UNKNOWN
    carrier: Optional[str] = None
    estimated_transit_days: Optional[int] = None
    is_free_shipping_observed: bool = False
    confidence: Confidence = Confidence.UNKNOWN
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.shipping_cost is not None and self.shipping_cost < 0:
            raise ValueError("shipping_cost cannot be negative")
        if self.estimated_transit_days is not None and self.estimated_transit_days < 0:
            raise ValueError("estimated_transit_days cannot be negative")
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))


@dataclass(frozen=True)
class SLARecord:
    """
    Registro explícito de un Acuerdo de Nivel de Servicio (SLA) (C.10).
    """
    metric_name: str
    target_value: float
    observed_value: Optional[float]
    unit: str
    compliance_status: SLAStatus
    deviation: Optional[float] = None
    evidence_source: str = "HISTORICAL_EVENTS"
    confidence: Confidence = Confidence.UNKNOWN

    def __post_init__(self):
        if not self.metric_name:
            raise ValueError("metric_name cannot be empty")


@dataclass(frozen=True)
class SupplierObservationEvent:
    """
    Registro histórico temporal inmutable de una observación sobre el proveedor (C.12 / C.14).
    """
    event_id: str
    supplier_id: str
    metric: str
    observed_value: Any
    unit: Optional[str] = None
    source: str = "ORDER_FULFILLMENT"
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    confidence: Confidence = Confidence.HIGH
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    notes: Optional[str] = None

    def __post_init__(self):
        if not self.event_id:
            raise ValueError("event_id cannot be empty")
        if not self.supplier_id:
            raise ValueError("supplier_id cannot be empty")
        if not self.metric:
            raise ValueError("metric cannot be empty")


@dataclass(frozen=True)
class HistoricalPerformanceProfile:
    """
    Perfil de desempeño histórico acumulado y tendencias del proveedor (C.12).
    """
    supplier_id: str
    observation_count: int
    first_observed_at: Optional[datetime]
    last_observed_at: Optional[datetime]
    fulfillment_rate: Optional[float] = None  # 0.0 - 1.0
    cancellation_rate: Optional[float] = None  # 0.0 - 1.0
    on_time_delivery_rate: Optional[float] = None  # 0.0 - 1.0
    incident_count: int = 0
    lead_time_trend: PerformanceTrend = PerformanceTrend.INSUFFICIENT_HISTORY
    sla_trend: PerformanceTrend = PerformanceTrend.INSUFFICIENT_HISTORY
    sla_records: Tuple[SLARecord, ...] = field(default_factory=tuple)
    events: Tuple[SupplierObservationEvent, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.observation_count < 0:
            raise ValueError("observation_count cannot be negative")
        if self.incident_count < 0:
            raise ValueError("incident_count cannot be negative")
        if self.fulfillment_rate is not None and not (0.0 <= self.fulfillment_rate <= 1.0):
            raise ValueError("fulfillment_rate must be between 0.0 and 1.0")
        if self.cancellation_rate is not None and not (0.0 <= self.cancellation_rate <= 1.0):
            raise ValueError("cancellation_rate must be between 0.0 and 1.0")
        if self.on_time_delivery_rate is not None and not (0.0 <= self.on_time_delivery_rate <= 1.0):
            raise ValueError("on_time_delivery_rate must be between 0.0 and 1.0")
        if not isinstance(self.sla_records, tuple):
            object.__setattr__(self, "sla_records", tuple(self.sla_records))
        if not isinstance(self.events, tuple):
            object.__setattr__(self, "events", tuple(self.events))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))


@dataclass(frozen=True)
class ReliabilityEvaluation:
    """
    Evaluación determinista de confiabilidad del proveedor (C.10).
    """
    supplier_id: str
    reliability_score: Optional[Decimal]  # 0 - 100 si es computable, None si UNKNOWN
    sla_compliance_rate: Optional[float]  # 0.0 - 1.0
    stock_consistency_score: Optional[Decimal]
    confidence: Confidence
    known_factors: Tuple[str, ...] = field(default_factory=tuple)
    unknown_factors: Tuple[str, ...] = field(default_factory=tuple)
    explanation: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.known_factors, tuple):
            object.__setattr__(self, "known_factors", tuple(self.known_factors))
        if not isinstance(self.unknown_factors, tuple):
            object.__setattr__(self, "unknown_factors", tuple(self.unknown_factors))
        if not isinstance(self.explanation, tuple):
            object.__setattr__(self, "explanation", tuple(self.explanation))


@dataclass(frozen=True)
class SupplierRiskDimension:
    """
    Evaluación individual de una dimensión de riesgo (C.11).
    """
    dimension_name: str
    risk_level: RiskLevel
    risk_score: Optional[Decimal]  # 0.0 (mínimo riesgo) - 100.0 (máximo riesgo), None si UNKNOWN
    signals_observed: Tuple[str, ...] = field(default_factory=tuple)
    uncertainties: Tuple[str, ...] = field(default_factory=tuple)
    explanation: str = ""

    def __post_init__(self):
        if not isinstance(self.signals_observed, tuple):
            object.__setattr__(self, "signals_observed", tuple(self.signals_observed))
        if not isinstance(self.uncertainties, tuple):
            object.__setattr__(self, "uncertainties", tuple(self.uncertainties))


@dataclass(frozen=True)
class SupplierRiskProfile:
    """
    Perfil de riesgo global explicable y determinista del proveedor (C.11).
    Separa riesgo operativo, logístico, disponibilidad, evidencia, comercial y concentración.
    """
    supplier_id: str
    overall_risk_level: RiskLevel
    overall_risk_score: Optional[Decimal]  # 0.0 (bajo riesgo) - 100.0 (riesgo crítico)
    operational_risk: SupplierRiskDimension
    logistics_risk: SupplierRiskDimension
    availability_risk: SupplierRiskDimension
    evidence_risk: SupplierRiskDimension
    commercial_risk: SupplierRiskDimension
    concentration_risk: Optional[SupplierRiskDimension] = None
    is_reject_recommended: bool = False
    rejection_reasons: Tuple[SupplierRejectionReason, ...] = field(default_factory=tuple)
    confidence: Confidence = Confidence.UNKNOWN
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    explanation: Tuple[str, ...] = field(default_factory=tuple)
    assessed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.rejection_reasons, tuple):
            object.__setattr__(self, "rejection_reasons", tuple(self.rejection_reasons))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.explanation, tuple):
            object.__setattr__(self, "explanation", tuple(self.explanation))


@dataclass(frozen=True)
class SupplierRiskComparisonItem:
    """
    Item comparativo completo de un proveedor integrando aspecto comercial, riesgo, confiabilidad y logística.
    """
    supplier: Supplier
    quote: Optional[CommercialQuote]
    lead_time_profile: LeadTimeProfile
    shipping_option: ShippingOption
    reliability: ReliabilityEvaluation
    risk_profile: SupplierRiskProfile
    historical_performance: HistoricalPerformanceProfile
    preliminary_commercial_score: Optional[Decimal]
    composite_suitability_score: Optional[Decimal]  # Score determinista preliminar integrando precio, confiabilidad y riesgo
    rank: Optional[int]
    is_disqualified: bool = False
    disqualification_reason: Optional[str] = None
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))


@dataclass(frozen=True)
class BestSupplierCandidate:
    """
    Representa el mejor candidato a proveedor conocido considerando condiciones comerciales,
    confiabilidad, logística y riesgo (C-03).
    NO constituye aún una recomendación final de compra (reservado para C-04).
    """
    supplier_id: str
    supplier_name: str
    sku: str
    commercial_score: Optional[Decimal]
    reliability_score: Optional[Decimal]
    overall_risk_score: Optional[Decimal]
    composite_suitability_score: Decimal
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    why_best: str
    key_strengths: Tuple[str, ...] = field(default_factory=tuple)
    identified_risks: Tuple[str, ...] = field(default_factory=tuple)
    remaining_unknowns: Tuple[str, ...] = field(default_factory=tuple)
    iteration: int = 1
    selected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.key_strengths, tuple):
            object.__setattr__(self, "key_strengths", tuple(self.key_strengths))
        if not isinstance(self.identified_risks, tuple):
            object.__setattr__(self, "identified_risks", tuple(self.identified_risks))
        if not isinstance(self.remaining_unknowns, tuple):
            object.__setattr__(self, "remaining_unknowns", tuple(self.remaining_unknowns))


@dataclass(frozen=True)
class SupplierRiskEvaluationResult:
    """
    Resultado global de la evaluación de riesgo, confiabilidad y logística de proveedores (C-03).
    """
    target_product_title: str
    target_sku: Optional[str]
    items: Tuple[SupplierRiskComparisonItem, ...]
    ranked_items: Tuple[SupplierRiskComparisonItem, ...]
    best_supplier_candidate: Optional[BestSupplierCandidate]
    rejected_candidates: Tuple[SupplierRiskComparisonItem, ...] = field(default_factory=tuple)
    non_comparable_logistics_reasons: Tuple[str, ...] = field(default_factory=tuple)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))
        if not isinstance(self.ranked_items, tuple):
            object.__setattr__(self, "ranked_items", tuple(self.ranked_items))
        if not isinstance(self.rejected_candidates, tuple):
            object.__setattr__(self, "rejected_candidates", tuple(self.rejected_candidates))
        if not isinstance(self.non_comparable_logistics_reasons, tuple):
            object.__setattr__(self, "non_comparable_logistics_reasons", tuple(self.non_comparable_logistics_reasons))


# ==============================================================================
# MODELOS DE DOMINIO PARA C-04: SUPPLIER RECOMMENDATION & DECISION ENGINE
# ==============================================================================

@dataclass(frozen=True)
class RecommendationCondition:
    """
    Condición explícita que debe verificarse o cumplirse para activar una recomendación.
    Ejemplo: 'Verificar costo de envío exacto antes de compra' o 'Confirmar stock disponible'.
    """
    code: str
    description: str
    is_critical: bool = True
    suggested_action: str = ""


@dataclass(frozen=True)
class PrimarySupplierSelection:
    """
    Selección inmutable del proveedor primario recomendado.
    Contiene la evidencia completa, scores, justificación de por qué fue seleccionado frente a alternativas
    y condiciones de invalidación.
    """
    supplier_id: str
    supplier_name: str
    sku: str
    commercial_score: Optional[Decimal]
    reliability_score: Optional[Decimal]
    overall_risk_score: Optional[Decimal]
    composite_suitability_score: Decimal
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    selection_reason: str
    why_over_fallback: str
    commercial_position: str
    logistics_position: str
    key_strengths: Tuple[str, ...] = field(default_factory=tuple)
    identified_risks: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    invalidation_criteria: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.key_strengths, tuple):
            object.__setattr__(self, "key_strengths", tuple(self.key_strengths))
        if not isinstance(self.identified_risks, tuple):
            object.__setattr__(self, "identified_risks", tuple(self.identified_risks))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.invalidation_criteria, tuple):
            object.__setattr__(self, "invalidation_criteria", tuple(self.invalidation_criteria))


@dataclass(frozen=True)
class FallbackSupplierSelection:
    """
    Selección inmutable del proveedor secundario de contingencia (Fallback).
    Solo se selecciona si es genuinamente viable (supera umbrales mínimos).
    """
    supplier_id: str
    supplier_name: str
    sku: str
    commercial_score: Optional[Decimal]
    reliability_score: Optional[Decimal]
    overall_risk_score: Optional[Decimal]
    composite_suitability_score: Decimal
    confidence: Confidence
    provenance_type: EvidenceProvenanceType
    fallback_reason: str
    tradeoffs_vs_primary: str
    activation_conditions: Tuple[str, ...] = field(default_factory=tuple)
    identified_risks: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.activation_conditions, tuple):
            object.__setattr__(self, "activation_conditions", tuple(self.activation_conditions))
        if not isinstance(self.identified_risks, tuple):
            object.__setattr__(self, "identified_risks", tuple(self.identified_risks))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))


@dataclass(frozen=True)
class StructuredRecommendationExplanation:
    """
    Explicación estructurada, determinista y auditable de la recomendación.
    Separa explícitamente las 4 capas epistémicas:
    - OBSERVED: Datos fácticos registrados (precios observados, stock reportado, incidentes).
    - DERIVED: Cálculos matemáticos y normalizaciones (scores, varianzas, percentiles).
    - INFERRED: Deducciones contextuales (tendencias, niveles de riesgo, compatibilidad).
    - RECOMMENDED: Directiva ejecutiva y condiciones requeridas.
    """
    observed_facts: Tuple[str, ...] = field(default_factory=tuple)
    derived_metrics: Tuple[str, ...] = field(default_factory=tuple)
    inferred_signals: Tuple[str, ...] = field(default_factory=tuple)
    recommendation_summary: str = ""
    why_selected: str = ""
    why_over_alternatives: str = ""
    contingency_plan: str = ""

    def __post_init__(self):
        if not isinstance(self.observed_facts, tuple):
            object.__setattr__(self, "observed_facts", tuple(self.observed_facts))
        if not isinstance(self.derived_metrics, tuple):
            object.__setattr__(self, "derived_metrics", tuple(self.derived_metrics))
        if not isinstance(self.inferred_signals, tuple):
            object.__setattr__(self, "inferred_signals", tuple(self.inferred_signals))


@dataclass(frozen=True)
class SupplierRecommendation:
    """
    Modelo final inmutable de recomendación de proveedor para una oportunidad (C.13).
    Cumple con todos los requisitos de Misión C-04:
    - Identificador y oportunidad asociada.
    - Decisión formal y justificación.
    - Proveedor primario y fallback(s).
    - Evidencias estructuradas por dimensión (comercial, riesgo, confiabilidad, logística).
    - Confianza, frescura, procedencia (provenance).
    - Incógnitas (unknowns), condiciones y razones de rechazo.
    """
    recommendation_id: str
    opportunity_id: str
    target_product_title: str
    target_sku: Optional[str]
    decision: SupplierRecommendationDecision
    decision_reason: str
    primary_supplier: Optional[PrimarySupplierSelection]
    fallback_supplier: Optional[FallbackSupplierSelection]
    all_evaluated_candidates: Tuple[SupplierRiskComparisonItem, ...] = field(default_factory=tuple)
    rejected_candidates: Tuple[SupplierRiskComparisonItem, ...] = field(default_factory=tuple)
    conditions: Tuple[RecommendationCondition, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    rejection_reasons: Tuple[str, ...] = field(default_factory=tuple)
    confidence: Confidence = Confidence.UNKNOWN
    freshness: QuoteFreshness = QuoteFreshness.UNKNOWN_FRESHNESS
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE
    explanation: Optional[StructuredRecommendationExplanation] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.all_evaluated_candidates, tuple):
            object.__setattr__(self, "all_evaluated_candidates", tuple(self.all_evaluated_candidates))
        if not isinstance(self.rejected_candidates, tuple):
            object.__setattr__(self, "rejected_candidates", tuple(self.rejected_candidates))
        if not isinstance(self.conditions, tuple):
            object.__setattr__(self, "conditions", tuple(self.conditions))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.rejection_reasons, tuple):
            object.__setattr__(self, "rejection_reasons", tuple(self.rejection_reasons))
