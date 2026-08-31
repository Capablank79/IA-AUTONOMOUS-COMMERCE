import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone
from decimal import Decimal

from src.application.fulfillment.fulfillment_service import FulfillmentService
from src.domain.fulfillment.models import (
    LabelFormat,
    LabelStatus,
    Shipment,
    ShipmentQueryResult,
    ShipmentStatus,
    ShippingLabel,
    ShippingServiceLevel,
    TrackingEvent,
    TrackingStatus,
)
from src.domain.fulfillment.ports import FulfillmentPort
from src.domain.market_intelligence.models import Confidence
from src.domain.order.models import BuyerReference, FulfillmentStatus, Order, OrderItem, OrderStatus, PaymentStatus
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import PolicyDecisionType, PolicyEvaluation
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.infrastructure.persistence.data.in_memory.fulfillment_repository import (
    InMemoryFulfillmentRepository,
)


@pytest.fixture
def channel():
    return SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )


@pytest.fixture
def sample_order(channel):
    return Order(
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=OrderStatus.CONFIRMED,
        payment_status=PaymentStatus.APPROVED,
        fulfillment_status=FulfillmentStatus.PENDING,
        items=(
            OrderItem(
                item_id="item_01",
                title="Bluetooth Headphones",
                quantity=1,
                unit_price=Decimal("25000"),
                currency="CLP",
            ),
        ),
        total_amount=Decimal("25000"),
        currency="CLP",
        buyer=BuyerReference(buyer_id="buyer_01"),
    )


@pytest.fixture
def mock_port():
    return MagicMock(spec=FulfillmentPort)


@pytest.fixture
def repo():
    return InMemoryFulfillmentRepository()


@pytest.fixture
def service(mock_port, repo):
    return FulfillmentService(
        fulfillment_port=mock_port,
        fulfillment_repository=repo,
    )


def test_prepare_fulfillment_happy_path(service, repo, sample_order):
    service.fulfillment_port.get_shipment_by_external_order_id.return_value = ShipmentQueryResult(
        shipments=(),
        total_count=0,
        channel=sample_order.channel,
    )

    shipment = service.prepare_fulfillment(
        order=sample_order,
        service_level=ShippingServiceLevel.ME2_DROP_OFF,
    )

    assert shipment is not None
    assert shipment.order_id == sample_order.order_id
    assert shipment.external_order_id == sample_order.external_order_id
    assert shipment.status == ShipmentStatus.READY_TO_SHIP
    assert shipment.service_level == ShippingServiceLevel.ME2_DROP_OFF

    # Verificación en repositorio
    stored = repo.get_shipment_by_id(shipment.shipment_id)
    assert stored is not None
    assert stored.shipment_id == shipment.shipment_id


def test_prepare_fulfillment_idempotent_duplicate_call(service, repo, sample_order):
    service.fulfillment_port.get_shipment_by_external_order_id.return_value = ShipmentQueryResult(
        shipments=(),
        total_count=0,
        channel=sample_order.channel,
    )

    shipment1 = service.prepare_fulfillment(order=sample_order)
    shipment2 = service.prepare_fulfillment(order=sample_order)

    assert shipment1.shipment_id == shipment2.shipment_id
    assert len(repo.list_shipments()) == 1


def test_prepare_fulfillment_rejects_cancelled_order(service, channel):
    cancelled_order = Order(
        order_id="ord_999",
        external_order_id="ext_ord_999",
        channel=channel,
        status=OrderStatus.CANCELLED,
        payment_status=PaymentStatus.REFUNDED,
        fulfillment_status=FulfillmentStatus.CANCELLED,
        items=(
            OrderItem(
                item_id="item_01",
                title="Cancelled Item",
                quantity=1,
                unit_price=Decimal("1000"),
                currency="CLP",
            ),
        ),
        total_amount=Decimal("1000"),
        currency="CLP",
        buyer=BuyerReference(buyer_id="buyer_99"),
    )

    with pytest.raises(ValueError, match="Cannot prepare fulfillment for CANCELLED order"):
        service.prepare_fulfillment(cancelled_order)


def test_sync_shipment_happy_path(service, repo, channel):
    ext_shipment = Shipment(
        shipment_id="shp_4321",
        external_shipment_id="4321",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.IN_TRANSIT,
        carrier="Chilexpress",
        tracking_number="TRK12345",
    )
    service.fulfillment_port.get_shipment_by_external_id.return_value = ShipmentQueryResult(
        shipments=(ext_shipment,),
        total_count=1,
        channel=channel,
    )

    synced = service.sync_shipment("4321", channel)

    assert synced is not None
    assert synced.status == ShipmentStatus.IN_TRANSIT
    assert repo.get_shipment_by_id("shp_4321") is not None


