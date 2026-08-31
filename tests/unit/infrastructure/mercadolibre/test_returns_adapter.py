from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.returns.models import (
    Claim,
    ClaimStatus,
    RefundDetail,
    RefundStatus,
    Return,
    ReturnErrorCategory,
    ReturnQueryResult,
    ReturnReason,
    ReturnStatus,
)
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.infrastructure.mercadolibre.returns_adapter import MercadoLibreReturnsAdapter


def test_status_and_reason_normalization():
    adapter = MercadoLibreReturnsAdapter()
    assert adapter.normalize_return_status("opened") == ReturnStatus.REQUESTED
    assert adapter.normalize_return_status("shipping") == ReturnStatus.IN_TRANSIT
    assert adapter.normalize_return_status("delivered") == ReturnStatus.RECEIVED
    assert adapter.normalize_return_status("closed") == ReturnStatus.RESOLVED
    assert adapter.normalize_return_status("rejected") == ReturnStatus.REJECTED
    assert adapter.normalize_return_status("unknown_xyz") == ReturnStatus.UNKNOWN

    assert adapter.normalize_claim_status("opened") == ClaimStatus.OPENED
    assert adapter.normalize_claim_status("mediation") == ClaimStatus.MEDIATION
    assert adapter.normalize_claim_status("closed") == ClaimStatus.CLOSED

    assert adapter.normalize_return_reason("item_broken_in_transit") == ReturnReason.DAMAGED
    assert adapter.normalize_return_reason("buyer_regret") == ReturnReason.CHANGED_MIND
    assert adapter.normalize_return_reason("other_unknown") == ReturnReason.OTHER


def test_fetch_returns_success():
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.return_value = {
        "results": [
            {
                "id": "1001",
                "order_id": "5001",
                "status": "opened",
                "reason_id": "defect",
                "shipment_id": "shp_900",
            },
            {
                "id": "1002",
                "order_id": "5002",
                "status": "delivered",
                "reason_id": "changed_mind",
            },
        ],
        "paging": {"total": 2},
    }
    adapter = MercadoLibreReturnsAdapter(api_client=mock_client)
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")

    res = adapter.fetch_returns(channel=channel)
    assert res.is_success is True
    assert len(res.returns) == 2
    assert res.returns[0].status == ReturnStatus.REQUESTED
    assert res.returns[0].reason == ReturnReason.DEFECTIVE
    assert res.returns[1].status == ReturnStatus.RECEIVED
    assert res.returns[1].reason == ReturnReason.CHANGED_MIND


def test_get_return_by_external_id_with_refund():
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.return_value = {
        "id": "1001",
        "order_id": "5001",
        "status": "closed",
        "reason_id": "damaged",
        "refund": {
            "id": "ref_999",
            "status": "approved",
            "amount": "120.50",
            "currency_id": "USD",
        },
    }
    adapter = MercadoLibreReturnsAdapter(api_client=mock_client)
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")

    res = adapter.get_return_by_external_id("1001", channel)
    assert res.is_success is True
    ret = res.returns[0]
    assert ret.status == ReturnStatus.RESOLVED
    assert ret.refund is not None
    assert ret.refund.status == RefundStatus.CONFIRMED
    assert ret.refund.amount == Decimal("120.50")


def test_api_error_returns_unknown_result():
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.get.side_effect = MercadoLibreApiError("Service Unavailable", status_code=503)
    adapter = MercadoLibreReturnsAdapter(api_client=mock_client)
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")

    res = adapter.get_return_by_external_id("1001", channel)
    assert res.is_unknown is True
    assert res.is_success is False
    assert len(res.errors) == 1
    assert res.errors[0].category == ReturnErrorCategory.EXTERNAL_SERVICE
    assert res.errors[0].retryable is True
