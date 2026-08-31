from unittest.mock import MagicMock
from decimal import Decimal
import pytest

from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.order.models import OrderStatus, PaymentStatus, FulfillmentStatus
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.infrastructure.mercadolibre.order_adapter import MercadoLibreOrderAdapter


def test_fetch_orders_success_and_normalization():
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.return_value = {
        "paging": {"total": 1, "offset": 0, "limit": 50},
        "results": [
            {
                "id": 200000001,
                "status": "paid",
                "total_amount": 19990.0,
                "currency_id": "CLP",
                "date_created": "2026-08-30T10:00:00.000Z",
                "last_updated": "2026-08-30T10:05:00.000Z",
                "buyer": {
                    "id": 123456,
                    "nickname": "COMPRADOR_DEMO",
                    "email": "user@test.com",
                },
                "order_items": [
                    {
                        "item": {
                            "id": "MLC12345678",
                            "title": "Smart Watch Bluetooth",
                            "seller_sku": "SKU-WATCH-01",
                        },
                        "quantity": 1,
                        "unit_price": 19990.0,
                        "currency_id": "CLP",
                    }
                ],
                "payments": [
                    {
                        "id": 555444333,
                        "status": "approved",
                        "transaction_amount": 19990.0,
                    }
                ],
                "shipping": {
                    "id": 999888777,
                    "status": "pending",
                },
            }
        ],
    }

    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        metadata={"user_id": "12345678"},
    )
    adapter = MercadoLibreOrderAdapter(api_client=mock_client)

    result = adapter.fetch_orders(channel=channel, status="paid")

    assert result.is_unknown is False
    assert result.total_count == 1
    assert len(result.orders) == 1

    order = result.orders[0]
    assert order.external_order_id == "200000001"
    assert order.status == OrderStatus.PAID
    assert order.payment_status == PaymentStatus.APPROVED
    assert order.fulfillment_status == FulfillmentStatus.PENDING
    assert order.total_amount == Decimal("19990.0")
    assert order.currency == "CLP"
    assert order.buyer.buyer_id == "123456"
    assert order.buyer.nickname == "COMPRADOR_DEMO"
    assert len(order.items) == 1
    assert order.items[0].listing_id == "MLC12345678"
    assert order.items[0].sku == "SKU-WATCH-01"
    assert order.items[0].quantity == 1
    assert order.items[0].unit_price == Decimal("19990.0")
    assert order.is_confirmed_and_paid is True


def test_get_order_by_external_id_success():
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.return_value = {
        "id": 200000002,
        "status": "confirmed",
        "total_amount": 50.0,
        "currency_id": "USD",
        "date_created": "2026-08-30T12:00:00.000Z",
        "buyer": {"id": 8888, "nickname": "BUYER_USD"},
        "order_items": [
            {
                "item": {"id": "MLC9999", "title": "Wireless Mouse"},
                "quantity": 2,
                "unit_price": 25.0,
                "currency_id": "USD",
            }
        ],
        "payments": [{"status": "approved"}],
        "shipping": {"id": 111, "status": "shipped", "tracking_number": "TRK12345"},
    }

    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    adapter = MercadoLibreOrderAdapter(api_client=mock_client)
    result = adapter.get_order_by_external_id("200000002", channel=channel)

    assert result.is_unknown is False
    assert len(result.orders) == 1
    order = result.orders[0]
    assert order.external_order_id == "200000002"
    assert order.status == OrderStatus.CONFIRMED
    assert order.payment_status == PaymentStatus.APPROVED
    assert order.fulfillment_status == FulfillmentStatus.SHIPPED
    assert order.shipment is not None
    assert order.shipment.tracking_number == "TRK12345"


def test_fetch_orders_handles_http_errors_as_unknown():
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.side_effect = MercadoLibreApiError("Gateway 504", status_code=504)

    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        metadata={"user_id": "12345678"},
    )
    adapter = MercadoLibreOrderAdapter(api_client=mock_client)

    result = adapter.fetch_orders(channel=channel)

    assert result.is_unknown is True
    assert result.total_count == 0
    assert len(result.orders) == 0
    assert len(result.errors) == 1
    assert result.errors[0].code == "504"
    assert "Gateway 504" in result.errors[0].message


def test_get_order_404_returns_empty_not_unknown():
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.side_effect = MercadoLibreApiError("Order not found", status_code=404)

    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        metadata={"user_id": "12345678"},
    )
    adapter = MercadoLibreOrderAdapter(api_client=mock_client)

    result = adapter.get_order_by_external_id("non_existent_id", channel=channel)

    assert result.is_unknown is False
    assert result.total_count == 0
    assert len(result.orders) == 0
    assert len(result.errors) == 1
    assert result.errors[0].code == "404"
