from unittest.mock import MagicMock
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.inventory.models import (
    StockLevel,
    InventoryAction,
    InventoryDecision,
    InventoryResult,
    InventoryStatus,
    InventoryChangeReason,
)
from src.domain.inventory.ports import InventoryPort
from src.application.inventory.inventory_action_executor import InventoryActionExecutor


def test_inventory_action_executor_update_success():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    mock_port = MagicMock(spec=InventoryPort)
    mock_port.update_inventory.return_value = InventoryResult(
        inventory_id="MLC123",
        channel=channel,
        status=InventoryStatus.APPLIED,
        listing_id="MLC123",
        applied_quantity=15,
        previous_quantity=10,
        request_id="req_1",
        idempotency_key="idemp_1",
        correlation_id="corr_1",
    )

    executor = InventoryActionExecutor(inventory_port=mock_port, default_channel=channel)
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Update inventory from supplier sync",
        parameters={
            "action_type": "UPDATE_INVENTORY",
            "listing_id": "MLC123",
            "proposed_quantity": 15,
            "current_quantity": 10,
            "idempotency_key": "idemp_1",
            "correlation_id": "corr_1",
        },
    )
    state = LoopState(mission_id="m1", iteration=1, goal="sync_stock")

    result = executor.execute(decision, state)

    assert result["status"] == "APPLIED"
    assert result["applied_quantity"] == 15
    assert result["previous_quantity"] == 10
    assert result["is_success"] is True
    assert executor.external_calls_count == 1


def test_inventory_action_executor_verify_stock():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    mock_port = MagicMock(spec=InventoryPort)
    mock_port.get_current_stock.return_value = InventoryResult(
        inventory_id="MLC123",
        channel=channel,
        status=InventoryStatus.APPLIED,
        listing_id="MLC123",
        applied_quantity=10,
    )

    executor = InventoryActionExecutor(inventory_port=mock_port, default_channel=channel)
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Verify current stock on channel",
        parameters={
            "action_type": "VERIFY_STOCK",
            "listing_id": "MLC123",
        },
    )
    state = LoopState(mission_id="m1", iteration=1, goal="verify_stock")

    result = executor.execute(decision, state)

    assert result["status"] == "APPLIED"
    assert result["current_quantity"] == 10
    assert result["is_success"] is True
