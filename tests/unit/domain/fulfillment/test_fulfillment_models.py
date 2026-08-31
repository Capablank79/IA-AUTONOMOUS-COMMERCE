import pytest
from datetime import datetime, timezone
from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.fulfillment.models import (
    FulfillmentError,
    FulfillmentErrorCategory,
    FulfillmentReconciliationReport,
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


def test_shipping_label_creation():
    label = ShippingLabel(
        label_id="lbl_123",
        external_reference="EXT_LBL_456",
        status=LabelStatus.READY,
        format=LabelFormat.PDF,
        url="https://api.mercadolibre.com/shipment_labels?shipment_ids=123",
    )
    assert label.label_id == "lbl_123"
    assert label.status == LabelStatus.READY
    assert label.format == LabelFormat.PDF
    assert label.provenance == EvidenceProvenanceType.LIVE
    assert label.confidence == Confidence.HIGH


def test_tracking_event_creation_and_immutability():
    now = datetime.now(timezone.utc)
    event = TrackingEvent(
        event_id="tr_ev_1",
        shipment_id="shp_100",
        external_shipment_id="ext_shp_200",
        status=TrackingStatus.IN_TRANSIT,
        normalized_status=ShipmentStatus.IN_TRANSIT,
        timestamp=now,
        location="Distribution Center Santiago",
        description="Package in transit to delivery station",
        correlation_id="corr_99",
    )
    assert event.event_id == "tr_ev_1"
    assert event.status == TrackingStatus.IN_TRANSIT
    assert event.normalized_status == ShipmentStatus.IN_TRANSIT
    assert event.location == "Distribution Center Santiago"

    # Immutability
    with pytest.raises(Exception):
        event.status = TrackingStatus.DELIVERED  # type: ignore


def test_shipment_creation_and_properties():
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre Chile")
    label = ShippingLabel(label_id="lbl_001", status=LabelStatus.READY)
    now1 = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    now2 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    ev1 = TrackingEvent(
        event_id="ev1",
        shipment_id="shp_01",
        external_shipment_id="ext_01",
        status=TrackingStatus.PICKED_UP,
        normalized_status=ShipmentStatus.SHIPPED,
        timestamp=now1,
    )
    ev2 = TrackingEvent(
        event_id="ev2",
        shipment_id="shp_01",
        external_shipment_id="ext_01",
        status=TrackingStatus.DELIVERED,
        normalized_status=ShipmentStatus.DELIVERED,
        timestamp=now2,
    )

    shipment = Shipment(
        shipment_id="shp_01",
        external_shipment_id="ext_01",
        order_id="ord_01",
        external_order_id="ext_ord_01",
        channel=channel,
        status=ShipmentStatus.DELIVERED,
        carrier="Chilexpress",
        service_level=ShippingServiceLevel.ME2_DROP_OFF,
        tracking_number="TRK123456789",
        label=label,
        tracking_events=(ev1, ev2),
    )

    assert shipment.shipment_id == "shp_01"
    assert shipment.is_terminal is True
    assert shipment.latest_tracking_event == ev2
    assert len(shipment.tracking_events) == 2


def test_fulfillment_reconciliation_report():
    report = FulfillmentReconciliationReport(
        shipment_id="shp_01",
        external_shipment_id="ext_01",
        order_id="ord_01",
        external_order_id="ext_ord_01",
        is_reconciled=False,
        internal_status=ShipmentStatus.READY_TO_SHIP,
        external_status=ShipmentStatus.SHIPPED,
        discrepancies=("Status mismatch: local=READY_TO_SHIP, external=SHIPPED",),
        requires_action=True,
    )
    assert not report.is_reconciled
    assert report.requires_action is True
    assert len(report.discrepancies) == 1
