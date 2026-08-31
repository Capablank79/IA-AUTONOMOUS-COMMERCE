import pytest
from decimal import Decimal
from typing import Sequence, Optional
from types import MappingProxyType

from src.domain.mission.models import LoopDecision, LoopAction, LoopState
from src.domain.supplier_intelligence.models import RiskLevel, EvidenceProvenanceType
from src.domain.capital.models import CapitalBudget
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyEvaluation,
)
from src.domain.policy.ports import PolicyAuditRepository
from src.domain.policy.engine import PolicyEngine


class InMemoryPolicyAuditRepository(PolicyAuditRepository):
    def __init__(self):
        self.evaluations = {}

    def save_evaluation(self, evaluation: PolicyEvaluation) -> None:
        self.evaluations[evaluation.evaluation_id] = evaluation

    def get_by_id(self, evaluation_id: str) -> Optional[PolicyEvaluation]:
        return self.evaluations.get(evaluation_id)

    def get_by_correlation_id(self, correlation_id: str) -> Sequence[PolicyEvaluation]:
        return [e for e in self.evaluations.values() if e.correlation_id == correlation_id]


@pytest.fixture
def policy_engine():
    repo = InMemoryPolicyAuditRepository()
    engine = PolicyEngine(audit_repository=repo)
    return engine, repo


def test_policy_engine_allow_scenario(policy_engine):
    engine, repo = policy_engine
    decision = LoopDecision(action=LoopAction.CONTINUE, reason="Safe action")
    state = LoopState(mission_id="m-allow", iteration=1, goal="Test allow")
    context = PolicyEvaluationContext(
        action_type="SEARCH_PRODUCTS",
        actor_id="agent-01",
        mission_id="m-allow",
        correlation_id="corr-allow",
        loop_decision=decision,
        loop_state=state,
        risk_level=RiskLevel.LOW,
        provenance=EvidenceProvenanceType.LIVE,
    )

    evaluation = engine.evaluate(context)

    assert evaluation.decision == PolicyDecisionType.ALLOW
    assert evaluation.is_allowed is True
    assert evaluation.is_denied is False
    assert len(evaluation.violations) == 0
    # Verifica guardado en auditoría
    assert repo.get_by_id(evaluation.evaluation_id) is not None
    assert len(repo.get_by_correlation_id("corr-allow")) == 1


def test_policy_engine_hierarchy_deny_overrides_approval_and_unknown(policy_engine):
    engine, repo = policy_engine
    decision = LoopDecision(action=LoopAction.CONTINUE, reason="Conflicted action")
    state = LoopState(mission_id="m-hierarchy", iteration=1, goal="Test hierarchy")
    
    # Contexto con múltiples conflictos:
    # 1. Prohibida -> DENY
    # 2. Requiere aprobación -> REQUIRE_APPROVAL
    # 3. Budget desconocido -> UNKNOWN
    context = PolicyEvaluationContext(
        action_type="FORBIDDEN_ACTION",
        actor_id="agent-01",
        mission_id="m-hierarchy",
        correlation_id="corr-hierarchy",
        loop_decision=decision,
        loop_state=state,
        prohibited_actions=["FORBIDDEN_ACTION"],
        actions_requiring_approval=["FORBIDDEN_ACTION"],
        requested_budget=Decimal("5000"),
        capital_budget=None,  # causaría UNKNOWN en budget
    )

    evaluation = engine.evaluate(context)

    # DENY tiene la máxima prioridad en la jerarquía
    assert evaluation.decision == PolicyDecisionType.DENY
    assert evaluation.is_denied is True
    assert evaluation.is_allowed is False


def test_policy_engine_require_approval_scenario(policy_engine):
    engine, repo = policy_engine
    decision = LoopDecision(action=LoopAction.CONTINUE, reason="Publish action")
    state = LoopState(mission_id="m-appr", iteration=1, goal="Test approval")
    context = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="agent-01",
        mission_id="m-appr",
        correlation_id="corr-appr",
        loop_decision=decision,
        loop_state=state,
        actions_requiring_approval=["PUBLISH"],
        human_approved=False,
        risk_level=RiskLevel.LOW,
        provenance=EvidenceProvenanceType.LIVE,
    )

    evaluation = engine.evaluate(context)

    assert evaluation.decision == PolicyDecisionType.REQUIRE_APPROVAL
    assert evaluation.requires_approval is True
    assert evaluation.is_allowed is False


def test_policy_engine_unknown_scenario(policy_engine):
    engine, repo = policy_engine
    decision = LoopDecision(action=LoopAction.CONTINUE, reason="High impact without risk score")
    state = LoopState(mission_id="m-unk", iteration=1, goal="Test unknown")
    context = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="agent-01",
        mission_id="m-unk",
        correlation_id="corr-unk",
        loop_decision=decision,
        loop_state=state,
        is_external_impact=True,
        risk_level=None,  # Desconocido
        idempotency_key="idemp_123",
        provenance=EvidenceProvenanceType.LIVE,
    )

    evaluation = engine.evaluate(context)

    assert evaluation.decision == PolicyDecisionType.UNKNOWN
    assert evaluation.is_unknown is True
    assert evaluation.is_allowed is False
    assert len(evaluation.evidence_unknowns) > 0
