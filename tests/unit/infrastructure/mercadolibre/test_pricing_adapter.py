import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.pricing.models import (
    PricingRequest,
    PricingResult,
    PricingStatus,
    PricingErrorCategory,
    PriceChangeReason,
)
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.infrastructure.mercadolibre.pricing_adapter import MercadoLibrePricingAdapter


@pytest.fixture
def channel():
    return SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        region="CL",
        currency="CLP",
    )


@pytest.fixture
def mock_api_client():
    return MagicMock(spec=MercadoLibreApiClient)


def test_update_price_success(mock_api_client, channel):
    adapter = MercadoLibrePricingAdapter(api_client=mock_api_client)
    mock_api_client.put.return_value = {
        "id": "MLC123456",
        "price": 14990.0,
        "currency_id": "CLP",
        "status": "active",
    }

    req = PricingRequest(
        request_id="req_001",
        listing_id="MLC123456",
        proposed_price=Decimal("14990"),
        current_price=Decimal("18990"),
        channel=channel,
        idempotency_key="idemp_001",
        correlation_id="corr_001",
    )

    result = adapter.update_price(req)

    assert result.status == PricingStatus.APPLIED
    assert result.is_success is True
    assert result.applied_price == Decimal("14990")
    assert result.previous_price == Decimal("18990")
    mock_api_client.put.assert_called_once_with(
        "/items/MLC123456",
        payload={"price": 14990.0},
    )


def test_update_price_validation_error_400(mock_api_client, channel):
    adapter = MercadoLibrePricingAdapter(api_client=mock_api_client)
    mock_api_client.put.side_effect = MercadoLibreApiError(
        message="Invalid price format",
        status_code=400,
        response_body="bad_request",
    )

    req = PricingRequest(
        request_id="req_400",
        listing_id="MLC123456",
        proposed_price=Decimal("14990"),
        channel=channel,
    )

    result = adapter.update_price(req)
    assert result.status == PricingStatus.FAILED
    assert result.is_success is False
    assert result.errors[0].category == PricingErrorCategory.VALIDATION


def test_update_price_auth_error_401_403(mock_api_client, channel):
    adapter = MercadoLibrePricingAdapter(api_client=mock_api_client)
    mock_api_client.put.side_effect = MercadoLibreApiError(
        message="Unauthorized user",
        status_code=401,
        response_body="unauthorized",
    )

    req = PricingRequest(
        request_id="req_401",
        listing_id="MLC123456",
        proposed_price=Decimal("14990"),
        channel=channel,
    )

    result = adapter.update_price(req)
    assert result.status == PricingStatus.FAILED
    assert result.errors[0].category == PricingErrorCategory.AUTHORIZATION


def test_update_price_not_found_404(mock_api_client, channel):
    adapter = MercadoLibrePricingAdapter(api_client=mock_api_client)
    mock_api_client.put.side_effect = MercadoLibreApiError(
        message="Item not found",
        status_code=404,
        response_body="not_found",
    )

    req = PricingRequest(
        request_id="req_404",
        listing_id="MLC999999",
        proposed_price=Decimal("14990"),
        channel=channel,
    )

    result = adapter.update_price(req)
    assert result.status == PricingStatus.FAILED
    assert result.errors[0].category == PricingErrorCategory.NOT_FOUND


def test_update_price_conflict_409(mock_api_client, channel):
    adapter = MercadoLibrePricingAdapter(api_client=mock_api_client)
    mock_api_client.put.side_effect = MercadoLibreApiError(
        message="Item is paused or locked",
        status_code=409,
        response_body="conflict",
    )

    req = PricingRequest(
        request_id="req_409",
        listing_id="MLC123456",
        proposed_price=Decimal("14990"),
        channel=channel,
    )

    result = adapter.update_price(req)
    assert result.status == PricingStatus.FAILED
    assert result.errors[0].category == PricingErrorCategory.CONFLICT


def test_update_price_rate_limit_429(mock_api_client, channel):
    adapter = MercadoLibrePricingAdapter(api_client=mock_api_client)
    mock_api_client.put.side_effect = MercadoLibreApiError(
        message="Too many requests",
        status_code=429,
        response_body="rate_limit_exceeded",
    )

    req = PricingRequest(
        request_id="req_429",
        listing_id="MLC123456",
        proposed_price=Decimal("14990"),
        channel=channel,
    )

    result = adapter.update_price(req)
    assert result.status == PricingStatus.FAILED
    assert result.errors[0].category == PricingErrorCategory.RATE_LIMIT


def test_update_price_timeout_unknown_500_504(mock_api_client, channel):
    adapter = MercadoLibrePricingAdapter(api_client=mock_api_client)
    mock_api_client.put.side_effect = MercadoLibreApiError(
        message="Gateway timeout",
        status_code=504,
        response_body="gateway_timeout",
    )

    req = PricingRequest(
        request_id="req_504",
        listing_id="MLC123456",
        proposed_price=Decimal("14990"),
        channel=channel,
    )

    result = adapter.update_price(req)
    assert result.status == PricingStatus.UNKNOWN
    assert result.is_unknown is True
    assert result.errors[0].category == PricingErrorCategory.UNKNOWN


def test_get_current_price_reconciliation(mock_api_client, channel):
    adapter = MercadoLibrePricingAdapter(api_client=mock_api_client)
    mock_api_client.get.return_value = {
        "id": "MLC123456",
        "price": 14990.0,
        "currency_id": "CLP",
        "status": "active",
    }

    result = adapter.get_current_price(channel=channel, listing_id="MLC123456")
    assert result.status == PricingStatus.APPLIED
    assert result.applied_price == Decimal("14990")
    assert result.is_success is True
    mock_api_client.get.assert_called_once_with("/items/MLC123456")