def test_sync_shipment_unknown_preservation(service, repo, channel):
    # Crear un shipment previo local
    initial_shipment = Shipment(
        shipment_id="shp_4321",
        external_shipment_id="4321",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.READY_TO_SHIP,
    )
    repo.save_shipment(initial_shipment)

    # El canal devuelve UNKNOWN por error de red
    unknown_shipment = Shipment(
        shipment_id="shp_4321",
        external_shipment_id="4321",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.UNKNOWN,
        confidence=Confidence.LOW,
    )
    service.fulfillment_port.get_shipment_by_external_id.return_value = ShipmentQueryResult(
        shipments=(unknown_shipment,),
        total_count=1,
        channel=channel,
        is_unknown=True,
    )

    res = service.sync_shipment("4321", channel)
    assert res is not None
    # No sobrescribe destructivamente el estado local previo válido
    assert res.status == ShipmentStatus.READY_TO_SHIP


def test_record_tracking_event_deduplication(service, repo):
    ev = TrackingEvent(
        event_id="ev_track_01",
        shipment_id="shp_4321",
        external_shipment_id="4321",
        status=TrackingStatus.IN_TRANSIT,
        normalized_status=ShipmentStatus.IN_TRANSIT,
        idempotency_key="idemp_tr_01",
    )

    first_ingest = service.record_tracking_event(ev, channel_id="ML-CL")
    second_ingest = service.record_tracking_event(ev, channel_id="ML-CL")

    assert first_ingest is True
    assert second_ingest is False
    assert len(repo.get_tracking_history("shp_4321")) == 1


def test_reconciliation_happy_path(service, repo, channel):
    local_shipment = Shipment(
        shipment_id="shp_4321",
        external_shipment_id="4321",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.IN_TRANSIT,
        tracking_number="TRK123",
    )
    repo.save_shipment(local_shipment)

    ext_shipment = Shipment(
        shipment_id="shp_4321",
        external_shipment_id="4321",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.IN_TRANSIT,
        tracking_number="TRK123",
    )
    service.fulfillment_port.get_shipment_by_external_id.return_value = ShipmentQueryResult(
        shipments=(ext_shipment,),
        total_count=1,
        channel=channel,
    )

    report = service.reconcile_shipment("shp_4321", channel)

    assert report.is_reconciled is True
    assert report.requires_action is False
    assert len(report.discrepancies) == 0


def test_reconciliation_discrepancy_detected(service, repo, channel):
    local_shipment = Shipment(
        shipment_id="shp_4321",
        external_shipment_id="4321",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.READY_TO_SHIP,
        tracking_number="TRK123",
    )
    repo.save_shipment(local_shipment)

    ext_shipment = Shipment(
        shipment_id="shp_4321",
        external_shipment_id="4321",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.DELIVERED,
        tracking_number="TRK123",
    )
    service.fulfillment_port.get_shipment_by_external_id.return_value = ShipmentQueryResult(
        shipments=(ext_shipment,),
        total_count=1,
        channel=channel,
    )

    report = service.reconcile_shipment("shp_4321", channel)

    assert report.is_reconciled is False
    assert report.requires_action is True
    assert any("Status mismatch" in d for d in report.discrepancies)
    # Verifica que actualizó localmente el shipment con el estado externo más avanzado
    assert repo.get_shipment_by_id("shp_4321").status == ShipmentStatus.DELIVERED


def test_execute_fulfillment_action_guarded_by_policy(service, channel):
    mock_policy_engine = MagicMock(spec=PolicyEngine)
    service.policy_engine = mock_policy_engine

    # Caso 1: Acción denegada por política
    mock_eval_denied = MagicMock(spec=PolicyEvaluation)
    mock_eval_denied.is_allowed = False
    mock_eval_denied.decision = PolicyDecisionType.DENY
    mock_eval_denied.reasons = ("Action blocked by safety policy",)
    mock_eval_denied.violations = ()
    mock_policy_engine.evaluate.return_value = mock_eval_denied

    shipment = Shipment(
        shipment_id="shp_4321",
        external_shipment_id="4321",
        order_id="ord_100",
        external_order_id="ext_ord_100",
        channel=channel,
        status=ShipmentStatus.READY_TO_SHIP,
    )

    res_denied = service.execute_fulfillment_action_guarded(
        action_name="DISPATCH_SHIPMENT",
        shipment=shipment,
        payload={"notes": "test"},
        correlation_id="corr_123",
    )
    assert res_denied["success"] is False
    assert res_denied["status"] == "DENIED"

    # Caso 2: Acción permitida por política
    mock_eval_allowed = MagicMock(spec=PolicyEvaluation)
    mock_eval_allowed.is_allowed = True
    mock_eval_allowed.decision = PolicyDecisionType.ALLOW
    mock_policy_engine.evaluate.return_value = mock_eval_allowed

    res_allowed = service.execute_fulfillment_action_guarded(
        action_name="DISPATCH_SHIPMENT",
        shipment=shipment,
        payload={"notes": "test"},
        correlation_id="corr_123",
    )
    assert res_allowed["success"] is True
    assert res_allowed["status"] == "EXECUTED"
