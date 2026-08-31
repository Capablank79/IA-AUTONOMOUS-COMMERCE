import pytest
from decimal import Decimal
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel
from src.domain.inventory.models import (
    StockLevel,
    InventoryDecision,
    InventoryAction,
    InventoryRequest,
    InventoryResult,
    InventoryStatus,
    InventoryChangeReason,
    InventoryError,
    InventoryErrorCategory,
)
from src.domain.inventory.engine import InventoryDecisionEngine


def test_stock_level_available_to_sell_calculation():
    # supplier=10, owned=5, reserved=2, safety_buffer=3 -> available = 15 - 5 = 10
    stock = StockLevel(
        supplier_stock=10,
        owned_stock=5,
        reserved_stock=2,
        safety_buffer=3,
        listed_stock=8,
    )
    assert stock.total_backed_stock == 15
    assert stock.total_commitments == 5
    assert stock.available_to_sell == 10
    assert stock.is_overselling(10) is False
    assert stock.is_overselling(11) is True
    assert stock.is_overselling(-1) is True


def test_stock_level_available_to_sell_zero_negative_floor():
    # supplier=2, owned=0, reserved=3, safety_buffer=2 -> total=2, commitments=5 -> max(0, -3) = 0
    stock = StockLevel(
        supplier_stock=2,
        owned_stock=0,
        reserved_stock=3,
        safety_buffer=2,
    )
    assert stock.total_backed_stock == 2
    assert stock.total_commitments == 5
    assert stock.available_to_sell == 0
    assert stock.is_overselling(1) is True
    assert stock.is_overselling(0) is False


def test_inventory_decision_engine_propose_and_formulate():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    stock_level = InventoryDecisionEngine.calculate_stock_levels(
        supplier_stock=20,
        owned_stock=5,
        reserved_stock=3,
        safety_buffer=2,
        listed_stock=15,
    )
    assert stock_level.available_to_sell == 20  # 25 - 5 = 20

    decision = InventoryDecisionEngine.evaluate_inventory_decision(
        listing_id="MLC12345",
        channel=channel,
        stock_level=stock_level,
        current_quantity=15,
        proposed_quantity=20,
        reason=InventoryChangeReason.SUPPLIER_SYNC,
        rationale="Supplier increased available stock",
        evidence={"supplier_feed_units": 20},
        confidence=Confidence.HIGH,
        risk_level=RiskLevel.LOW,
    )

    assert decision.is_overselling is False
    assert decision.proposed_quantity == 20
    assert decision.current_quantity == 15

    action = InventoryDecisionEngine.formulate_inventory_action(
        decision,
        idempotency_key="idemp_inv_123",
        correlation_id="corr_inv_123",
    )
    assert action.listing_id == "MLC12345"
    assert action.proposed_quantity == 20
    assert action.idempotency_key == "idemp_inv_123"
    assert action.correlation_id == "corr_inv_123"


def test_inventory_decision_engine_overselling_detection():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    stock_level = InventoryDecisionEngine.calculate_stock_levels(
        supplier_stock=5,
        owned_stock=0,
        reserved_stock=1,
        safety_buffer=2,
        listed_stock=2,
    )
    # Available = 5 - 3 = 2
    decision = InventoryDecisionEngine.evaluate_inventory_decision(
        listing_id="MLC12345",
        channel=channel,
        stock_level=stock_level,
        current_quantity=2,
        proposed_quantity=5,  # Trying to publish 5 when only 2 available
        reason=InventoryChangeReason.REPLENISHMENT,
    )

    assert decision.is_overselling is True
    assert decision.max_available_quantity == 2
