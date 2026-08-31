from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Tuple, Mapping, Any, Dict
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel
from src.domain.profit.models import Money
from src.domain.capital.models import CapitalBudget, CapitalExposure, AllocationDecision
from src.domain.mission.models import LoopDecision, LoopState, LoopAction


class PolicyDecisionType(str, Enum):
    """
    Decisión determinista resultante de la evaluación de políticas de gobernanza.
    """
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DEFER = "DEFER"
    UNKNOWN = "UNKNOWN"


class PolicyRuleCategory(str, Enum):
    """
    Categorías de reglas de política ordenadas por severidad y jerarquía de seguridad.
    """
    SAFETY = "SAFETY"
    AUTHORIZATION = "AUTHORIZATION"
    IDEMPOTENCY = "IDEMPOTENCY"
    BUDGET = "BUDGET"
    RISK = "RISK"
    APPROVAL = "APPROVAL"
    DATA_QUALITY = "DATA_QUALITY"
    BUSINESS_RULE = "BUSINESS_RULE"


class PolicySeverity(str, Enum):
    """
    Severidad de una violación de política o condición.
    """
    BLOCKING = "BLOCKING"      # Produce DENY
    REQUIRES_HUMAN = "REQUIRES_HUMAN"  # Produce REQUIRE_APPROVAL
    UNCERTAIN = "UNCERTAIN"    # Produce UNKNOWN o DEFER
    WARNING = "WARNING"        # Informativo, no bloquea


@dataclass(frozen=True)
class PolicyViolation:
    """
    Representa una violación o condición detectada por una regla de política.
    """
    rule_name: str
    category: PolicyRuleCategory
    severity: PolicySeverity
    message: str
    code: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class PolicyEvaluationContext:
    """
    Contexto de entrada estructurado para la evaluación de políticas.
    
    Aislamiento y Seguridad:
    - NO contiene objetos HTTP, SDKs externos, ni credenciales.
    - Preserva correlation_id, idempotency_key, request_id, mission_id.
    - Contiene estado del agente/misión, acción propuesta, presupuesto, riesgo, autorizaciones.
    """
    action_type: str
    actor_id: str
    mission_id: str
    correlation_id: str
    loop_decision: LoopDecision
    loop_state: Optional[LoopState] = None
    idempotency_key: Optional[str] = None
    request_id: Optional[str] = None
    target_resource: Optional[str] = None
    channel: Optional[str] = None
    requested_budget: Optional[Decimal] = None
    capital_budget: Optional[CapitalBudget] = None
    capital_allocation_decision: Optional[AllocationDecision] = None
    risk_level: Optional[RiskLevel] = None
    confidence: Optional[Confidence] = None
    provenance: Optional[EvidenceProvenanceType] = None
    is_external_impact: bool = False
    is_irreversible: bool = False
    human_approved: bool = False
    executed_idempotency_keys: Tuple[str, ...] = field(default_factory=tuple)
    in_flight_idempotency_keys: Tuple[str, ...] = field(default_factory=tuple)
    allowed_actions: Tuple[str, ...] = field(default_factory=tuple)
    prohibited_actions: Tuple[str, ...] = field(default_factory=tuple)
    actions_requiring_approval: Tuple[str, ...] = field(default_factory=tuple)
    custom_context: Mapping[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.executed_idempotency_keys, tuple):
            object.__setattr__(self, "executed_idempotency_keys", tuple(self.executed_idempotency_keys))
        if not isinstance(self.in_flight_idempotency_keys, tuple):
            object.__setattr__(self, "in_flight_idempotency_keys", tuple(self.in_flight_idempotency_keys))
        if not isinstance(self.allowed_actions, tuple):
            object.__setattr__(self, "allowed_actions", tuple(self.allowed_actions))
        if not isinstance(self.prohibited_actions, tuple):
            object.__setattr__(self, "prohibited_actions", tuple(self.prohibited_actions))
        if not isinstance(self.actions_requiring_approval, tuple):
            object.__setattr__(self, "actions_requiring_approval", tuple(self.actions_requiring_approval))
        if not isinstance(self.custom_context, MappingProxyType):
            object.__setattr__(self, "custom_context", MappingProxyType(dict(self.custom_context)))


@dataclass(frozen=True)
class RuleEvaluationResult:
    """
    Resultado individual de evaluar una regla de política específica.
    """
    rule_name: str
    category: PolicyRuleCategory
    passed: bool
    decision_impact: PolicyDecisionType
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    violations: Tuple[PolicyViolation, ...] = field(default_factory=tuple)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))
        if not isinstance(self.violations, tuple):
            object.__setattr__(self, "violations", tuple(self.violations))


@dataclass(frozen=True)
class PolicyEvaluation:
    """
    Resultado global de la evaluación de políticas de gobernanza.
    Inmutable, auditable y con trazabilidad completa.
    """
    evaluation_id: str
    decision: PolicyDecisionType
    action_type: str
    actor_id: str
    mission_id: str
    correlation_id: str
    rules_evaluated: Tuple[str, ...]
    rule_results: Tuple[RuleEvaluationResult, ...]
    reasons: Tuple[str, ...]
    violations: Tuple[PolicyViolation, ...]
    is_allowed: bool
    requires_approval: bool
    is_unknown: bool
    is_denied: bool
    is_deferred: bool
    budget_impact: Optional[Decimal] = None
    risk_level: Optional[RiskLevel] = None
    idempotency_key: Optional[str] = None
    evidence_unknowns: Tuple[str, ...] = field(default_factory=tuple)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.rules_evaluated, tuple):
            object.__setattr__(self, "rules_evaluated", tuple(self.rules_evaluated))
        if not isinstance(self.rule_results, tuple):
            object.__setattr__(self, "rule_results", tuple(self.rule_results))
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))
        if not isinstance(self.violations, tuple):
            object.__setattr__(self, "violations", tuple(self.violations))
        if not isinstance(self.evidence_unknowns, tuple):
            object.__setattr__(self, "evidence_unknowns", tuple(self.evidence_unknowns))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
