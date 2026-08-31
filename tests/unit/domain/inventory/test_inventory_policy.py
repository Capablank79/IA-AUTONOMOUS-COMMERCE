import pytest
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel
from src.domain.mission.models import LoopDecision, LoopAction
from src.domain.inventory.models import (
    StockLevel,
    InventoryDecision,
    InventoryChangeReason,
)
from src.domain.inventory.engine import InventoryDecisionEngine
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.domain.policy.rules import (
    OversellingProtectionPolicyRule,
    InventorySafetyBufferPolicyRule,
)


def test_overselling_protection_rule_deny():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    stock_level = StockLevel(supplier_stock=5, owned_stock=0, reserved_stock=1, safety_buffer=1)
    # available = 5 - 2 = 3
    decision = InventoryDecisionEngine.evaluate_inventory_decision(
        listing_id="MLC999",
        channel=channel,
        stock_level=stock_level,
        current_quantity=1,
        proposed_quantity=4,  # > 3 -> overselling
        reason=InventoryChangeReason.SUPPLIER_SYNC,
    )

    engine = PolicyEngine()
    loop_dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Update stock",
    )
    context = PolicyEvaluationContext(
        action_type="UPDATE_INVENTORY",
        actor_id="test_agent",
        mission_id="m1",
        correlation_id="corr_test_1",
        loop_decision=loop_dec,
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        custom_context={"inventory_decision": decision},
    )

    eval_result = engine.evaluate(context)
    assert eval_result.decision == PolicyDecisionType.DENY
    assert eval_result.is_denied is True
    assert any("OVERSELLING_PREVENTED" in v.code for v in eval_result.violations)


def test_negative_stock_blocked_deny():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    decision = InventoryDecision(
        decision_id="dec_neg_1",
        listing_id="MLC999",
        channel=channel,
        proposed_stock=-5,
        current_stock=2,
        stock_levels=StockLevel(supplier_stock=10),
        reason=InventoryChangeReason.POLICY_CORRECTION,
    )

    engine = PolicyEngine()
    loop_dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Negative stock test",
    )
    context = PolicyEvaluationContext(
        action_type="UPDATE_INVENTORY",
        actor_id="test_agent",
        mission_id="m1",
        correlation_id="corr_test_2",
        loop_decision=loop_dec,
        custom_context={"inventory_decision": decision},
    )

    eval_result = engine.evaluate(context)
    assert eval_result.decision == PolicyDecisionType.DENY
    assert any("NEGATIVE_STOCK_BLOCKED" in v.code for v in eval_result.violations)


def test_safety_buffer_policy_rule_unknown_when_supplier_stock_none():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    stock_level = StockLevel(supplier_stock=0, safety_buffer=0)  # Unknown supplier evidence
    decision = InventoryDecision(
        decision_id="dec_unk_1",
        listing_id="MLC999",
        channel=channel,
        proposed_stock=5,
        current_stock=0,
        stock_levels=stock_level,
        reason=InventoryChangeReason.SUPPLIER_SYNC,
        evidence={"supplier_stock": None},  # Explicit missing supplier stock
    )

    engine = PolicyEngine()
    loop_dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Unknown supplier stock test",
    )
    context = PolicyEvaluationContext(
        action_type="UPDATE_INVENTORY",
        actor_id="test_agent",
        mission_id="m1",
        correlation_id="corr_test_3",
        loop_decision=loop_dec,
        custom_context={"inventory_decision": decision},
    )

    eval_result = engine.evaluate(context)
    # UNKNOWN or DENY
    assert eval_result.is_allowed is False


def test_inventory_policy_allow_when_safe():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    stock_level = StockLevel(supplier_stock=10, owned_stock=0, reserved_stock=1, safety_buffer=2)
    # available = 10 - 3 = 7
    decision = InventoryDecisionEngine.evaluate_inventory_decision(
        listing_id="MLC999",
        channel=channel,
        stock_level=stock_level,
        current_quantity=2,
        proposed_quantity=5,  # <= 7 and > 0, buffer preserved
        reason=InventoryChangeReason.SUPPLIER_SYNC,
        confidence=Confidence.HIGH,
        risk_level=RiskLevel.LOW,
    )

    engine = PolicyEngine()
    loop_dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Safe stock test",
    )
    context = PolicyEvaluationContext(
        action_type="UPDATE_INVENTORY",
        actor_id="test_agent",
        mission_id="m1",
        correlation_id="corr_test_4",
        loop_decision=loop_dec,
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        custom_context={"inventory_decision": decision},
    )

    eval_result = engine.evaluate(context)
    assert eval_result.decision == PolicyDecisionType.ALLOW
    assert eval_result.is_allowed is True
