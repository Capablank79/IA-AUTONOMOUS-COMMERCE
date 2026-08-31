from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Tuple, Mapping, Any
from types import MappingProxyType
from enum import Enum

from src.domain.market_intelligence.models import MarketEvidence, MarketListing, Confidence, SignalType

class OpportunityReadiness(str, Enum):
    """
    Representa el nivel de madurez/disposición de la oportunidad para avanzar en el embudo comercial.
    Transición lógica:
    INSUFFICIENT_EVIDENCE -> NEEDS_INVESTIGATION -> READY -> PROMOTED
    o REJECTED si no es viable.
    """
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"
    READY = "READY"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    # Alias / compatibilidad hacia atrás
    SUFFICIENT_EVIDENCE = "SUFFICIENT_EVIDENCE"
    NOT_VIABLE = "NOT_VIABLE"

class EvidenceSufficiency(str, Enum):
    """
    Evaluación explícita de la suficiencia de la evidencia de mercado disponible.
    """
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"

class RejectionReason(str, Enum):
    """
    Razones de dominio estructuradas para el rechazo de una oportunidad.
    """
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    LOW_SCORE = "LOW_SCORE"
    WEAK_DEMAND = "WEAK_DEMAND"
    EXCESSIVE_COMPETITION = "EXCESSIVE_COMPETITION"
    CONTRADICTORY_SIGNALS = "CONTRADICTORY_SIGNALS"
    HIGH_RISK = "HIGH_RISK"
    INFERIOR_TO_ALTERNATIVES = "INFERIOR_TO_ALTERNATIVES"
    STALE_DATA = "STALE_DATA"
    CRITICAL_UNCERTAINTY = "CRITICAL_UNCERTAINTY"
    OTHER = "OTHER"

@dataclass(frozen=True)
class OpportunityRejection:
    """
    Representa el rechazo explícito y justificado de una oportunidad con trazabilidad de dominio.
    """
    product_id: str
    reason: RejectionReason
    details: str
    confidence: Confidence
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_snapshot: Optional[MarketEvidence] = None

@dataclass(frozen=True)
class OpportunityEvaluationHistoryEntry:
    """
    Registro histórico inmutable de una reevaluación temporal de la oportunidad.
    Permite preservar la evolución sin sobrescribir el estado anterior.
    """
    timestamp: datetime
    previous_score: Optional[Decimal]
    new_score: Optional[Decimal]
    previous_readiness: Optional[OpportunityReadiness]
    new_readiness: OpportunityReadiness
    previous_confidence: Optional[Confidence]
    new_confidence: Confidence
    change_reason: str
    previous_evidence: Optional[MarketEvidence] = None
    evidence_summary: str = ""

    @property
    def reason(self) -> str:
        return self.change_reason

@dataclass(frozen=True)
class OpportunityComparisonDimension:
    """
    Dimensión individual de comparación entre oportunidades.
    """
    dimension_name: str
    winner_id: Optional[str]
    summary: str
    scores_by_candidate: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.scores_by_candidate, MappingProxyType):
            object.__setattr__(self, "scores_by_candidate", MappingProxyType(dict(self.scores_by_candidate)))

    @property
    def rationale(self) -> str:
        return self.summary

    @property
    def scores_by_opportunity(self) -> Mapping[str, Any]:
        return self.scores_by_candidate

@dataclass(frozen=True)
class OpportunityComparisonResult:
    """
    Resultado estructurado y determinista de la comparación entre dos o más oportunidades.
    """
    candidate_ids: Tuple[str, ...]
    best_candidate_id: Optional[str]
    dimensions: Tuple[OpportunityComparisonDimension, ...]
    comparison_summary: str
    why_winner: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.candidate_ids, tuple):
            object.__setattr__(self, "candidate_ids", tuple(self.candidate_ids))
        if not isinstance(self.dimensions, tuple):
            object.__setattr__(self, "dimensions", tuple(self.dimensions))

    @property
    def winner_id(self) -> Optional[str]:
        return self.best_candidate_id

    @property
    def compared_opportunities(self) -> Tuple[str, ...]:
        return self.candidate_ids

    @property
    def summary_rationale(self) -> str:
        return self.comparison_summary

