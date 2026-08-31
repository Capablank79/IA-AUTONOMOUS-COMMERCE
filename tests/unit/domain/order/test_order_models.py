from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.order.models import (
    OrderStatus,
    PaymentStatus,
    FulfillmentStatus,
    BuyerReference,
    ShipmentReference,
    OrderItem,
    Order,
    OrderEvent,
    OrderEventType,
    OrderQueryResult,
    OrderReconciliationReport,
)


def test_order_item_creation_and_immutability():
    item = OrderItem(
        item_id="item_001",
        listing_id="MLC11223344",
        sku="SKU-GAMING-01",
        title="Gaming Keyboard RGB",
        quantity=2,
        unit_price=Decimal("49.99"),
        currency="USD",
    )
    assert item.item_id == "item_001"
    assert item.listing_id == "MLC11223344"
    assert item.sku == "SKU-GAMING-01"
    assert item.quantity == 2
    assert item.unit_price == Decimal("49.99")
    assert item.total_amount == Decimal("99.98")

    # Inmutability check
    with pytest.raises(Exception):
        item.quantity = 5  # type: ignore


def test_buyer_reference_pii_minimization():
    buyer = BuyerReference(
        buyer_id="buyer_999",
        nickname="tech_user",
        city="Santiago",
        state="RM",
        country_id="CL",
    )
    assert buyer.buyer_id == "buyer_999"
    assert buyer.nickname == "tech_user"
    assert buyer.city == "Santiago"
    assert buyer.country_id == "CL"


def test_order_creation_and_properties():
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    buyer = BuyerReference(buyer_id="buyer_123", nickname="buyer_one")
    item = OrderItem(
        item_id="item_1",
        listing_id="MLC999888",
        sku="SKU-PRO-01",
        title="Pro Headset",
        quantity=1,
        unit_price=Decimal("150.00"),
        currency="CLP",
    )
    shipment = ShipmentReference(
        shipment_id="ship_555",
        status="pending",
        tracking_number=None,
    )
    now = datetime.now(timezone.utc)

    order = Order(
        order_id="ord_internal_001",
        external_order_id="ext_20000001",
        channel=channel,
        status=OrderStatus.PAID,
        payment_status=PaymentStatus.APPROVED,
        fulfillment_status=FulfillmentStatus.PENDING,
        items=[item],
        total_amount=Decimal("150.00"),
        currency="CLP",
        buyer=buyer,
        shipment=shipment,
        created_at=now,
        idempotency_key="idemp_ord_001",
        correlation_id="corr_001",
        provenance=EvidenceProvenanceType.LIVE,
        confidence=Confidence.HIGH,
    )

    assert order.order_id == "ord_internal_001"
    assert order.external_order_id == "ext_20000001"
    assert order.is_confirmed_and_paid is True
    assert order.is_cancelled is False
    assert len(order.items) == 1

    # Inmutability check
    with pytest.raises(Exception):
        order.status = OrderStatus.CANCELLED  # type: ignore


def test_order_status_checks():
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    buyer = BuyerReference(buyer_id="b_1")
    item = OrderItem(
        item_id="it_1",
        listing_id="MLC1",
        title="Test",
        quantity=1,
        unit_price=Decimal("10.00"),
        currency="USD",
    )

    # Pending payment order
    pending_order = Order(
        order_id="o1",
        external_order_id="e1",
        channel=channel,
        status=OrderStatus.PENDING,
        payment_status=PaymentStatus.PENDING,
        fulfillment_status=FulfillmentStatus.PENDING,
        items=[item],
        total_amount=Decimal("10.00"),
        currency="USD",
        buyer=buyer,
    )
    assert pending_order.is_confirmed_and_paid is False
    assert pending_order.is_cancelled is False

    # Cancelled order
    cancelled_order = Order(
        order_id="o2",
        external_order_id="e2",
        channel=channel,
        status=OrderStatus.CANCELLED,
        payment_status=PaymentStatus.REFUNDED,
        fulfillment_status=FulfillmentStatus.CANCELLED,
        items=[item],
        total_amount=Decimal("10.00"),
        currency="USD",
        buyer=buyer,
    )
    assert cancelled_order.is_confirmed_and_paid is False
    assert cancelled_order.is_cancelled is True


def test_order_event_immutability_and_validation():
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    event = OrderEvent(
        event_id="evt_001",
        event_type=OrderEventType.ORDER_CREATED,
        external_order_id="ext_999",
        channel=channel,
        raw_payload={"status": "paid"},
        idempotency_key="idemp_evt_001",
        correlation_id="corr_evt_001",
    )
    assert event.event_id == "evt_001"
    assert event.event_type == OrderEventType.ORDER_CREATED
    assert event.idempotency_key == "idemp_evt_001"

    with pytest.raises(Exception):
        event.event_id = "evt_002"  # type: ignore


def test_order_query_result_and_reconciliation_report():
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    query_res = OrderQueryResult(
        orders=(),
        total_count=0,
        channel=channel,
        is_unknown=True,
    )
    assert query_res.is_unknown is True
    assert query_res.total_count == 0

    report = OrderReconciliationReport(
        order_id="ord_1",
        external_order_id="ext_1",
        is_reconciled=False,
        internal_status=OrderStatus.PAID,
        external_status=OrderStatus.CANCELLED,
        internal_payment_status=PaymentStatus.APPROVED,
        external_payment_status=PaymentStatus.REFUNDED,
        discrepancies=("Status mismatch: internal PAID != external CANCELLED",),
        requires_action=True,
    )
    assert report.is_reconciled is False
    assert len(report.discrepancies) == 1
