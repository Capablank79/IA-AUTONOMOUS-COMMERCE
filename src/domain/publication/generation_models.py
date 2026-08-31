from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple, Mapping, Any, Sequence, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence, MarketEvidence
from src.domain.publication.models import SalesChannel, SalesChannelType, ListingDraft


class KeywordSourceType(str, Enum):
    """
    Origen y procedencia de la palabra clave SEO.
    Distingue palabras observadas directamente en el mercado de aquellas derivadas o propuestas.
    """
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    PROPOSED = "PROPOSED"


@dataclass(frozen=True)
class SEOKeyword:
    """
    Representa una palabra clave SEO con procedencia, score de relevancia y volumen observado.
    """
    keyword: str
    source_type: KeywordSourceType
    relevance_score: float = 1.0
    search_volume_observed: Optional[int] = None
    provenance_id: Optional[str] = None

    def __post_init__(self):
        if not self.keyword or not self.keyword.strip():
            raise ValueError("SEOKeyword keyword cannot be empty")
        if not (0.0 <= self.relevance_score <= 1.0):
            raise ValueError("SEOKeyword relevance_score must be between 0.0 and 1.0")
        if self.search_volume_observed is not None and self.search_volume_observed < 0:
            raise ValueError("search_volume_observed cannot be negative")


@dataclass(frozen=True)
class SEOStrategy:
    """
    Estrategia de optimización para motores de búsqueda basada en evidencia.
    """
    primary_keywords: Tuple[SEOKeyword, ...] = field(default_factory=tuple)
    secondary_keywords: Tuple[SEOKeyword, ...] = field(default_factory=tuple)
    search_terms: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.primary_keywords, tuple):
            object.__setattr__(self, "primary_keywords", tuple(self.primary_keywords))
        if not isinstance(self.secondary_keywords, tuple):
            object.__setattr__(self, "secondary_keywords", tuple(self.secondary_keywords))
        if not isinstance(self.search_terms, tuple):
            object.__setattr__(self, "search_terms", tuple(self.search_terms))


class CustomerPainCategory(str, Enum):
    """
    Categorías taxonómicas para la clasificación de quejas e insatisfacciones de clientes.
    """
    FUNCTIONAL = "FUNCTIONAL"
    QUALITY = "QUALITY"
    PERFORMANCE = "PERFORMANCE"
    DURABILITY = "DURABILITY"
    USABILITY = "USABILITY"
    SIZE = "SIZE"
    BATTERY = "BATTERY"
    MATERIAL = "MATERIAL"
    SHIPPING = "SHIPPING"
    PACKAGING = "PACKAGING"
    PRICE_VALUE = "PRICE_VALUE"
    MISSING_FEATURE = "MISSING_FEATURE"
    COMPATIBILITY = "COMPATIBILITY"
    OTHER = "OTHER"


@dataclass(frozen=True)
class CustomerPainPoint:
    """
    Punto de dolor o insatisfacción recurrente detectado en reseñas del mercado.
    """
    pain_id: str
    category: CustomerPainCategory
    complaint_summary: str
    frequency: str = "FREQUENT"
    severity: int = 5
    evidence_count: int = 1
    source_review_ids: Tuple[str, ...] = field(default_factory=tuple)
    confidence: Confidence = Confidence.MEDIUM

    def __post_init__(self):
        if not self.pain_id or not self.pain_id.strip():
            raise ValueError("pain_id cannot be empty")
        if not self.complaint_summary or not self.complaint_summary.strip():
            raise ValueError("complaint_summary cannot be empty")
        if not (1 <= self.severity <= 10):
            raise ValueError("severity must be between 1 and 10")
        if self.evidence_count < 0:
            raise ValueError("evidence_count cannot be negative")
        if not isinstance(self.source_review_ids, tuple):
            object.__setattr__(self, "source_review_ids", tuple(self.source_review_ids))


