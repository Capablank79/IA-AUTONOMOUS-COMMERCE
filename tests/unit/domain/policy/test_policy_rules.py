import pytest
from decimal import Decimal
from datetime import datetime, timezone

from src.domain.mission.models import LoopDecision, LoopAction, LoopState
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel, EvidenceProvenanceType
from src.domain.capital.models import CapitalBudget, AllocationDecision, AllocationStatus, AllocationDecisionReason, CapitalDownsideAnalysis
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyRuleCategory,
    PolicySeverity,
    PolicyEvaluationContext,
)
from src.domain.policy.rules import (
    AuthorizationPolicyRule,
    HumanApprovalPolicyRule,
    IdempotencyPolicyRule,
    BudgetAndCapitalPolicyRule,
    RiskPolicyRule,
    DataQualityAndSafetyRule,
)


@pytest.fixture
def base_context():
    decision = LoopDecision(action=LoopAction.CONTINUE, reason="Test rule execution")
    state = LoopState(mission_id="m-rules", iteration=1, goal="Rule tests")
    return PolicyEvaluationContext(
        action_type="EXPLORE",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=decision,
        loop_state=state,
    )


# 1. Authorization Rule Tests
def test_authorization_rule_allow(base_context):
    rule = AuthorizationPolicyRule()
    res = rule.evaluate(base_context)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW


def test_authorization_rule_deny_prohibited(base_context):
    rule = AuthorizationPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="DELETE_ALL",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        prohibited_actions=["DELETE_ALL", "DROP_TABLE"],
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.DENY
    assert len(res.violations) == 1
    assert res.violations[0].code == "AUTH_ACTION_PROHIBITED"


def test_authorization_rule_deny_not_in_allowed_list(base_context):
    rule = AuthorizationPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="TRANSFER_FUNDS",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        allowed_actions=["EXPLORE", "ANALYZE"],
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.DENY
    assert res.violations[0].code == "AUTH_ACTION_NOT_ALLOWED"


# 2. Human Approval Rule Tests
def test_human_approval_rule_allow_when_not_required(base_context):
    rule = HumanApprovalPolicyRule()
    res = rule.evaluate(base_context)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW


def test_human_approval_rule_requires_approval_when_listed(base_context):
    rule = HumanApprovalPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        actions_requiring_approval=["PUBLISH"],
        human_approved=False,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.REQUIRE_APPROVAL
    assert res.violations[0].code == "APPROVAL_REQUIRED"


def test_human_approval_rule_allow_when_previously_approved(base_context):
    rule = HumanApprovalPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        actions_requiring_approval=["PUBLISH"],
        human_approved=True,
    )
    res = rule.evaluate(ctx)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW


def test_human_approval_rule_requires_approval_for_irreversible_action(base_context):
    rule = HumanApprovalPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PAY",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        is_irreversible=True,
        human_approved=False,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.REQUIRE_APPROVAL


# 3. Idempotency Rule Tests
def test_idempotency_rule_allow_new_key(base_context):
    rule = IdempotencyPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        idempotency_key="key_123",
        executed_idempotency_keys=["key_001"],
    )
    res = rule.evaluate(ctx)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW


def test_idempotency_rule_deny_duplicate(base_context):
    rule = IdempotencyPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        idempotency_key="key_duplicate",
        executed_idempotency_keys=["key_duplicate"],
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.DENY
    assert res.violations[0].code == "IDEMPOTENCY_DUPLICATE_ACTION"


def test_idempotency_rule_defer_in_flight(base_context):
    rule = IdempotencyPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        idempotency_key="key_inflight",
        in_flight_idempotency_keys=["key_inflight"],
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.DEFER
    assert res.violations[0].code == "IDEMPOTENCY_IN_FLIGHT"


def test_idempotency_rule_unknown_when_missing_for_external_impact(base_context):
    rule = IdempotencyPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        is_external_impact=True,
        idempotency_key=None,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.UNKNOWN
    assert res.violations[0].code == "IDEMPOTENCY_KEY_MISSING"


