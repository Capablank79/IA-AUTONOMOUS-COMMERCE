import pytest
from decimal import Decimal
from datetime import datetime, timezone

from src.domain.market_intelligence.models import Confidence, Money
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
    PublicationRequest,
    PublicationStatus,
)
from src.domain.publication.validation_models import (
    ValidationStatus,
    ListingValidationContext,
    ListingValidationResult,
    QualityScoreBreakdown,
)
from src.domain.publication.validation_engine import DeterministicListingValidator
from src.domain.policy.models import (
    PolicyEvaluationContext,
    PolicyDecisionType,
)
from src.domain.mission.models import LoopDecision, LoopAction
from src.domain.policy.engine import PolicyEngine


class TestListingValidatorPolicySecurityBoundary:
    @pytest.fixture
    def channel(self):
        return SalesChannel(
            channel_id="ml_cl",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
        )

    def test_blocked_listing_cannot_proceed_to_publication(self, channel):
        # Listing containing prohibited medical claim
        blocked_draft = ListingDraft(
            draft_id="draft_prohibited_cure",
            product_reference_id="prod_med_01",
            channel=channel,
            title="Aparato Laser Terapeutico Cura Artritis y Diabetes",
            description="Dispositivo milagroso que cura cualquier enfermedad.",
            price=Decimal("45000"),
            currency="CLP",
            available_quantity=10,
            category_id="MLC999",
        )

        validator = DeterministicListingValidator()
        val_result = validator.validate(ListingValidationContext(draft=blocked_draft))

        # 1. G.2 Validator asserts BLOCKED and invalid
        assert val_result.status == ValidationStatus.BLOCKED
        assert val_result.is_valid is False
        assert len(val_result.violations) > 0

        # 2. Verify Policy Engine rejects or guards action if status is not valid
        policy_engine = PolicyEngine()
        context = PolicyEvaluationContext(
            action_type="PUBLISH_LISTING",
            actor_id="test_agent",
            mission_id="m_001",
            correlation_id="c_001",
            loop_decision=LoopDecision(action=LoopAction.CONTINUE, reason="Attempt publish"),
            is_external_impact=True,
            custom_context={
                "draft_id": blocked_draft.draft_id,
                "validation_status": val_result.status.value,
                "is_valid": val_result.is_valid,
            },
        )
        decision = policy_engine.evaluate(context)
        assert val_result.status != ValidationStatus.VALID

    def test_unknown_critical_evidence_forces_needs_review_or_blocked(self, channel):
        # Product draft has unknown brand and unknown power attribute
        draft = ListingDraft(
            draft_id="draft_unknown_brand",
            product_reference_id="prod_unknown_01",
            channel=channel,
            title="Taladro Inalambrico 18V Impacto",
            description="Taladro potente para trabajo pesado.",
            price=Decimal("35000"),
            currency="CLP",
            available_quantity=5,
            category_id="MLC888",
            attributes={"brand": "SuperDrill", "voltage": 18},
        )

        validator = DeterministicListingValidator()
        # Product truth is empty -> attributes are unverified/unknown
        val_result = validator.validate(
            ListingValidationContext(
                draft=draft,
                product_truth_attributes={},
            )
        )

        assert val_result.status in (ValidationStatus.BLOCKED, ValidationStatus.NEEDS_REVIEW, ValidationStatus.INVALID)
        assert val_result.is_valid is False
        assert any(f.code == "UNGROUNDED_CRITICAL_ATTRIBUTE" for f in val_result.violations)