@dataclass(frozen=True)
class OpportunityExplanation:
    """
    Explicación estructurada basada estrictamente en evidencia.
    Distingue OBSERVED, DERIVED, INFERRED, RISKS, UNKNOWNS, RECOMMENDED.
    """
    product_id: str
    title: str
    why_winner: str
    observed_evidence: Tuple[str, ...] = field(default_factory=tuple)
    derived_signals: Tuple[str, ...] = field(default_factory=tuple)
    inferred_insights: Tuple[str, ...] = field(default_factory=tuple)
    risks: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    recommended_action: Optional[str] = None

    def __post_init__(self):
        for field_name in ["observed_evidence", "derived_signals", "inferred_insights", "risks", "unknowns"]:
            val = getattr(self, field_name)
            if not isinstance(val, tuple):
                object.__setattr__(self, field_name, tuple(val))

@dataclass(frozen=True)
class OpportunityDecision:
    """
    Representa la decisión del motor de oportunidades tras evaluar la evidencia.
    """
    evidence: MarketEvidence
    readiness: OpportunityReadiness
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    opportunity_score: Optional[Decimal] = None
    confidence: Confidence = Confidence.UNKNOWN
    evidence_sufficiency: EvidenceSufficiency = EvidenceSufficiency.INSUFFICIENT
    rejection: Optional[OpportunityRejection] = None
    explanation: Optional[OpportunityExplanation] = None

    def __post_init__(self):
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))

@dataclass(frozen=True)
class Opportunity:
    """
    Modelo de Dominio central de una Oportunidad de Negocio comercial.
    Representa explícitamente:
    - identidad
    - producto/listing
    - evidencia
    - señales
    - score
    - confidence
    - evidence sufficiency
    - readiness
    - risks
    - unknowns
    - decision
    - explanation
    - timestamps
    - provenance
    - historial de reevaluaciones
    """
    opportunity_id: str
    product_id: str
    title: str
    listing: MarketListing
    evidence: MarketEvidence
    score: Optional[Decimal]
    confidence: Confidence
    evidence_sufficiency: EvidenceSufficiency
    readiness: OpportunityReadiness
    risks: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    decision: Optional[OpportunityDecision] = None
    explanation: Optional[OpportunityExplanation] = None
    rejection: Optional[OpportunityRejection] = None
    history: Tuple[OpportunityEvaluationHistoryEntry, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.opportunity_id:
            raise ValueError("opportunity_id must be valid")
        if not self.product_id:
            raise ValueError("product_id must be valid")
        if not isinstance(self.risks, tuple):
            object.__setattr__(self, "risks", tuple(self.risks))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        if not isinstance(self.history, tuple):
            object.__setattr__(self, "history", tuple(self.history))
        if not isinstance(self.provenance, MappingProxyType):
            object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

@dataclass(frozen=True)
class BestKnownOpportunity:
    """
    Representa la mejor oportunidad conocida preservada en el estado del loop.
    Reconstruible e inmutable.
    """
    product_id: str
    title: str
    score: Decimal
    confidence: Confidence
    evidence: MarketEvidence
    iteration: int
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    explanation: Optional[OpportunityExplanation] = None

@dataclass(frozen=True)
class OpportunityProgress:
    """
    Métricas de progreso de la misión en el ciclo autónomo.
    """
    previous_best_score: Optional[Decimal] = None
    current_best_score: Optional[Decimal] = None
    improvement: Decimal = Decimal("0.0")
    evidence_coverage: float = 0.0
    search_coverage: int = 0
    uncertainty_level: str = "HIGH"
    iterations_count: int = 0
    external_calls_count: int = 0
    estimated_cost_usd: Optional[Decimal] = None

@dataclass(frozen=True)
class CompletionPolicy:
    """
    Reglas deterministas de validación para aceptar la terminación de una misión.
    """
    min_candidates: int = 1
    min_score: Decimal = Decimal("30.0")
    min_confidence: Tuple[Confidence, ...] = (Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW, Confidence.UNKNOWN)
    allow_unknown_if_insufficient_evidence: bool = True
    require_best_known: bool = True
