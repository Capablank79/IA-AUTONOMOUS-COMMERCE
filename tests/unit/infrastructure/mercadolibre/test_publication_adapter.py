import json
from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
    PublicationRequest,
    PublicationResult,
    PublicationStatus,
    PublicationErrorCategory,
)
from src.domain.publication.ports import PublicationPort
from src.domain.oauth.models import OAuthConnection
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.infrastructure.mercadolibre.publication_adapter import (
    MercadoLibrePublicationAdapter,
)


@pytest.fixture
def sample_channel():
    return SalesChannel(
        channel_id="meli_cl",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        region="CL",
        currency="CLP",
        metadata={"user_id": "12345678"},
    )


@pytest.fixture
def sample_draft(sample_channel):
    return ListingDraft(
        draft_id="draft_001",
        product_reference_id="prod_ssd_480",
        title="Disco Solido SSD Kingston 480GB A400 SATA 3",
        description="Excelente SSD Kingston de 480GB, alta velocidad y rendimiento.",
        price=Decimal("35990"),
        currency="CLP",
        available_quantity=10,
        channel=sample_channel,
        images=("https://http2.mlstatic.com/D_NQ_NP_1.jpg", "https://http2.mlstatic.com/D_NQ_NP_2.jpg"),
        attributes={"BRAND": "Kingston", "MODEL": "SA400S37/480G"},
        sku="SKU-KING-480",
        category_id="MLC1672",
        condition="new",
        metadata={"listing_type_id": "gold_special", "buying_mode": "buy_it_now"},
    )


@pytest.fixture
def sample_request(sample_draft, sample_channel):
    return PublicationRequest(
        request_id="req_test_001",
        draft=sample_draft,
        channel=sample_channel,
        idempotency_key="idemp_draft_001",
        correlation_id="corr_mission_001",
    )


# ---------------------------------------------------------------------------
# TEST A: Mapping interno -> payload
# ---------------------------------------------------------------------------
def test_mapping_draft_to_payload(sample_draft):
    adapter = MercadoLibrePublicationAdapter()
    payload = adapter.map_draft_to_payload(sample_draft)

    assert payload["title"] == "Disco Solido SSD Kingston 480GB A400 SATA 3"
    assert payload["category_id"] == "MLC1672"
    assert payload["price"] == 35990
    assert payload["currency_id"] == "CLP"
    assert payload["available_quantity"] == 10
    assert payload["buying_mode"] == "buy_it_now"
    assert payload["listing_type_id"] == "gold_special"
    assert payload["condition"] == "new"
    assert payload["description"] == {"plain_text": sample_draft.description}
    assert len(payload["pictures"]) == 2
    assert payload["pictures"][0] == {"source": "https://http2.mlstatic.com/D_NQ_NP_1.jpg"}
    
    # Attributes & SKU
    attr_ids = [a["id"] for a in payload["attributes"]]
    assert "BRAND" in attr_ids
    assert "MODEL" in attr_ids
    assert "SELLER_SKU" in attr_ids
    sku_attr = next(a for a in payload["attributes"] if a["id"] == "SELLER_SKU")
    assert sku_attr["value_name"] == "SKU-KING-480"


# ---------------------------------------------------------------------------
# TEST B: Mapping response -> PublicationResult
# ---------------------------------------------------------------------------
def test_mapping_response_to_result_published(sample_channel, sample_request):
    adapter = MercadoLibrePublicationAdapter()
    meli_resp = {
        "id": "MLC99887766",
        "status": "active",
        "permalink": "https://articulo.mercadolibre.cl/MLC-99887766-disco-ssd.html",
        "site_id": "MLC",
        "seller_id": 12345678,
    }

    result = adapter.map_response_to_result(meli_resp, sample_channel, request=sample_request)

    assert result.status == PublicationStatus.PUBLISHED
    assert result.is_success is True
    assert result.publication_id == "MLC99887766"
    assert result.external_reference == "MLC99887766"
    assert result.permalink == "https://articulo.mercadolibre.cl/MLC-99887766-disco-ssd.html"
    assert result.metadata["correlation_id"] == "corr_mission_001"
    assert result.metadata["idempotency_key"] == "idemp_draft_001"
    assert result.metadata["request_id"] == "req_test_001"
    assert result.published_at is not None


