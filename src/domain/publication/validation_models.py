from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple, Mapping, Any, Sequence, List, Dict
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence, MarketEvidence
from src.domain.publication.models import SalesChannel, SalesChannelType, ListingDraft
from src.domain.publication.generation_models import (
    ListingFactGrounding,
    ChannelContentConstraint,
    ClaimProvenance,
    SEOStrategy,
    DifferentiationStrategy,
)


class ValidationStatus(str, Enum):
    """
    Estado formal de validación de un ListingDraft (G.2).
    - VALID: El listing cumple todas las políticas, restricciones de canal y fundamentación factual.
    - NEEDS_REVIEW: El listing contiene advertencias o incertidumbres menores que requieren revisión humana.
    - INVALID: El listing incumple especificaciones formales o campos requeridos corregibles.
    - BLOCKED: El listing contiene violaciones críticas de seguridad, falsedad factual o claims prohibidos.
    """
    VALID = "VALID"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    INVALID = "INVALID"
    BLOCKED = "BLOCKED"


class FindingSeverity(str, Enum):
    """
    Severidad jerárquica de un hallazgo o violación durante la validación de un listing.
    """
    INFO = "INFO"            # Informativo o recomendación
    WARNING = "WARNING"      # Advertencia no bloqueante
    ERROR = "ERROR"          # Error formal (produce INVALID)
    BLOCKER = "BLOCKER"      # Violación crítica de seguridad/factualidad (produce BLOCKED)


class ValidationDimension(str, Enum):
    """
    Dimensiones taxonómicas evaluadas por el Listing Quality & Policy Validator.
    """
    REQUIRED_FIELDS = "REQUIRED_FIELDS"
    FIELD_TYPES = "FIELD_TYPES"
    TITLE_CONSTRAINTS = "TITLE_CONSTRAINTS"
    DESCRIPTION_CONSTRAINTS = "DESCRIPTION_CONSTRAINTS"
    ATTRIBUTE_COMPLETENESS = "ATTRIBUTE_COMPLETENESS"
    CATEGORY_COMPATIBILITY = "CATEGORY_COMPATIBILITY"
    PRICE_VALIDITY = "PRICE_VALIDITY"
    INVENTORY_VALIDITY = "INVENTORY_VALIDITY"
    IMAGE_CONSTRAINTS = "IMAGE_CONSTRAINTS"
    KEYWORD_QUALITY = "KEYWORD_QUALITY"
    KEYWORD_STUFFING = "KEYWORD_STUFFING"
    UNSUPPORTED_CLAIMS = "UNSUPPORTED_CLAIMS"
    PROHIBITED_CLAIMS = "PROHIBITED_CLAIMS"
    PRODUCT_TRUTH_GROUNDING = "PRODUCT_TRUTH_GROUNDING"
    CUSTOMER_PAIN_DIFFERENTIATION = "CUSTOMER_PAIN_DIFFERENTIATION"
    DUPLICATE_CONTENT = "DUPLICATE_CONTENT"
    CHANNEL_SPECIFIC_CONSTRAINTS = "CHANNEL_SPECIFIC_CONSTRAINTS"
    PROVENANCE_COMPLETENESS = "PROVENANCE_COMPLETENESS"
    CONFIDENCE_REQUIREMENTS = "CONFIDENCE_REQUIREMENTS"
    CRITICAL_UNKNOWN = "CRITICAL_UNKNOWN"


