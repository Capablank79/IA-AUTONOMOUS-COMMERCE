from unittest.mock import MagicMock
from decimal import Decimal
import pytest

from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.market_intelligence.models import Confidence
from src.domain.order.models import (
    Order,
    OrderItem,
    OrderStatus,
    PaymentStatus,
    FulfillmentStatus,
    BuyerReference,
    OrderEvent,
    OrderEventType,
)
from src.domain.inventory.models import (
    StockLevel,
    InventoryRequest,
    InventoryResult,
    InventoryStatus,
)
from src.domain.inventory.ports import InventoryPort
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.order_adapter import MercadoLibreOrderAdapter
from src.infrastructure.persistence.data.in_memory.order_repository import (
    InMemoryOrderRepository,
)
from src.application.order.order_processing_service import OrderProcessingService
from src.domain.tool.registry import ToolRegistry
from src.domain.tool.models import ToolExecutionChannel, ToolSideEffectLevel
from src.application.tool.catalog import register_standard_commerce_tools


def test_e2e_order_pipeline_ingestion_normalization_inventory_policy_audit():
    """
    Pipeline de Integración E2E G.6:
    External Order Webhook / Ingestion
    -> MercadoLibreOrderAdapter (Mapping & Normalization)
    -> Order Domain Models & State Machine
    -> OrderProcessingService
    -> Policy Guard (Gobernanza de impacto)
    -> Inventory Deduction (Exactly-Once)
    -> Order Persistence & Event Deduplication
    -> Reconciliation against external marketplace
    -> Tool Registry discovery
    """
    # 1. Setup Infraestructura & Canales
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        metadata={"user_id": "12345678"},
    )

    # 2. Ingestión de Evento / Notificación de Orden Pagada
    external_order_payload = {
        "id": 987654321,
        "status": "paid",
        "total_amount": 75000.0,
        "currency_id": "CLP",
        "date_created": "2026-08-30T14:30:00.000Z",
        "buyer": {
            "id": 445566,
            "nickname": "ALEX_BUYER",
            "email": "alex.buyer@company.com",
            "phone": {"area_code": "56", "number": "912345678"},
        },
        "order_items": [
            {
                "item": {
                    "id": "MLC777888",
                    "title": "Ergonomic Mechanical Keyboard",
                    "seller_sku": "SKU-KB-ERGO-01",
                },
                "quantity": 3,
                "unit_price": 25000.0,
                "currency_id": "CLP",
            }
        ],
        "payments": [
            {
                "id": 11223344,
                "status": "approved",
                "transaction_amount": 75000.0,
                "payment_type": "credit_card",
            }
        ],
        "shipping": {
            "id": 990011,
            "status": "pending",
        },
    }

    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.return_value = external_order_payload
    order_adapter = MercadoLibreOrderAdapter(api_client=mock_client)
    order_repo = InMemoryOrderRepository()
    mock_inventory_port = MagicMock(spec=InventoryPort)

    mock_inventory_port.get_current_stock.return_value = InventoryResult(
        inventory_id="inv_chk_1",
        listing_id="MLC777888",
        channel=channel,
        status=InventoryStatus.APPLIED,
        applied_quantity=15,
    )
    mock_inventory_port.update_inventory.return_value = InventoryResult(
        inventory_id="inv_upd_1",
        listing_id="MLC777888",
        channel=channel,
        status=InventoryStatus.APPLIED,
        applied_quantity=12,
    )

    policy_engine = PolicyEngine()
    order_service = OrderProcessingService(
        order_port=order_adapter,
        order_repository=order_repo,
        inventory_port=mock_inventory_port,
        policy_engine=policy_engine,
        default_channel=channel,
    )

    event = OrderEvent(
        event_id="evt_webhook_987654321",
        event_type=OrderEventType.ORDER_CREATED,
        external_order_id="987654321",
        channel=channel,
        raw_payload=external_order_payload,
        idempotency_key="idemp_wh_987654321",
        correlation_id="corr_pipe_987654321",
    )

    # 3. Procesamiento del Evento
    result = order_service.process_order_event(event)

    assert result["status"] == "PROCESSED"
    assert result["order_id"] == "ord_987654321"
    assert result["inventory_impacted"] is True
    assert len(result["inventory_results"]) == 1
    assert result["inventory_results"][0]["status"] == "APPLIED"

    # Verificar persistencia interna y minimización de PII
    stored_order = order_repo.get_order_by_external_id("987654321", "ML-CL")
    assert stored_order is not None
    assert stored_order.status == OrderStatus.PAID
    assert stored_order.payment_status == PaymentStatus.APPROVED
    assert stored_order.fulfillment_status == FulfillmentStatus.PENDING
    assert stored_order.buyer.nickname == "ALEX_BUYER"
    assert stored_order.items[0].quantity == 3

    # Verificar que el stock se descontó exactamente una vez
    mock_inventory_port.update_inventory.assert_called_once()
    inv_call_args = mock_inventory_port.update_inventory.call_args[0][0]
    assert isinstance(inv_call_args, InventoryRequest)
    assert inv_call_args.listing_id == "MLC777888"
    assert inv_call_args.proposed_quantity == 12  # 15 - 3

    # 4. Replay / Duplicado del mismo evento (Idempotencia y protección anti doble descuento)
    dup_result = order_service.process_order_event(event)
    assert dup_result["status"] == "DUPLICATE_IGNORED"
    assert dup_result["inventory_impacted"] is False

    # No se debe haber invocado nuevamente a update_inventory
    assert mock_inventory_port.update_inventory.call_count == 1

    # 5. Reconciliación con Mercado Libre
    mock_client.get.return_value = external_order_payload
    recon_report = order_service.reconcile_order(stored_order.order_id)
    assert recon_report.is_reconciled is True
    assert recon_report.internal_status == OrderStatus.PAID
    assert recon_report.external_status == OrderStatus.PAID
    assert len(recon_report.discrepancies) == 0

    # 6. Tool Registry Integration
    registry = ToolRegistry()
    register_standard_commerce_tools(registry)
    tool_get_orders = registry.get("get_orders")
    tool_get_order = registry.get("get_order")
    tool_reconcile_order = registry.get("reconcile_order")

    assert tool_get_orders is not None
    assert tool_get_order is not None
    assert tool_reconcile_order is not None
    assert ToolExecutionChannel.MERCADO_LIBRE in tool_reconcile_order.supported_channels
    assert tool_reconcile_order.side_effect_level == ToolSideEffectLevel.ANALYSIS