# ---------------------------------------------------------------------------
# TEST C: Adapter cumple PublicationPort
# ---------------------------------------------------------------------------
def test_adapter_satisfies_publication_port():
    adapter = MercadoLibrePublicationAdapter()
    assert isinstance(adapter, PublicationPort)
    assert hasattr(adapter, "publish")
    assert hasattr(adapter, "get_status")


# ---------------------------------------------------------------------------
# TEST D: Success Publication
# ---------------------------------------------------------------------------
def test_publish_success(sample_request, sample_channel):
    mock_api = MagicMock()
    mock_api.post.return_value = {
        "id": "MLC123456789",
        "status": "active",
        "permalink": "https://articulo.mercadolibre.cl/MLC-123456789.html",
        "site_id": "MLC",
        "seller_id": 12345678,
    }

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    assert result.status == PublicationStatus.PUBLISHED
    assert result.is_success is True
    assert result.publication_id == "MLC123456789"
    assert result.external_reference == "MLC123456789"
    assert result.permalink == "https://articulo.mercadolibre.cl/MLC-123456789.html"
    assert len(result.errors) == 0

    mock_api.post.assert_called_once()
    path, kwargs = mock_api.post.call_args[0][0], mock_api.post.call_args[1]
    assert path == "/items"
    assert kwargs["payload"]["title"] == sample_request.draft.title


# ---------------------------------------------------------------------------
# TEST E: Validation Error (HTTP 400 / 422)
# ---------------------------------------------------------------------------
def test_publish_validation_error(sample_request):
    mock_api = MagicMock()
    mock_api.post.side_effect = MercadoLibreApiError(
        "Validation error",
        status_code=400,
        response_body=json.dumps({
            "message": "Validation error",
            "error": "body.attributes.invalid",
            "status": 400,
            "cause": [{"code": "attribute.not_allowed", "message": "Attribute FOO is not allowed"}],
        }),
    )

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    assert result.status == PublicationStatus.FAILED
    assert result.is_failed is True
    assert len(result.errors) == 1
    assert result.errors[0].category == PublicationErrorCategory.VALIDATION
    assert result.errors[0].retryable is False
    assert result.errors[0].message == "body.attributes.invalid"


# ---------------------------------------------------------------------------
# TEST F: Authorization Error (HTTP 401 / 403)
# ---------------------------------------------------------------------------
def test_publish_authorization_error(sample_request):
    mock_api = MagicMock()
    mock_api.post.side_effect = MercadoLibreApiError(
        "Forbidden",
        status_code=403,
        response_body=json.dumps({
            "message": "Access to the requested resource is forbidden",
            "error": "access_denied",
            "status": 403,
        }),
    )

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    assert result.status == PublicationStatus.FAILED
    assert result.is_failed is True
    assert len(result.errors) == 1
    assert result.errors[0].category == PublicationErrorCategory.AUTHORIZATION
    assert result.errors[0].retryable is False


# ---------------------------------------------------------------------------
# TEST G: Rate Limit Error (HTTP 429)
# ---------------------------------------------------------------------------
def test_publish_rate_limit_error(sample_request):
    mock_api = MagicMock()
    mock_api.post.side_effect = MercadoLibreApiError(
        "Rate limit exceeded",
        status_code=429,
        response_body=json.dumps({
            "message": "Too many requests",
            "error": "too_many_requests",
            "status": 429,
        }),
    )

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    assert result.status == PublicationStatus.FAILED
    assert result.is_failed is True
    assert len(result.errors) == 1
    assert result.errors[0].category == PublicationErrorCategory.RATE_LIMIT
    assert result.errors[0].retryable is True


# ---------------------------------------------------------------------------
# TEST H: Timeout / Transport Error -> UNKNOWN
# ---------------------------------------------------------------------------
def test_publish_timeout_preserves_unknown(sample_request):
    mock_api = MagicMock()
    mock_api.post.side_effect = MercadoLibreApiError("Mercado Libre API unavailable")

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    assert result.status == PublicationStatus.UNKNOWN
    assert result.is_unknown is True
    assert len(result.errors) == 1
    assert result.errors[0].category == PublicationErrorCategory.TIMEOUT
    assert result.errors[0].retryable is True