# 4. Budget and Capital Rule Tests
def test_budget_rule_allow_within_limit(base_context):
    rule = BudgetAndCapitalPolicyRule()
    budget = CapitalBudget(
        budget_id="b-test-1",
        total_capital=Decimal("1000000"),
        reserved_capital=Decimal("200000"),
        committed_capital=Decimal("100000"),
        currency="CLP"
    )
    ctx = PolicyEvaluationContext(
        action_type="INVEST",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        requested_budget=Decimal("50000"),
        capital_budget=budget,
    )
    res = rule.evaluate(ctx)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW


def test_budget_rule_deny_budget_exceeded(base_context):
    rule = BudgetAndCapitalPolicyRule()
    budget = CapitalBudget(
        budget_id="b-test-2",
        total_capital=Decimal("100000"),
        reserved_capital=Decimal("50000"),
        committed_capital=Decimal("40000"),
        currency="CLP"
    )  # allocatable = 10000
    ctx = PolicyEvaluationContext(
        action_type="INVEST",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        requested_budget=Decimal("25000"),
        capital_budget=budget,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.DENY
    assert res.violations[0].code == "BUDGET_EXCEEDED"


def test_budget_rule_unknown_when_budget_missing(base_context):
    rule = BudgetAndCapitalPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="INVEST",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        requested_budget=Decimal("25000"),
        capital_budget=None,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.UNKNOWN
    assert res.violations[0].code == "BUDGET_UNKNOWN"


def test_budget_rule_deny_when_capital_engine_rejected(base_context):
    rule = BudgetAndCapitalPolicyRule()
    alloc_decision = AllocationDecision(
        decision_id="DEC-01",
        opportunity_id="opp-test",
        supplier_id="sup-test",
        status=AllocationStatus.REJECTED,
        reason=AllocationDecisionReason.NEGATIVE_OR_INSUFFICIENT_MARGIN,
        requested_capital=Decimal("100000"),
        approved_capital=Decimal("0"),
        unapproved_capital=Decimal("100000"),
        maximum_allowed_exposure=Decimal("50000"),
        available_allocatable_capital=Decimal("50000"),
        remaining_allocatable_capital=Decimal("50000"),
        currency="CLP",
        allocation_ratio=Decimal("0"),
        profit_score=None,
        risk_score=None,
        opportunity_score=None,
        allocation_score=None,
        expected_profit=None,
        expected_margin_pct=None,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        downside_analysis=CapitalDownsideAnalysis(capital_at_risk=Decimal("100000")),
    )
    ctx = PolicyEvaluationContext(
        action_type="ALLOCATE",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        capital_allocation_decision=alloc_decision,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.DENY
    assert res.violations[0].code == "CAPITAL_ALLOCATION_REJECTED"


# 5. Risk Rule Tests
def test_risk_rule_allow_low_moderate(base_context):
    rule = RiskPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        risk_level=RiskLevel.LOW,
    )
    res = rule.evaluate(ctx)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW


def test_risk_rule_deny_critical(base_context):
    rule = RiskPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        risk_level=RiskLevel.CRITICAL,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.DENY
    assert res.violations[0].code == "RISK_CRITICAL_BLOCKED"


def test_risk_rule_unknown_when_missing_for_external_impact(base_context):
    rule = RiskPolicyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        is_external_impact=True,
        risk_level=None,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.UNKNOWN
    assert res.violations[0].code == "RISK_UNKNOWN"


# 6. Data Quality & Safety Rule Tests
def test_data_quality_rule_blocks_mock_for_live_impact(base_context):
    rule = DataQualityAndSafetyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        is_external_impact=True,
        provenance=EvidenceProvenanceType.MOCK,
    )
    res = rule.evaluate(ctx)
    assert res.passed is False
    assert res.decision_impact == PolicyDecisionType.DENY
    assert res.violations[0].code == "SYNTHETIC_DATA_BLOCKED_FOR_LIVE_ACTION"


def test_data_quality_rule_allows_live_provenance(base_context):
    rule = DataQualityAndSafetyRule()
    ctx = PolicyEvaluationContext(
        action_type="PUBLISH",
        actor_id="test_agent",
        mission_id="m-rules",
        correlation_id="corr-test",
        loop_decision=base_context.loop_decision,
        is_external_impact=True,
        provenance=EvidenceProvenanceType.LIVE,
    )
    res = rule.evaluate(ctx)
    assert res.passed is True
    assert res.decision_impact == PolicyDecisionType.ALLOW
