import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from src.domain.market_intelligence.models import Confidence, Money
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
)
from src.domain.publication.validation_models import (
    ValidationStatus,
    ListingValidationContext,
    ListingValidationResult,
    QualityScoreBreakdown,
)
from src.application.publication.listing_validator_service import ListingQualityValidatorService


class TestListingQualityValidatorService:
    @pytest.fixture
    def mock_draft(self):
        return ListingDraft(
            draft_id="draft_srv_001",
            product_reference_id="prod_200",
            channel=SalesChannel(
                channel_id="ml_cl",
                channel_type=SalesChannelType.MARKETPLACE,
                name="ML Chile",
            ),
            title="Soporte Magnetico para Auto Celular Universal",
            description="Soporte magnetico para rejilla de ventilacion compatible con todo tipo de smartphone.",
            price=Decimal("9990"),
            currency="CLP",
            available_quantity=20,
            category_id="MLC45678",
        )

    def test_service_initialization_default_validator(self):
        service = ListingQualityValidatorService()
        assert service.validator is not None

    def test_service_delegates_to_custom_validator(self, mock_draft):
        mock_port = MagicMock()
        mock_score = QualityScoreBreakdown(
            completeness_score=100.0,
            factuality_score=100.0,
            seo_score=90.0,
            readability_score=95.0,
            policy_compliance_score=100.0,
            differentiation_score=85.0,
            channel_compliance_score=100.0,
            overall_score=96.0,
        )
        mock_result = ListingValidationResult(
            draft_id=mock_draft.draft_id,
            channel_id="ml_cl",
            status=ValidationStatus.VALID,
            is_valid=True,
            quality_score=mock_score,
        )
        mock_port.validate.return_value = mock_result

        service = ListingQualityValidatorService(validator=mock_port)
        context = ListingValidationContext(draft=mock_draft)
        result = service.validate_listing(context)

        assert result.is_valid is True
        assert result.status == ValidationStatus.VALID
        mock_port.validate.assert_called_once_with(context)

    def test_service_validation_input_guards(self):
        service = ListingQualityValidatorService()
        with pytest.raises(ValueError, match="context cannot be None"):
            service.validate_listing(None)

        with pytest.raises(ValueError, match="context.draft cannot be None"):
            # Empty mock context with None draft
            invalid_ctx = MagicMock()
            invalid_ctx.draft = None
            service.validate_listing(invalid_ctx)
