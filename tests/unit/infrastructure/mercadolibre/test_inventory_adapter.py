import pytest
from unittest.mock import MagicMock

from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.inventory.models import (
    InventoryRequest,
    InventoryStatus,
    InventoryErrorCategory,
)
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.infrastructure.mercadolibre.inventory_adapter import (
    MercadoLibreInventoryAdapter,
)


def test_update_inventory_success():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.put.return_value = {
        "id": "MLC12345",
        "available_quantity": 12,
        "status": "active",
        "last_updated": "2026-08-30T16:00:00Z",
    }

    adapter = MercadoLibreInventoryAdapter(api_client=mock_client)
    request = InventoryRequest(
        request_id="req_1",
        listing_id="MLC12345",
        proposed_quantity=12,
        current_quantity=5,
        channel=channel,
        idempotency_key="idemp_1",
        correlation_id="corr_1",
    )

    result = adapter.update_inventory(request)

    assert result.status == InventoryStatus.APPLIED
    assert result.is_success is True
    assert result.applied_quantity == 12
    assert result.previous_quantity == 5
    assert result.idempotency_key == "idemp_1"
    assert result.correlation_id == "corr_1"
    mock_client.put.assert_called_once_with("/items/MLC12345", payload={"available_quantity": 12})


def test_update_inventory_timeout_unknown():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.put.side_effect = MercadoLibreApiError("Network timeout connecting to Mercado Libre", status_code=None)

    adapter = MercadoLibreInventoryAdapter(api_client=mock_client)
    request = InventoryRequest(
        request_id="req_timeout",
        listing_id="MLC12345",
        proposed_quantity=8,
        channel=channel,
    )

    result = adapter.update_inventory(request)

    assert result.status == InventoryStatus.UNKNOWN
    assert result.is_unknown is True
    assert result.is_success is False
    assert any(err.category == InventoryErrorCategory.UNKNOWN for err in result.errors)


def test_update_inventory_5xx_unknown():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.put.side_effect = MercadoLibreApiError("Internal Server Error", status_code=502)

    adapter = MercadoLibreInventoryAdapter(api_client=mock_client)
    request = InventoryRequest(
        request_id="req_502",
        listing_id="MLC12345",
        proposed_quantity=8,
        channel=channel,
    )

    result = adapter.update_inventory(request)

    assert result.status == InventoryStatus.UNKNOWN
    assert result.is_unknown is True


def test_get_current_stock_and_reconciliation():
    channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="MercadoLibre")
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.return_value = {
        "id": "MLC12345",
        "available_quantity": 8,
        "status": "active",
        "title": "Producto Test",
    }

    adapter = MercadoLibreInventoryAdapter(api_client=mock_client)
    result = adapter.get_current_stock(channel=channel, listing_id="MLC12345")

    assert result.status == InventoryStatus.APPLIED
    assert result.applied_quantity == 8
    assert result.is_success is True
    mock_client.get.assert_called_once_with("/items/MLC12345")
