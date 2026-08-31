from unittest.mock import MagicMock
import pytest

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel
from src.domain.inventory.models import (
    StockLevel,
    InventoryChangeReason,
    InventoryDecision,
    InventoryAction,
    InventoryStatus,
)
from src.domain.inventory.engine import InventoryDecisionEngine
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.application.inventory.inventory_action_executor import InventoryActionExecutor
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.infrastructure.mercadolibre.inventory_adapter import (
    MercadoLibreInventoryAdapter,
)
from src.domain.tool.registry import ToolRegistry
from src.domain.tool.models import ToolExecutionChannel
from src.application.tool.catalog import register_standard_commerce_tools


def test_e2e_inventory_pipeline_supplier_evidence_decision_policy_execution_audit():
    """
    Integration Pipeline E2E (Casos A-AB):
    Supplier Evidence
    -> Inventory Decision Engine (StockLevel, Available stock, Buffer)
    -> Policy Guard (Overselling & Safety Buffer)
    -> Inventory Action
    -> ActionExecutor
    -> Marketplace Adapter (MercadoLibre PUT /items/{id})
    -> InventoryResult
    -> Audit / Verification
    """
    # 1. Channel y Mock API Client
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.put.return_value = {
        "id": "MLC555666",
        "available_quantity": 8,
        "status": "active",
        "last_updated": "2026-08-30T17:00:00Z",
    }

    adapter = MercadoLibreInventoryAdapter(api_client=mock_client)
    executor = InventoryActionExecutor(inventory_port=adapter, default_channel=channel)
    policy_engine = PolicyEngine()

    # 2. Supplier Evidence & Stock Calculation
    # supplier=10, owned=0, reserved=1, safety_buffer=1 -> available = 10 - 2 = 8
    stock_level = InventoryDecisionEngine.calculate_stock_levels(
        supplier_stock=10,
        owned_stock=0,
        reserved_stock=1,
        safety_buffer=1,
        listed_stock=3,
    )
    assert stock_level.available_to_sell == 8

    # Decisión de proponer 8 unidades
    decision = InventoryDecisionEngine.evaluate_inventory_decision(
        listing_id="MLC555666",
        channel=channel,
        stock_level=stock_level,
        current_quantity=3,
        proposed_quantity=8,
        reason=InventoryChangeReason.SUPPLIER_SYNC,
        rationale="Synchronize stock with refreshed supplier feed",
        evidence={"supplier_feed_units": 10, "feed_timestamp": "2026-08-30T16:55:00Z"},
        confidence=Confidence.HIGH,
        risk_level=RiskLevel.LOW,
    )

    assert decision.is_overselling is False
    assert decision.proposed_quantity == 8

    # 3. Policy Gate Evaluation
    loop_decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Execute supplier stock synchronization",
        parameters={},
    )

    policy_context = PolicyEvaluationContext(
        action_type="UPDATE_INVENTORY",
        actor_id="autonomous_inventory_agent",
        mission_id="mission_e2e_inventory",
        correlation_id="corr_e2e_inv_555",
        idempotency_key="idemp_e2e_inv_555",
        loop_decision=loop_decision,
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        is_external_impact=True,
        custom_context={
            "inventory_decision": decision,
        },
    )

    policy_eval = policy_engine.evaluate(policy_context)
    assert policy_eval.decision == PolicyDecisionType.ALLOW
    assert policy_eval.is_allowed is True

    # 4. Formular InventoryAction y Ejecutar vía ActionExecutor
    inventory_action = InventoryDecisionEngine.formulate_inventory_action(
        decision,
        idempotency_key="idemp_e2e_inv_555",
        correlation_id="corr_e2e_inv_555",
    )

    execution_decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Approved inventory update execution",
        parameters={
            "action_type": "UPDATE_INVENTORY",
            "inventory_action": inventory_action,
            "channel": channel,
        },
    )
    state = LoopState(mission_id="mission_e2e_inventory", iteration=1, goal="e2e_inventory")

    exec_result = executor.execute(execution_decision, state)

    # 5. Verificaciones
    assert exec_result["status"] == "APPLIED"
    assert exec_result["is_success"] is True
    assert exec_result["applied_quantity"] == 8
    assert exec_result["previous_quantity"] == 3
    assert exec_result["idempotency_key"] == "idemp_e2e_inv_555"
    assert exec_result["correlation_id"] == "corr_e2e_inv_555"

    mock_client.put.assert_called_once_with(
        "/items/MLC555666",
        payload={"available_quantity": 8},
    )


def test_tool_registry_inventory_tools_integration():
    registry = ToolRegistry()
    register_standard_commerce_tools(registry)

    # get_inventory
    get_tool = registry.get("get_inventory")
    assert get_tool is not None
    assert get_tool.name == "Marketplace Inventory Reader"
    assert get_tool.input_contract.schema_name == "GetInventoryInput"
    assert get_tool.output_contract.schema_name == "GetInventoryOutput"

    # update_inventory
    upd_tool = registry.get("update_inventory")
    assert upd_tool is not None
    assert upd_tool.name == "Marketplace Inventory Updater"
    assert upd_tool.requires_approval is True
    assert upd_tool.requires_idempotency is True
    assert ToolExecutionChannel.MERCADO_LIBRE in upd_tool.supported_channels

    # reconcile_inventory
    rec_tool = registry.get("reconcile_inventory")
    assert rec_tool is not None
    assert rec_tool.name == "Marketplace Inventory Reconciler"
    assert rec_tool.input_contract.schema_name == "ReconcileInventoryInput"
