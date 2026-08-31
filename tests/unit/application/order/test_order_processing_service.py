from unittest.mock import MagicMock
from decimal import Decimal
import pytest

from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.order.models import (
    Order,
    OrderItem,
    OrderStatus,
    PaymentStatus,
    FulfillmentStatus,
    BuyerReference,
    OrderEvent,
    OrderEventType,
    OrderQueryResult,
)
from src.domain.inventory.models import (
    StockLevel,
    InventoryRequest,
    InventoryResult,
    InventoryStatus,
)
from src.domain.inventory.ports import InventoryPort
from src.domain.policy.engine import PolicyEngine
from src.domain.mission.models import LoopDecision, LoopAction, LoopState
from src.infrastructure.persistence.data.in_memory.order_repository import (
    InMemoryOrderRepository,
)
from src.application.order.order_processing_service import (
    OrderProcessingService,
    OrderActionExecutor,
)
from src.domain.order.ports import OrderPort


def test_order_event_processing_and_exactly_once_inventory_deduction():
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    repo = InMemoryOrderRepository()
    mock_order_port = MagicMock(spec=OrderPort)
    mock_inventory_port = MagicMock(spec=InventoryPort)

    # Configurar respuesta del mock de inventario
    mock_inventory_port.get_current_stock.return_value = InventoryResult(
        inventory_id="inv_check_1",
        listing_id="MLC112233",
        channel=channel,
        status=InventoryStatus.APPLIED,
        applied_quantity=10,
    )
    mock_inventory_port.update_inventory.return_value = InventoryResult(
        inventory_id="inv_upd_1",
        listing_id="MLC112233",
        channel=channel,
        status=InventoryStatus.APPLIED,
        applied_quantity=8,
    )

    policy_engine = PolicyEngine()
    service = OrderProcessingService(
        order_port=mock_order_port,
        order_repository=repo,
        inventory_port=mock_inventory_port,
        policy_engine=policy_engine,
        default_channel=channel,
    )

    # Evento de orden creada y pagada
    event = OrderEvent(
        event_id="evt_paid_100",
        event_type=OrderEventType.ORDER_CREATED,
        external_order_id="200000100",
        channel=channel,
        order=Order(
            order_id="ord_200000100",
            external_order_id="200000100",
            channel=channel,
            status=OrderStatus.PAID,
            payment_status=PaymentStatus.APPROVED,
            fulfillment_status=FulfillmentStatus.PENDING,
            items=[
                OrderItem(
                    item_id="MLC112233",
                    title="Headset",
                    quantity=2,
                    unit_price=Decimal("50.0"),
                    currency="USD",
                    listing_id="MLC112233",
                )
            ],
            total_amount=Decimal("100.0"),
            currency="USD",
            buyer=BuyerReference(buyer_id="b_1", nickname="Buyer1"),
        ),
        idempotency_key="idemp_100",
        correlation_id="corr_100",
    )

    # 1. Primer procesamiento
    res1 = service.process_order_event(event)
    assert res1["status"] == "PROCESSED"
    assert res1["order_id"] == "ord_200000100"
    assert res1["inventory_impacted"] is True
    assert len(res1["inventory_results"]) == 1
    assert res1["inventory_results"][0]["status"] == "APPLIED"

    # Verificar que el stock se descontó una vez
    mock_inventory_port.update_inventory.assert_called_once()
    assert repo.get_order_by_external_id("200000100", "ML-CL") is not None

    # 2. Reintento con el MISMO evento (Idempotencia)
    res2 = service.process_order_event(event)
    assert res2["status"] == "DUPLICATE_IGNORED"
    assert res2["inventory_impacted"] is False

    # Verificar que NO se volvió a llamar a update_inventory (Exactly-once)
    assert mock_inventory_port.update_inventory.call_count == 1