@dataclass(frozen=True)
class ValidationFinding:
    """
    Hallazgo estructurado e inmutable detectado durante la validación del listing.
    """
    dimension: ValidationDimension
    severity: FindingSeverity
    code: str
    message: str
    field_name: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    suggested_action: Optional[str] = None

    def __post_init__(self):
        if not self.code or not self.code.strip():
            raise ValueError("ValidationFinding code cannot be empty")
        if not self.message or not self.message.strip():
            raise ValueError("ValidationFinding message cannot be empty")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class QualityScoreBreakdown:
    """
    Desglose estructurado y explicable del Quality Score de un listing (0.0 - 100.0).
    Totalmente independiente de Opportunity Score, Risk Score y Confidence.
    """
    completeness_score: float
    factuality_score: float
    seo_score: float
    readability_score: float
    policy_compliance_score: float
    differentiation_score: float
    channel_compliance_score: float
    overall_score: float

    def __post_init__(self):
        scores = [
            self.completeness_score,
            self.factuality_score,
            self.seo_score,
            self.readability_score,
            self.policy_compliance_score,
            self.differentiation_score,
            self.channel_compliance_score,
            self.overall_score,
        ]
        for s in scores:
            if not (0.0 <= s <= 100.0):
                raise ValueError(f"Quality score components must be between 0.0 and 100.0 (got {s})")


@dataclass(frozen=True)
class ListingValidationContext:
    """
    Contexto de entrada formal e inmutable para la validación de un listing.
    Reúne el borrador generado, verdades del producto, evidencia de mercado y reglas de canal.
    """
    draft: ListingDraft
    product_truth_attributes: Mapping[str, Any] = field(default_factory=dict)
    grounding: Optional[ListingFactGrounding] = None
    market_evidence: Optional[MarketEvidence] = None
    seo_strategy: Optional[SEOStrategy] = None
    differentiation_strategy: Optional[DifferentiationStrategy] = None
    channel_constraints: Optional[ChannelContentConstraint] = None
    existing_catalog_titles: Tuple[str, ...] = field(default_factory=tuple)
    min_confidence: Confidence = Confidence.LOW
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.product_truth_attributes, MappingProxyType):
            object.__setattr__(self, "product_truth_attributes", MappingProxyType(dict(self.product_truth_attributes)))
        if not isinstance(self.existing_catalog_titles, tuple):
            object.__setattr__(self, "existing_catalog_titles", tuple(self.existing_catalog_titles))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ListingValidationResult:
    """
    Resultado formal, estructurado e inmutable de la validación de un ListingDraft (G.2).
    """
    draft_id: str
    channel_id: str
    status: ValidationStatus
    is_valid: bool
    quality_score: QualityScoreBreakdown
    findings: Tuple[ValidationFinding, ...] = field(default_factory=tuple)
    violations: Tuple[ValidationFinding, ...] = field(default_factory=tuple)
    warnings: Tuple[ValidationFinding, ...] = field(default_factory=tuple)
    unsupported_claims: Tuple[str, ...] = field(default_factory=tuple)
    missing_fields: Tuple[str, ...] = field(default_factory=tuple)
    confidence: Confidence = Confidence.HIGH
    validator_version: str = "v1.0.0"
    validated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.draft_id or not self.draft_id.strip():
            raise ValueError("draft_id cannot be empty")
        if not self.channel_id or not self.channel_id.strip():
            raise ValueError("channel_id cannot be empty")
        if not isinstance(self.findings, tuple):
            object.__setattr__(self, "findings", tuple(self.findings))
        if not isinstance(self.violations, tuple):
            object.__setattr__(self, "violations", tuple(self.violations))
        if not isinstance(self.warnings, tuple):
            object.__setattr__(self, "warnings", tuple(self.warnings))
        if not isinstance(self.unsupported_claims, tuple):
            object.__setattr__(self, "unsupported_claims", tuple(self.unsupported_claims))
        if not isinstance(self.missing_fields, tuple):
            object.__setattr__(self, "missing_fields", tuple(self.missing_fields))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def is_blocked(self) -> bool:
        return self.status == ValidationStatus.BLOCKED

    @property
    def is_invalid(self) -> bool:
        return self.status == ValidationStatus.INVALID

    @property
    def needs_review(self) -> bool:
        return self.status == ValidationStatus.NEEDS_REVIEW

    @property
    def has_blockers(self) -> bool:
        return any(f.severity == FindingSeverity.BLOCKER for f in self.findings)
