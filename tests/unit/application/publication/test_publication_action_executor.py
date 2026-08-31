import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Optional, Dict, Any
from types import MappingProxyType
import uuid

from src.domain.mission.models import (
    LoopDecision,
    LoopState,
    LoopAction,
    MissionType,
    LoopTraceEntry,
)
from src.domain.mission.ports import ActionExecutor, DecisionProvider
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits, LoopResult
from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    PublicationStatus,
    PublicationErrorCategory,
    PublicationError,
    ListingDraft,
    PublicationRequest,
    PublicationResult,
)
from src.domain.publication.ports import (
    PublicationPort,
    PublicationRepository,
)
from src.application.publication.publication_action_executor import PublicationActionExecutor


class MockPublicationPort(PublicationPort):
    def __init__(self, response_result: Optional[PublicationResult] = None):
        self.response_result = response_result
        self.last_request: Optional[PublicationRequest] = None
        self.get_status_calls: list = []

    def publish(self, request: PublicationRequest) -> PublicationResult:
        self.last_request = request
        if self.response_result:
            return self.response_result
        return PublicationResult(
            publication_id="PUB-12345",
            channel=request.channel,
            status=PublicationStatus.PUBLISHED,
            external_reference="MLC123456789",
            permalink="https://articulo.mercadolibre.cl/MLC-123456789",
            published_at=datetime.now(timezone.utc),
            confidence=Confidence.HIGH,
        )

    def get_status(self, channel: SalesChannel, external_reference: str) -> PublicationResult:
        self.get_status_calls.append((channel, external_reference))
        if self.response_result:
            return self.response_result
        return PublicationResult(
            publication_id="PUB-12345",
            channel=channel,
            status=PublicationStatus.PUBLISHED,
            external_reference=external_reference,
            permalink=f"https://articulo.mercadolibre.cl/{external_reference}",
            confidence=Confidence.HIGH,
        )


class MockPublicationRepository(PublicationRepository):
    def __init__(self):
        self.drafts: Dict[str, ListingDraft] = {}
        self.results: Dict[str, PublicationResult] = {}

    def save_draft(self, draft: ListingDraft) -> None:
        self.drafts[draft.draft_id] = draft

    def get_draft(self, draft_id: str) -> Optional[ListingDraft]:
        return self.drafts.get(draft_id)

    def save_result(self, result: PublicationResult) -> None:
        if result.publication_id:
            self.results[result.publication_id] = result

    def get_result_by_id(self, publication_id: str) -> Optional[PublicationResult]:
        return self.results.get(publication_id)

    def get_result_by_external_reference(
        self, channel_id: str, external_reference: str
    ) -> Optional[PublicationResult]:
        for res in self.results.values():
            if res.channel.channel_id == channel_id and res.external_reference == external_reference:
                return res
        return None


@pytest.fixture
def sample_channel() -> SalesChannel:
    return SalesChannel(
        channel_id="CH_MERCADOLIBRE_CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
        region="CL",
        currency="CLP",
    )


@pytest.fixture
def sample_draft(sample_channel: SalesChannel) -> ListingDraft:
    return ListingDraft(
        draft_id="DRAFT-999",
        product_reference_id="PROD-ABC",
        title="Audífonos Inalámbricos Bluetooth Pro",
        description="Audífonos de alta fidelidad con cancelación de ruido",
        price=Decimal("29990"),
        currency="CLP",
        available_quantity=50,
        channel=sample_channel,
        images=("https://img.domain.com/photo1.jpg",),
        sku="SKU-AUD-01",
    )


@pytest.fixture
def sample_state() -> LoopState:
    return LoopState(
        mission_id="mission_pub_001",
        iteration=1,
        goal="Publish validated listing to sales channel",
    )