@dataclass(frozen=True)
class UnmetNeed:
    """
    Necesidad subyacente no satisfecha derivada de múltiples quejas.
    """
    need_id: str
    description: str
    related_pain_ids: Tuple[str, ...] = field(default_factory=tuple)
    confidence: Confidence = Confidence.MEDIUM

    def __post_init__(self):
        if not self.need_id or not self.need_id.strip():
            raise ValueError("need_id cannot be empty")
        if not self.description or not self.description.strip():
            raise ValueError("description cannot be empty")
        if not isinstance(self.related_pain_ids, tuple):
            object.__setattr__(self, "related_pain_ids", tuple(self.related_pain_ids))


@dataclass(frozen=True)
class DifferentiationStrategy:
    """
    Estrategia de posicionamiento diferencial sustentada por evidencia del producto.
    """
    unmet_needs_addressed: Tuple[UnmetNeed, ...] = field(default_factory=tuple)
    differential_claims: Tuple[str, ...] = field(default_factory=tuple)
    evidence_backed: bool = True
    product_truth_mapping: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.unmet_needs_addressed, tuple):
            object.__setattr__(self, "unmet_needs_addressed", tuple(self.unmet_needs_addressed))
        if not isinstance(self.differential_claims, tuple):
            object.__setattr__(self, "differential_claims", tuple(self.differential_claims))
        if not isinstance(self.product_truth_mapping, MappingProxyType):
            object.__setattr__(self, "product_truth_mapping", MappingProxyType(dict(self.product_truth_mapping)))


class ClaimProvenanceType(str, Enum):
    """
    Tipo de procedencia de cada afirmación o claim generado.
    """
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ClaimProvenance:
    """
    Trazabilidad y respaldo evidencial de una afirmación comercial específica.
    """
    claim_text: str
    provenance_type: ClaimProvenanceType
    source_field: Optional[str] = None
    source_evidence_id: Optional[str] = None
    confidence: Confidence = Confidence.MEDIUM

    def __post_init__(self):
        if not self.claim_text or not self.claim_text.strip():
            raise ValueError("claim_text cannot be empty")


@dataclass(frozen=True)
class ListingFactGrounding:
    """
    Reporte de fundamentación factual del contenido generado.
    Registra atributos verificados, beneficios inferidos y claims omitidos por falta de respaldo.
    """
    verified_attributes: Mapping[str, Any] = field(default_factory=dict)
    inferred_benefits: Tuple[str, ...] = field(default_factory=tuple)
    unsupported_claims_omitted: Tuple[str, ...] = field(default_factory=tuple)
    claims_provenance: Tuple[ClaimProvenance, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.verified_attributes, MappingProxyType):
            object.__setattr__(self, "verified_attributes", MappingProxyType(dict(self.verified_attributes)))
        if not isinstance(self.inferred_benefits, tuple):
            object.__setattr__(self, "inferred_benefits", tuple(self.inferred_benefits))
        if not isinstance(self.unsupported_claims_omitted, tuple):
            object.__setattr__(self, "unsupported_claims_omitted", tuple(self.unsupported_claims_omitted))
        if not isinstance(self.claims_provenance, tuple):
            object.__setattr__(self, "claims_provenance", tuple(self.claims_provenance))

    @property
    def has_unsupported_claims(self) -> bool:
        return len(self.unsupported_claims_omitted) > 0


@dataclass(frozen=True)
class ChannelContentConstraint:
    """
    Restricciones estructurales y de contenido impuestas por un canal de venta.
    """
    max_title_length: int = 60
    max_description_length: int = 5000
    allows_html: bool = False
    allows_bullets: bool = True
    max_bullets: int = 5
    max_bullet_length: int = 150
    required_attributes: Tuple[str, ...] = field(default_factory=tuple)
    forbidden_terms: Tuple[str, ...] = (
        "100% garantizado",
        "el mejor",
        "el numero 1",
        "el número 1",
        "incomparable",
        "milagroso",
        "gratis de por vida",
    )

    def __post_init__(self):
        if self.max_title_length <= 0:
            raise ValueError("max_title_length must be positive")
        if self.max_description_length <= 0:
            raise ValueError("max_description_length must be positive")
        if not isinstance(self.required_attributes, tuple):
            object.__setattr__(self, "required_attributes", tuple(self.required_attributes))
        if not isinstance(self.forbidden_terms, tuple):
            object.__setattr__(self, "forbidden_terms", tuple(self.forbidden_terms))


