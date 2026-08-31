import json
from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
    PublicationRequest,
    PublicationResult,
    PublicationStatus,
    PublicationErrorCategory,
)
from src.application.publication.publication_action_executor import PublicationActionExecutor
from src.infrastructure.mercadolibre.publication_adapter import MercadoLibrePublicationAdapter
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)


@pytest.fixture
def sample_channel():
    return SalesChannel(
        channel_id="CH_MERCADOLIBRE_CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        region="CL",
        currency="CLP",
        metadata={"user_id": "99887766"},
    )


@pytest.fixture
def sample_draft(sample_channel):
    return ListingDraft(
        draft_id="DRAFT-E2E-001",
        product_reference_id="PROD-SSD-480GB",
        title="Disco Estado Solido Kingston A400 480GB",
        description="SSD interno SATA 3 2.5 pulgadas Kingston 480GB.",
        price=Decimal("34990"),
        currency="CLP",
        available_quantity=15,
        channel=sample_channel,
        images=("https://http2.mlstatic.com/D_NQ_NP_TEST1.jpg",),
        sku="SKU-KING-480GB",
        category_id="MLC1672",
    )


@pytest.fixture
def sample_state():
    return LoopState(
        mission_id="mission_pub_e2e_001",
        iteration=1,
        goal="E2E publication integration test",
    )


def test_e2e_decision_to_mercadolibre_adapter_success(sample_channel, sample_draft, sample_state):
    """
    Verifica el flujo completo:
    LoopDecision -> PublicationActionExecutor -> MercadoLibrePublicationAdapter -> Mocked HTTP API -> PublicationResult
    """
    mock_api = MagicMock()
    mock_api.post.return_value = {
        "id": "MLC987654321",
        "status": "active",
        "permalink": "https://articulo.mercadolibre.cl/MLC-987654321-disco-ssd.html",
        "site_id": "MLC",
        "seller_id": 99887766,
    }

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    executor = PublicationActionExecutor(publication_port=adapter, default_channel=sample_channel)

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publishing vetted listing to Mercado Libre Chile",
        parameters={
            "action_type": "PUBLISH_LISTING",
            "draft": sample_draft,
            "correlation_id": "corr-e2e-123",
            "idempotency_key": "idemp-e2e-123",
        },
    )

    observation = executor.execute(decision, sample_state)

    assert observation["action_executed"] == "PUBLISH"
    assert observation["status"] == "PUBLISHED"
    assert observation["is_success"] is True
    assert observation["is_unknown"] is False
    assert observation["publication_id"] == "MLC987654321"
    assert observation["external_reference"] == "MLC987654321"
    assert observation["permalink"] == "https://articulo.mercadolibre.cl/MLC-987654321-disco-ssd.html"
    assert observation["correlation_id"] == "corr-e2e-123"
    assert observation["idempotency_key"] == "idemp-e2e-123"

    mock_api.post.assert_called_once()
    path, kwargs = mock_api.post.call_args[0][0], mock_api.post.call_args[1]
    assert path == "/items"
    assert kwargs["payload"]["title"] == "Disco Estado Solido Kingston A400 480GB"
    assert kwargs["payload"]["price"] == 34990


def test_e2e_decision_timeout_preserves_unknown_and_recovers_via_verify_status(
    sample_channel, sample_draft, sample_state
):
    """
    Verifica el flujo de resiliencia:
    1. Publicación falla por timeout / desconexión HTTP -> Retorna UNKNOWN (no FAILED).
    2. Siguiente decisión ejecuta VERIFY_STATUS -> Consulta /items/{id} y confirma PUBLISHED.
    """
    mock_api = MagicMock()
    # 1. Fallo inicial por timeout
    mock_api.post.side_effect = MercadoLibreApiError("Mercado Libre API unavailable")

    adapter = MercadoLibrePublicationAdapter(api_client=mock_api)
    executor = PublicationActionExecutor(publication_port=adapter, default_channel=sample_channel)

    decision_pub = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publishing item",
        parameters={
            "action_type": "PUBLISH",
            "draft": sample_draft,
        },
    )

    obs_pub = executor.execute(decision_pub, sample_state)
    assert obs_pub["status"] == "UNKNOWN"
    assert obs_pub["is_unknown"] is True
    assert obs_pub["is_failed"] is False
    assert obs_pub["errors"][0]["category"] == "TIMEOUT"

    # 2. Recuperación vía VERIFY_STATUS
    mock_api.get.return_value = {
        "id": "MLC987654321",
        "status": "active",
        "permalink": "https://articulo.mercadolibre.cl/MLC-987654321",
    }

    decision_verify = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Verifying status of publication",
        parameters={
            "action_type": "VERIFY_STATUS",
            "external_reference": "MLC987654321",
            "channel": sample_channel,
        },
    )

    obs_verify = executor.execute(decision_verify, sample_state)
    assert obs_verify["action_executed"] == "VERIFY_STATUS"
    assert obs_verify["status"] == "PUBLISHED"
    assert obs_verify["is_success"] is True
    assert obs_verify["publication_id"] == "MLC987654321"