class TestPublicationActionExecutor:
    """A. Action Creation & Basic Validations"""

    def test_executor_requires_port(self):
        with pytest.raises(ValueError, match="publication_port cannot be None"):
            PublicationActionExecutor(publication_port=None)  # type: ignore

    def test_implements_action_executor_port(self):
        port = MockPublicationPort()
        executor = PublicationActionExecutor(publication_port=port)
        assert isinstance(executor, ActionExecutor)

    """B. Action Execution & Propagation"""

    def test_successful_publish_execution(self, sample_channel: SalesChannel, sample_draft: ListingDraft, sample_state: LoopState):
        port = MockPublicationPort()
        repo = MockPublicationRepository()
        executor = PublicationActionExecutor(publication_port=port, repository=repo)

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publishing vetted listing",
            parameters={
                "action_type": "PUBLISH_LISTING",
                "draft": sample_draft,
                "correlation_id": "corr-12345",
                "idempotency_key": "idemp-custom-key-1",
            },
        )

        obs = executor.execute(decision, sample_state)

        # Port invocation verification
        assert port.last_request is not None
        assert port.last_request.draft.draft_id == "DRAFT-999"
        assert port.last_request.channel.channel_id == "CH_MERCADOLIBRE_CL"
        assert port.last_request.correlation_id == "corr-12345"
        assert port.last_request.idempotency_key == "idemp-custom-key-1"

        # Observation result
        assert obs["action_executed"] == "PUBLISH"
        assert obs["status"] == PublicationStatus.PUBLISHED.value
        assert obs["is_success"] is True
        assert obs["is_unknown"] is False
        assert obs["is_failed"] is False
        assert obs["external_reference"] == "MLC123456789"
        assert obs["publication_id"] == "PUB-12345"
        assert obs["correlation_id"] == "corr-12345"
        assert obs["idempotency_key"] == "idemp-custom-key-1"

        # Repository persistence
        assert repo.get_draft("DRAFT-999") is not None
        assert repo.get_result_by_id("PUB-12345") is not None
        assert executor.external_calls_count == 1

    """C. Failure Handling"""

    def test_publish_failure_propagates_correctly(self, sample_channel: SalesChannel, sample_draft: ListingDraft, sample_state: LoopState):
        fail_result = PublicationResult(
            publication_id=None,
            channel=sample_channel,
            status=PublicationStatus.FAILED,
            errors=(
                PublicationError(
                    category=PublicationErrorCategory.VALIDATION,
                    message="Missing required attribute: COLOR",
                    code="ERR_ATTR_COLOR_REQUIRED",
                    retryable=False,
                ),
            ),
        )
        port = MockPublicationPort(response_result=fail_result)
        executor = PublicationActionExecutor(publication_port=port)

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Attempt publishing",
            parameters={
                "action_type": "PUBLISH",
                "draft": sample_draft,
            },
        )

        obs = executor.execute(decision, sample_state)

        assert obs["status"] == PublicationStatus.FAILED.value
        assert obs["is_success"] is False
        assert obs["is_failed"] is True
        assert obs["is_unknown"] is False
        assert len(obs["errors"]) == 1
        assert obs["errors"][0]["category"] == "VALIDATION"
        assert obs["errors"][0]["message"] == "Missing required attribute: COLOR"

    """D. UNKNOWN State Handling (Non-destructive Uncertainty)"""

    def test_unknown_status_preserved_without_converting_to_failed(self, sample_channel: SalesChannel, sample_draft: ListingDraft, sample_state: LoopState):
        unknown_result = PublicationResult(
            publication_id=None,
            channel=sample_channel,
            status=PublicationStatus.UNKNOWN,
            errors=(
                PublicationError(
                    category=PublicationErrorCategory.TIMEOUT,
                    message="Upstream request timed out after 30s; outcome uncertain",
                    code="ERR_TIMEOUT_UNCERTAIN",
                    retryable=True,
                ),
            ),
            confidence=Confidence.LOW,
        )
        port = MockPublicationPort(response_result=unknown_result)
        executor = PublicationActionExecutor(publication_port=port)

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publish listing to channel",
            parameters={
                "action_type": "PUBLISH",
                "draft": sample_draft,
            },
        )

        obs = executor.execute(decision, sample_state)

        # Critical: UNKNOWN must NOT be converted into FAILED
        assert obs["status"] == PublicationStatus.UNKNOWN.value
        assert obs["is_unknown"] is True
        assert obs["is_failed"] is False
        assert obs["is_success"] is False
        assert obs["confidence"] == "LOW"
        assert obs["errors"][0]["category"] == "TIMEOUT"
        assert obs["errors"][0]["retryable"] is True

    """E. Status Verification for UNKNOWN Recovery"""

    def test_verify_status_action(self, sample_channel: SalesChannel, sample_state: LoopState):
        verified_result = PublicationResult(
            publication_id="PUB-RECOVERED-99",
            channel=sample_channel,
            status=PublicationStatus.PUBLISHED,
            external_reference="EXT-REC-99",
            permalink="https://articulo.mercadolibre.cl/EXT-REC-99",
            confidence=Confidence.HIGH,
        )
        port = MockPublicationPort(response_result=verified_result)
        executor = PublicationActionExecutor(publication_port=port, default_channel=sample_channel)

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Checking status after previous timeout",
            parameters={
                "action_type": "VERIFY_STATUS",
                "external_reference": "EXT-REC-99",
            },
        )

        obs = executor.execute(decision, sample_state)

        assert port.get_status_calls == [(sample_channel, "EXT-REC-99")]
        assert obs["action_executed"] == "VERIFY_STATUS"
        assert obs["status"] == PublicationStatus.PUBLISHED.value
        assert obs["is_success"] is True
        assert obs["publication_id"] == "PUB-RECOVERED-99"

    """F. Idempotency & Correlation ID Preservation"""

    def test_idempotency_key_and_correlation_id_preservation(self, sample_draft: ListingDraft, sample_state: LoopState):
        port = MockPublicationPort()
        executor = PublicationActionExecutor(publication_port=port)

        # 1. With explicit keys
        decision1 = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publish 1",
            parameters={
                "action_type": "PUBLISH",
                "draft": sample_draft,
                "idempotency_key": "IDEMP-EXPLICIT-1",
                "correlation_id": "CORR-EXPLICIT-1",
            },
        )
        executor.execute(decision1, sample_state)
        assert port.last_request.idempotency_key == "IDEMP-EXPLICIT-1"
        assert port.last_request.correlation_id == "CORR-EXPLICIT-1"

        # 2. With fallback to state mission_id and draft_id (no random mutations)
        decision2 = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publish 2",
            parameters={
                "action_type": "PUBLISH",
                "draft": sample_draft,
            },
        )
        executor.execute(decision2, sample_state)
        assert port.last_request.idempotency_key == f"idemp_{sample_draft.draft_id}"
        assert port.last_request.correlation_id == sample_state.mission_id

    """G. Missing Parameters Validation"""

    def test_missing_draft_returns_failed_observation(self, sample_state: LoopState):
        port = MockPublicationPort()
        executor = PublicationActionExecutor(publication_port=port)

        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publish without draft",
            parameters={"action_type": "PUBLISH"},
        )
        obs = executor.execute(decision, sample_state)
        assert obs["status"] == PublicationStatus.FAILED.value
        assert "Missing required 'draft'" in obs["error"]

    """H. Integration with AutonomousLoop"""

    def test_integration_with_autonomous_loop(self, sample_channel: SalesChannel, sample_draft: ListingDraft):
        port = MockPublicationPort()
        executor = PublicationActionExecutor(publication_port=port)

        class MockPublicationDecisionProvider(DecisionProvider):
            def __init__(self, draft: ListingDraft):
                self.draft = draft
                self.step = 0

            def decide(self, state: LoopState) -> LoopDecision:
                self.step += 1
                if self.step == 1:
                    return LoopDecision(
                        action=LoopAction.CONTINUE,
                        reason="Publishing approved draft",
                        parameters={
                            "action_type": "PUBLISH",
                            "draft": self.draft,
                            "correlation_id": state.mission_id,
                        },
                    )
                else:
                    return LoopDecision(
                        action=LoopAction.COMPLETE,
                        reason="Listing successfully published and confirmed",
                        confidence=1.0,
                    )

        provider = MockPublicationDecisionProvider(sample_draft)
        loop = AutonomousLoop(
            decision_provider=provider,
            action_executor=executor,
            max_iterations=3,
        )

        result: LoopResult = loop.run(
            mission_id="mission_e01_pub_test",
            goal="Publish product listing to Mercado Libre Chile",
        )

        assert result.status == "COMPLETED"
        assert len(result.trace) == 2
        # Verify first step executed publication
        first_step_trace = result.trace[0]
        assert first_step_trace.observation["status"] == "PUBLISHED"
        assert first_step_trace.observation["draft_id"] == "DRAFT-999"
        assert first_step_trace.observation["external_reference"] == "MLC123456789"
        assert executor.external_calls_count == 1
