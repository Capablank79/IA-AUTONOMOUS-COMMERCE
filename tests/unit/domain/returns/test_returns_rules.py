from decimal import Decimal
from datetime import datetime, timezone
import pytest

from src.domain.market_intelligence.models import Confidence
from src.domain.mission.models import LoopAction, LoopDecision
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyRuleCategory,
    PolicySeverity,
)
from src.domain.returns.rules import ReturnActionPolicyRule
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel


def test_return_action_policy_rule_issue_refund_valid():
    rule = ReturnActionPolicyRule(max_autonomous_refund_amount=Decimal("100.00"))
    ctx = PolicyEvaluationContext(
        action_type="ISSUE_REFUND",
        actor_id="test_agent",
        mission_id="m_1",
        correlation_id="c_1",
        loop_decision=LoopDecision(action=LoopAction.CONTINUE, reason="test"),
        requested_budget=Decimal("50.00"),
        human_approved=False,
    )
    res = rule.evaluate(ctx)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW


def test_return_action_policy_rule_issue_refund_requires_approval_on_high_amount():
    rule = ReturnActionPolicyRule(max_autonomous_refund_amount=Decimal("100.00"))
    ctx = PolicyEvaluationContext(
        action_type="ISSUE_REFUND",
        actor_id="test_agent",
        mission_id="m_1",
        correlation_id="c_1",
        loop_decision=LoopDecision(action=LoopAction.CONTINUE, reason="test"),
        requested_budget=Decimal("250.00"),
        human_approved=False,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.REQUIRE_APPROVAL
    assert any(v.code == "REFUND_EXCEEDS_AUTONOMOUS_LIMIT" for v in res.violations)


def test_return_action_policy_rule_issue_refund_allowed_if_human_approved():
    rule = ReturnActionPolicyRule(max_autonomous_refund_amount=Decimal("100.00"))
    ctx = PolicyEvaluationContext(
        action_type="ISSUE_REFUND",
        actor_id="test_agent",
        mission_id="m_1",
        correlation_id="c_1",
        loop_decision=LoopDecision(action=LoopAction.CONTINUE, reason="test"),
        requested_budget=Decimal("250.00"),
        human_approved=True,
    )
    res = rule.evaluate(ctx)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW


def test_return_action_policy_rule_reject_return_requires_human():
    rule = ReturnActionPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="REJECT_RETURN",
        actor_id="test_agent",
        mission_id="m_1",
        correlation_id="c_1",
        loop_decision=LoopDecision(action=LoopAction.CONTINUE, reason="test"),
        human_approved=False,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.REQUIRE_APPROVAL
    assert any(v.code == "REJECT_RETURN_REQUIRES_HUMAN" for v in res.violations)
