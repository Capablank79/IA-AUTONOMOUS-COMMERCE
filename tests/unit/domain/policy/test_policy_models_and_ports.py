import pytest
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

from src.domain.mission.models import LoopDecision, LoopAction, LoopState
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel
from src.domain.capital.models import CapitalBudget
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyRuleCategory,
    PolicySeverity,
    PolicyViolation,
    RuleEvaluationResult,
    PolicyEvaluationContext,
    PolicyEvaluation,
)


def test_policy_violation_immutability():
    violation = PolicyViolation(
        rule_name="TestRule",
        category=PolicyRuleCategory.AUTHORIZATION,
        severity=PolicySeverity.BLOCKING,
        message="Action blocked",
        code="AUTH_01",
        details={"reason": "unauthorized"}
    )
    assert violation.rule_name == "TestRule"
    assert isinstance(violation.details, MappingProxyType)
    with pytest.raises((TypeError, AttributeError)):
        violation.details["reason"] = "modified"


def test_policy_evaluation_context_immutability_and_defaults():
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Testing context",
        parameters={"key": "val"}
    )
    state = LoopState(
        mission_id="m-test",
        iteration=1,
        goal="Test goal"
    )
    context = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="agent-01",
        mission_id="m-test",
        correlation_id="corr-123",
        loop_decision=decision,
        loop_state=state,
        allowed_actions=["PUBLISH", "VERIFY"],
        executed_idempotency_keys=["idemp-1"],
        custom_context={"flag": True}
    )

    assert context.action_type == "PUBLISH"
    assert isinstance(context.allowed_actions, tuple)
    assert isinstance(context.executed_idempotency_keys, tuple)
    assert isinstance(context.custom_context, MappingProxyType)

    # Immutability
    with pytest.raises(Exception):
        context.action_type = "OTHER"


def test_policy_evaluation_immutability():
    decision = LoopDecision(action=LoopAction.CONTINUE, reason="Eval test")
    eval_res = RuleEvaluationResult(
        rule_name="Rule1",
        category=PolicyRuleCategory.AUTHORIZATION,
        passed=True,
        decision_impact=PolicyDecisionType.ALLOW,
        reasons=("Reason 1",),
        violations=()
    )
    evaluation = PolicyEvaluation(
        evaluation_id="EVAL-001",
        decision=PolicyDecisionType.ALLOW,
        action_type="PUBLISH",
        actor_id="agent-01",
        mission_id="m-01",
        correlation_id="corr-01",
        rules_evaluated=("Rule1",),
        rule_results=(eval_res,),
        reasons=("Reason 1",),
        violations=(),
        is_allowed=True,
        requires_approval=False,
        is_unknown=False,
        is_denied=False,
        is_deferred=False,
    )

    assert evaluation.is_allowed is True
    assert evaluation.decision == PolicyDecisionType.ALLOW
    assert isinstance(evaluation.rules_evaluated, tuple)
    assert isinstance(evaluation.rule_results, tuple)