# ---------------------------------------------------------------------------
# TEST I: 5xx External Service Error -> UNKNOWN on POST
# ---------------------------------------------------------------------------
def test_publish_500_preserves_unknown(sample_request):
    mock_api = MagicMock()
    mock_api.post.side_effect = MercadoLibreApiError(
        "Internal server error",
        status_code=500,
        response_body=json.dumps({
            "message": "Internal error",
            "error": "internal_error",
            "status": 500,
        }),
    )

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    assert result.status == PublicationStatus.UNKNOWN
    assert result.is_unknown is True
    assert len(result.errors) == 1
    assert result.errors[0].category == PublicationErrorCategory.EXTERNAL_SERVICE
    assert result.errors[0].retryable is True


# ---------------------------------------------------------------------------
# TEST J: Malformed Response -> UNKNOWN
# ---------------------------------------------------------------------------
def test_publish_malformed_response_returns_unknown(sample_request):
    mock_api = MagicMock()
    mock_api.post.return_value = {"unexpected_structure": True}

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    assert result.status == PublicationStatus.UNKNOWN
    assert result.is_unknown is True
    assert len(result.errors) == 1
    assert result.errors[0].category == PublicationErrorCategory.UNKNOWN
    assert result.errors[0].code == "MALFORMED_RESPONSE"


# ---------------------------------------------------------------------------
# TEST K: Verify Status (get_status)
# ---------------------------------------------------------------------------
def test_get_status_success(sample_channel):
    mock_api = MagicMock()
    mock_api.get.return_value = {
        "id": "MLC123456789",
        "status": "active",
        "permalink": "https://articulo.mercadolibre.cl/MLC-123456789.html",
        "site_id": "MLC",
    }

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.get_status(sample_channel, "MLC123456789")

    assert result.status == PublicationStatus.PUBLISHED
    assert result.is_success is True
    assert result.publication_id == "MLC123456789"
    assert result.external_reference == "MLC123456789"
    mock_api.get.assert_called_once_with("/items/MLC123456789")


def test_get_status_handles_error(sample_channel):
    mock_api = MagicMock()
    mock_api.get.side_effect = MercadoLibreApiError(
        "Item not found",
        status_code=404,
        response_body=json.dumps({"message": "Item not found", "error": "not_found", "status": 404}),
    )

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.get_status(sample_channel, "MLC_INVALID")

    assert result.status == PublicationStatus.FAILED
    assert result.is_failed is True
    assert len(result.errors) == 1
    assert result.errors[0].code == "404"


# ---------------------------------------------------------------------------
# TEST L & M: Idempotency and Correlation Preservation
# ---------------------------------------------------------------------------
def test_idempotency_and_correlation_preserved(sample_request):
    mock_api = MagicMock()
    mock_api.post.return_value = {
        "id": "MLC55555",
        "status": "active",
        "permalink": "https://articulo.mercadolibre.cl/MLC-55555.html",
    }

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    assert result.metadata["correlation_id"] == "corr_mission_001"
    assert result.metadata["idempotency_key"] == "idemp_draft_001"
    assert result.metadata["request_id"] == "req_test_001"


# ---------------------------------------------------------------------------
# TEST N: Secrets never leaked in result or errors
# ---------------------------------------------------------------------------
def test_no_secrets_in_result_or_errors(sample_request):
    mock_api = MagicMock()
    mock_api.post.side_effect = MercadoLibreApiError(
        "Invalid token: APP_USR-1234567890-SECRET-TOKEN",
        status_code=401,
        response_body=json.dumps({"message": "Invalid token", "error": "unauthorized"}),
    )

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    result = adapter.publish(sample_request)

    result_repr = repr(result)
    assert "APP_USR-1234567890-SECRET-TOKEN" not in result_repr
    assert result.errors[0].message == "unauthorized"


# ---------------------------------------------------------------------------
# TEST O: Integration with OAuthConnectionService
# ---------------------------------------------------------------------------
def test_adapter_with_oauth_connection_service(sample_request, sample_channel):
    mock_oauth_service = MagicMock()
    mock_oauth_service.get_valid_connection.return_value = OAuthConnection(
        provider="mercadolibre",
        user_id="12345678",
        access_token="valid-refreshed-token",
        refresh_token="valid-refresh-token",
        expires_at=None,
    )

    adapter = MercadoLibrePublicationAdapter(oauth_service=mock_oauth_service)
    client = adapter._get_api_client(sample_channel)

    assert isinstance(client, MercadoLibreApiClient)
    assert client.access_token == "valid-refreshed-token"
    mock_oauth_service.get_valid_connection.assert_called_once_with(
        provider="mercadolibre",
        user_id="12345678",
    )