@dataclass(frozen=True)
class MultichannelContent:
    """
    Representación adaptada del contenido para un canal comercial específico.
    """
    channel_type: SalesChannelType
    channel_id: str
    title: str
    body: str
    bullets: Tuple[str, ...] = field(default_factory=tuple)
    tags_or_keywords: Tuple[str, ...] = field(default_factory=tuple)
    call_to_action: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.channel_id or not self.channel_id.strip():
            raise ValueError("channel_id cannot be empty")
        if not self.title or not self.title.strip():
            raise ValueError("title cannot be empty")
        if not self.body or not self.body.strip():
            raise ValueError("body cannot be empty")
        if not isinstance(self.bullets, tuple):
            object.__setattr__(self, "bullets", tuple(self.bullets))
        if not isinstance(self.tags_or_keywords, tuple):
            object.__setattr__(self, "tags_or_keywords", tuple(self.tags_or_keywords))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ListingGenerationInput:
    """
    Entrada tipada e inmutable para la generación comercial de un listing.
    Agrupa datos de producto, evidencia de mercado, señales de dolor, SEO y restricciones de canal.
    """
    product_id: str
    title: str
    price: Decimal
    currency: str
    available_quantity: int
    channel: SalesChannel
    brand: Optional[str] = None
    model: Optional[str] = None
    category_id: Optional[str] = None
    sku: Optional[str] = None
    condition: str = "new"
    attributes: Mapping[str, Any] = field(default_factory=dict)
    images: Tuple[str, ...] = field(default_factory=tuple)
    market_evidence: Optional[MarketEvidence] = None
    customer_pains: Tuple[CustomerPainPoint, ...] = field(default_factory=tuple)
    seo_keywords: Tuple[SEOKeyword, ...] = field(default_factory=tuple)
    constraints: Optional[ChannelContentConstraint] = None
    locale: str = "es-CL"
    supplier_context: Mapping[str, Any] = field(default_factory=dict)
    economics_context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.product_id or not self.product_id.strip():
            raise ValueError("product_id cannot be empty")
        if not self.title or not self.title.strip():
            raise ValueError("title cannot be empty")
        if self.price <= Decimal("0"):
            raise ValueError("price must be greater than zero")
        if not self.currency or not self.currency.strip():
            raise ValueError("currency cannot be empty")
        if self.available_quantity < 0:
            raise ValueError("available_quantity cannot be negative")
        if not isinstance(self.attributes, MappingProxyType):
            object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        if not isinstance(self.images, tuple):
            object.__setattr__(self, "images", tuple(self.images))
        if not isinstance(self.customer_pains, tuple):
            object.__setattr__(self, "customer_pains", tuple(self.customer_pains))
        if not isinstance(self.seo_keywords, tuple):
            object.__setattr__(self, "seo_keywords", tuple(self.seo_keywords))
        if not isinstance(self.supplier_context, MappingProxyType):
            object.__setattr__(self, "supplier_context", MappingProxyType(dict(self.supplier_context)))
        if not isinstance(self.economics_context, MappingProxyType):
            object.__setattr__(self, "economics_context", MappingProxyType(dict(self.economics_context)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ListingGenerationResult:
    """
    Resultado formal e inmutable de la generación de un listing comercial.
    Contiene el ListingDraft listo para validación por G.2, además de la fundamentación factual,
    estrategia SEO, diferenciación y variantes multicanal.
    """
    draft: ListingDraft
    grounding: ListingFactGrounding
    seo_strategy: SEOStrategy
    differentiation_strategy: DifferentiationStrategy
    multichannel_variants: Tuple[MultichannelContent, ...] = field(default_factory=tuple)
    confidence: Confidence = Confidence.HIGH
    generation_metadata: Mapping[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.multichannel_variants, tuple):
            object.__setattr__(self, "multichannel_variants", tuple(self.multichannel_variants))
        if not isinstance(self.generation_metadata, MappingProxyType):
            object.__setattr__(self, "generation_metadata", MappingProxyType(dict(self.generation_metadata)))