def test_order_event_pending_does_not_deduct_inventory():
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    repo = InMemoryOrderRepository()
    mock_order_port = MagicMock(spec=OrderPort)
    mock_inventory_port = MagicMock(spec=InventoryPort)

    service = OrderProcessingService(
        order_port=mock_order_port,
        order_repository=repo,
        inventory_port=mock_inventory_port,
        default_channel=channel,
    )

    # Orden con pago PENDING
    event = OrderEvent(
        event_id="evt_pending_200",
        event_type=OrderEventType.ORDER_CREATED,
        external_order_id="200000200",
        channel=channel,
        order=Order(
            order_id="ord_200000200",
            external_order_id="200000200",
            channel=channel,
            status=OrderStatus.PENDING,
            payment_status=PaymentStatus.PENDING,
            fulfillment_status=FulfillmentStatus.PENDING,
            items=[
                OrderItem(
                    item_id="MLC999",
                    title="Cable",
                    quantity=1,
                    unit_price=Decimal("50.0"),
                    currency="USD",
                    listing_id="MLC999",
                )
            ],
            total_amount=Decimal("50.0"),
            currency="USD",
            buyer=BuyerReference(buyer_id="b_2", nickname="Buyer2"),
        ),
        idempotency_key="idemp_200",
    )

    res = service.process_order_event(event)
    assert res["status"] == "PROCESSED"
    assert res["inventory_impacted"] is False
    assert len(res["inventory_results"]) == 0
    mock_inventory_port.update_inventory.assert_not_called()


def test_order_reconciliation_detects_discrepancy():
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    repo = InMemoryOrderRepository()
    mock_order_port = MagicMock(spec=OrderPort)

    # Guardamos una orden interna con estado PAID
    order = Order(
        order_id="ord_int_300",
        external_order_id="ext_300",
        channel=channel,
        status=OrderStatus.PAID,
        payment_status=PaymentStatus.APPROVED,
        fulfillment_status=FulfillmentStatus.PENDING,
        items=[
            OrderItem(
                item_id="it_300",
                title="Product 300",
                quantity=1,
                unit_price=Decimal("100.00"),
                currency="USD",
            )
        ],
        total_amount=Decimal("100.00"),
        currency="USD",
        buyer=BuyerReference(buyer_id="b_3"),
    )
    repo.save_order(order)

    # El canal externo ahora reporta que la orden fue CANCELADA
    external_order = Order(
        order_id="ord_ext_300",
        external_order_id="ext_300",
        channel=channel,
        status=OrderStatus.CANCELLED,
        payment_status=PaymentStatus.REFUNDED,
        fulfillment_status=FulfillmentStatus.CANCELLED,
        items=[
            OrderItem(
                item_id="it_300",
                title="Product 300",
                quantity=1,
                unit_price=Decimal("100.00"),
                currency="USD",
            )
        ],
        total_amount=Decimal("100.00"),
        currency="USD",
        buyer=BuyerReference(buyer_id="b_3"),
    )
    mock_order_port.get_order_by_external_id.return_value = OrderQueryResult(
        orders=[external_order],
        total_count=1,
        channel=channel,
    )

    service = OrderProcessingService(
        order_port=mock_order_port,
        order_repository=repo,
        default_channel=channel,
    )

    report = service.reconcile_order("ord_int_300")
    assert report.is_reconciled is False
    assert report.internal_status == OrderStatus.PAID
    assert report.external_status == OrderStatus.CANCELLED
    assert len(report.discrepancies) > 0
    assert report.requires_action is True


def test_order_action_executor_sync_and_reconcile():
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    repo = InMemoryOrderRepository()
    mock_order_port = MagicMock(spec=OrderPort)
    mock_order_port.fetch_orders.return_value = OrderQueryResult(
        orders=(),
        total_count=0,
        channel=channel,
    )

    service = OrderProcessingService(
        order_port=mock_order_port,
        order_repository=repo,
        default_channel=channel,
    )
    executor = OrderActionExecutor(order_processing_service=service)

    state = LoopState(
        mission_id="mission_order_01",
        iteration=1,
        goal="Process marketplace orders",
    )
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Sync orders from marketplace",
        parameters={"action_type": "SYNC_ORDERS", "status": "paid", "limit": 10},
    )

    result = executor.execute(decision, state)
    assert result["status"] == "SUCCESS"
    assert result["synced_orders_count"] == 0
    mock_order_port.fetch_orders.assert_called_once()
