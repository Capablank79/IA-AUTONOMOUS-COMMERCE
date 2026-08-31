from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.pricing.models import (
    PricingAction,
    PricingDecision,
    PricingRequest,
    PricingResult,
    PricingStatus,
    PriceChangeReason,
)
from src.domain.pricing.ports import PricingPort, PricingRepository
from src.application.pricing.pricing_action_executor import PricingActionExecutor


@pytest.fixture
def channel():
    return SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )


@pytest.fixture
def mock_pricing_port():
    return MagicMock(spec=PricingPort)


@pytest.fixture
def mock_repository():
    return MagicMock(spec=PricingRepository)


def test_pricing_action_executor_update_price_success(mock_pricing_port, mock_repository, channel):
    executor = PricingActionExecutor(
        pricing_port=mock_pricing_port,
        repository=mock_repository,
        default_channel=channel,
    )

    mock_pricing_port.update_price.return_value = PricingResult(
        pricing_id="MLC12345",
        channel=channel,
        status=PricingStatus.APPLIED,
        listing_id="MLC12345",
        applied_price=Decimal("13500"),
        previous_price=Decimal("15000"),
        currency="CLP",
        request_id="req_test_1",
        idempotency_key="idemp_test_1",
        correlation_id="corr_test_1",
    )

    action = PricingAction(
        action_id="act_001",
        decision_id="dec_test_1",
        listing_id="MLC12345",
        channel=channel,
        current_price=Decimal("15000"),
        proposed_price=Decimal("13500"),
        reason=PriceChangeReason.COMPETITIVE_MATCH,
        idempotency_key="idemp_test_1",
        correlation_id="corr_test_1",
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Executing approved pricing update",
        parameters={
            "action_type": "UPDATE_PRICE",
            "pricing_action": action,
            "channel": channel,
        },
    )
    state = LoopState(mission_id="m_001", iteration=1, goal="test_pricing")

    result_dict = executor.execute(decision, state)

    assert result_dict["status"] == "APPLIED"
    assert result_dict["is_success"] is True
    assert result_dict["applied_price"] == 13500.0
    assert result_dict["previous_price"] == 15000.0
    assert result_dict["idempotency_key"] == "idemp_test_1"
    assert result_dict["correlation_id"] == "corr_test_1"
    assert executor.external_calls_count == 1
    mock_repository.save_result.assert_called_once()


def test_pricing_action_executor_verify_price(mock_pricing_port, channel):
    executor = PricingActionExecutor(
        pricing_port=mock_pricing_port,
        default_channel=channel,
    )

    mock_pricing_port.get_current_price.return_value = PricingResult(
        pricing_id="MLC12345",
        channel=channel,
        status=PricingStatus.APPLIED,
        listing_id="MLC12345",
        applied_price=Decimal("13500"),
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Verify price on marketplace",
        parameters={
            "action_type": "VERIFY_PRICE",
            "listing_id": "MLC12345",
            "channel": channel,
        },
    )
    state = LoopState(mission_id="m_001", iteration=2, goal="test_pricing")

    result_dict = executor.execute(decision, state)

    assert result_dict["status"] == "APPLIED"
    assert result_dict["is_success"] is True
    assert result_dict["current_price"] == 13500.0
    mock_pricing_port.get_current_price.assert_called_once_with(channel=channel, listing_id="MLC12345")
