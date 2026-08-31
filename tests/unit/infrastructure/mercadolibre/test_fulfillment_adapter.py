import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from src.domain.fulfillment.models import (
    LabelFormat,
    LabelStatus,
    Shipment,
    ShipmentStatus,
    ShippingLabel,
    ShippingServiceLevel,
    TrackingEvent,
    TrackingStatus,
)
from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.infrastructure.mercadolibre.fulfillment_adapter import (
    MercadoLibreFulfillmentAdapter,
)


@pytest.fixture
def mock_api_client():
    return MagicMock(spec=MercadoLibreApiClient)


@pytest.fixture
def channel():
    return SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        metadata={"user_id": "123456789"},
    )


@pytest.fixture
def adapter(mock_api_client):
    return MercadoLibreFulfillmentAdapter(api_client=mock_api_client)


def test_normalize_shipment_status(adapter):
    assert adapter.normalize_shipment_status("to_be_agreed") == ShipmentStatus.PENDING
    assert adapter.normalize_shipment_status("pending") == ShipmentStatus.PENDING
    assert adapter.normalize_shipment_status("handling", "ready_to_print") == ShipmentStatus.READY_TO_SHIP
    assert adapter.normalize_shipment_status("handling", "manufacturing") == ShipmentStatus.PROCESSING
    assert adapter.normalize_shipment_status("ready_to_ship") == ShipmentStatus.READY_TO_SHIP
    assert adapter.normalize_shipment_status("shipped") == ShipmentStatus.IN_TRANSIT
    assert adapter.normalize_shipment_status("out_for_delivery") == ShipmentStatus.OUT_FOR_DELIVERY
    assert adapter.normalize_shipment_status("delivered") == ShipmentStatus.DELIVERED
    assert adapter.normalize_shipment_status("cancelled") == ShipmentStatus.CANCELLED
    assert adapter.normalize_shipment_status("unknown") == ShipmentStatus.UNKNOWN
    assert adapter.normalize_shipment_status(None) == ShipmentStatus.PENDING


def test_normalize_service_level(adapter):
    assert adapter.normalize_service_level("fulfillment", "me2") == ShippingServiceLevel.ME2_FULFILLMENT
    assert adapter.normalize_service_level("drop_off", "me2") == ShippingServiceLevel.ME2_DROP_OFF
    assert adapter.normalize_service_level("cross_docking", "me2") == ShippingServiceLevel.ME2_CROSS_DOCKING
    assert adapter.normalize_service_level("self_service", "me2") == ShippingServiceLevel.ME2_FLEX
    assert adapter.normalize_service_level("custom", "custom") == ShippingServiceLevel.CUSTOM
    assert adapter.normalize_service_level("other", "other") == ShippingServiceLevel.STANDARD


def test_get_shipment_by_external_id_success(adapter, mock_api_client, channel):
    mock_api_client.get.return_value = {
        "id": 4321098765,
        "order_id": 200000123456,
        "status": "ready_to_ship",
        "substatus": "printed",
        "logistic_type": "drop_off",
        "shipping_mode": "me2",
        "tracking_number": "TRK987654321",
        "tracking_url": "https://chilexpress.cl/tracking/TRK987654321",
        "carrier_info": {"name": "Chilexpress"},
        "date_created": "2026-03-01T10:00:00.000Z",
        "substatus_history": [
            {
                "status": "handling",
                "substatus": "printed",
                "date": "2026-03-01T11:00:00.000Z",
                "description": "Label printed and package packed",
            }
        ],
    }

    shipment = adapter.get_shipment_by_external_id("4321098765", channel)

    assert shipment is not None
    assert shipment.shipment_id == "shp_4321098765"
    assert shipment.external_shipment_id == "4321098765"
    assert shipment.external_order_id == "200000123456"
    assert shipment.status == ShipmentStatus.READY_TO_SHIP
    assert shipment.carrier == "Chilexpress"
    assert shipment.tracking_number == "TRK987654321"
    assert shipment.service_level == ShippingServiceLevel.ME2_DROP_OFF
    assert len(shipment.tracking_events) == 1
    assert shipment.tracking_events[0].status == TrackingStatus.LABEL_CREATED


def test_get_shipment_by_external_id_404_not_found(adapter, mock_api_client, channel):
    mock_api_client.get.side_effect = MercadoLibreApiError("Shipment not found", status_code=404)

    shipment = adapter.get_shipment_by_external_id("non_existent", channel)
    assert shipment is None


def test_get_shipment_by_external_id_500_unknown_preservation(adapter, mock_api_client, channel):
    mock_api_client.get.side_effect = MercadoLibreApiError("Internal Server Error", status_code=500)

    shipment = adapter.get_shipment_by_external_id("err_500", channel)
    assert shipment is not None
    assert shipment.status == ShipmentStatus.UNKNOWN
    assert shipment.confidence == Confidence.LOW


def test_get_shipping_label_success(adapter, mock_api_client, channel):
    mock_api_client.get.return_value = {
        "id": 4321098765,
        "status": "ready_to_ship",
        "substatus": "printed",
    }

    label = adapter.get_shipping_label("4321098765", channel)
    assert label is not None
    assert label.status == LabelStatus.READY
    assert label.format == LabelFormat.PDF
    assert "shipment_labels?shipment_ids=4321098765" in label.url


def test_get_shipping_label_unknown_on_error(adapter, mock_api_client, channel):
    mock_api_client.get.side_effect = MercadoLibreApiError("Gateway Timeout", status_code=504)

    label = adapter.get_shipping_label("timeout_id", channel)
    assert label is not None
    assert label.status == LabelStatus.ERROR
    assert label.confidence == Confidence.LOW
