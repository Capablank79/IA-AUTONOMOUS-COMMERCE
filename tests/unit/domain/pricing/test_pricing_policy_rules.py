from decimal import Decimal
import pytest
from src.domain.mission.models import LoopDecision, LoopAction
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import (
    PolicyEvaluationContext,
    PolicyDecisionType,
)
from src.domain.policy.rules import (
    PriceFloorPolicyRule,
    MarginProtectionPolicyRule,
    MaxPriceChangePolicyRule,
)
from src.domain.pricing.models import (
    PricingDecision,
    PriceChangeReason,
)
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.supplier_intelligence.models import RiskLevel
from src.domain.market_intelligence.models import Confidence


def _dummy_loop_decision(action: LoopAction = LoopAction.CONTINUE):
    return LoopDecision(
        action=action,
        reason="test",
        parameters={},
    )


def test_price_floor_policy_rule_allow_when_above_floor():
    rule = PriceFloorPolicyRule()
    context = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="pricing_agent",
        mission_id="m_001",
        correlation_id="corr_001",
        loop_decision=_dummy_loop_decision(),
        custom_context={
            "proposed_price": Decimal("12000"),
            "minimum_allowed_price": Decimal("10000"),
        },
    )
    result = rule.evaluate(context)
    assert result.passed is True
    assert result.decision_impact == PolicyDecisionType.ALLOW


def test_price_floor_policy_rule_deny_when_below_floor():
    rule = PriceFloorPolicyRule()
    context = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="pricing_agent",
        mission_id="m_001",
        correlation_id="corr_001",
        loop_decision=_dummy_loop_decision(),
        custom_context={
            "proposed_price": Decimal("8500"),
            "minimum_allowed_price": Decimal("10000"),
        },
    )
    result = rule.evaluate(context)
    assert result.passed is False
    assert result.decision_impact == PolicyDecisionType.DENY
    assert any(v.code == "PRICE_BELOW_FLOOR" for v in result.violations)


def test_margin_protection_policy_rule_deny_when_margin_below_threshold():
    rule = MarginProtectionPolicyRule()
    context = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="pricing_agent",
        mission_id="m_001",
        correlation_id="corr_001",
        loop_decision=_dummy_loop_decision(),
        custom_context={
            "expected_margin_pct": Decimal("0.03"),
            "minimum_margin_pct": Decimal("0.10"),
        },
    )
    result = rule.evaluate(context)
    assert result.passed is False
    assert result.decision_impact == PolicyDecisionType.DENY
    assert any(v.code == "MARGIN_BELOW_MINIMUM" for v in result.violations)


def test_max_price_change_policy_rule_excessive_change_deny_and_require_approval():
    rule = MaxPriceChangePolicyRule()

    # 1. Requiere aprobacion si > 20%
    context_25pct = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="pricing_agent",
        mission_id="m_001",
        correlation_id="corr_001",
        loop_decision=_dummy_loop_decision(),
        custom_context={
            "current_price": Decimal("10000"),
            "proposed_price": Decimal("7500"),  # 25% drop
        },
    )
    res_approval = rule.evaluate(context_25pct)
    assert res_approval.passed is False
    assert res_approval.decision_impact == PolicyDecisionType.REQUIRE_APPROVAL
    assert any(v.code == "PRICE_CHANGE_APPROVAL_REQUIRED" for v in res_approval.violations)

    # 2. Con human_approved=True pasa
    context_25pct_approved = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="pricing_agent",
        mission_id="m_001",
        correlation_id="corr_001",
        loop_decision=_dummy_loop_decision(),
        human_approved=True,
        custom_context={
            "current_price": Decimal("10000"),
            "proposed_price": Decimal("7500"),
        },
    )
    res_approved = rule.evaluate(context_25pct_approved)
    assert res_approved.passed is True
    assert res_approved.decision_impact == PolicyDecisionType.ALLOW

    # 3. Bloqueo total (DENY) si > 50%
    context_60pct = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="pricing_agent",
        mission_id="m_001",
        correlation_id="corr_001",
        loop_decision=_dummy_loop_decision(),
        custom_context={
            "current_price": Decimal("10000"),
            "proposed_price": Decimal("3500"),  # 65% drop
        },
    )
    res_deny = rule.evaluate(context_60pct)
    assert res_deny.passed is False
    assert res_deny.decision_impact == PolicyDecisionType.DENY
    assert any(v.code == "EXCESSIVE_PRICE_CHANGE_BLOCKED" for v in res_deny.violations)


def test_policy_engine_end_to_end_pricing_decision_evaluation():
    engine = PolicyEngine()
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")

    # Decisión válida
    decision_ok = PricingDecision(
        decision_id="dec_ok",
        listing_id="MLC999",
        channel=channel,
        current_price=Decimal("10000"),
        proposed_price=Decimal("9500"),  # 5% change
        minimum_allowed_price=Decimal("8000"),
        expected_margin_pct=Decimal("0.18"),
        confidence=Confidence.HIGH,
        risk_level=RiskLevel.LOW,
    )

    ctx_ok = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="pricing_agent",
        mission_id="m_001",
        correlation_id="corr_001",
        idempotency_key="idemp_ok_1",
        loop_decision=_dummy_loop_decision(),
        risk_level=RiskLevel.LOW,
        custom_context={
            "pricing_decision": decision_ok,
            "minimum_margin_pct": Decimal("0.10"),
        },
    )
    eval_ok = engine.evaluate(ctx_ok)
    assert eval_ok.decision == PolicyDecisionType.ALLOW

    # Decisión violando price floor -> DENY
    decision_bad_floor = PricingDecision(
        decision_id="dec_bad",
        listing_id="MLC999",
        channel=channel,
        current_price=Decimal("10000"),
        proposed_price=Decimal("7000"),
        minimum_allowed_price=Decimal("8000"),
        expected_margin_pct=Decimal("0.02"),
    )
    ctx_bad = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="pricing_agent",
        mission_id="m_001",
        correlation_id="corr_001",
        idempotency_key="idemp_bad_1",
        loop_decision=_dummy_loop_decision(),
        custom_context={
            "pricing_decision": decision_bad_floor,
        },
    )
    eval_bad = engine.evaluate(ctx_bad)
    assert eval_bad.decision == PolicyDecisionType.DENY
