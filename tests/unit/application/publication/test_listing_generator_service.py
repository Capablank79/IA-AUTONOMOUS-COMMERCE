import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
)
from src.domain.publication.generation_models import (
    ListingGenerationInput,
    ListingGenerationResult,
    ListingFactGrounding,
    SEOStrategy,
    DifferentiationStrategy,
)
from src.domain.publication.ports import ListingGeneratorPort
from src.application.publication.listing_generator_service import ListingDraftGeneratorService


class TestListingDraftGeneratorService:
    @pytest.fixture
    def channel(self):
        return SalesChannel(
            channel_id="CH_MERCADOLIBRE_CL",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
            currency="CLP",
        )

    @pytest.fixture
    def input_data(self, channel):
        return ListingGenerationInput(
            product_id="P-100",
            title="Teclado Mecánico RGB",
            price=Decimal("45000"),
            currency="CLP",
            available_quantity=8,
            channel=channel,
            attributes={"switch": "Red Silent", "layout": "ISO Español"},
        )

    def test_service_with_default_deterministic_generator(self, input_data):
        service = ListingDraftGeneratorService()
        result = service.generate_listing_draft(input_data)

        assert isinstance(result, ListingGenerationResult)
        assert result.draft.product_reference_id == "P-100"
        assert result.draft.title.startswith("Teclado Mecánico RGB")
        assert "switch" in result.grounding.verified_attributes
        assert result.confidence == Confidence.HIGH

    def test_service_with_custom_mock_generator(self, input_data, channel):
        mock_generator = MagicMock(spec=ListingGeneratorPort)
        mock_draft = ListingDraft(
            draft_id="DRAFT-CUSTOM-1",
            product_reference_id="P-100",
            title="Custom Mock Title",
            description="Custom Description",
            price=Decimal("45000"),
            currency="CLP",
            available_quantity=8,
            channel=channel,
        )
        mock_result = ListingGenerationResult(
            draft=mock_draft,
            grounding=ListingFactGrounding(),
            seo_strategy=SEOStrategy(),
            differentiation_strategy=DifferentiationStrategy(),
            confidence=Confidence.HIGH,
        )
        mock_generator.generate.return_value = mock_result

        service = ListingDraftGeneratorService(generator=mock_generator)
        res = service.generate_listing_draft(input_data)

        mock_generator.generate.assert_called_once_with(input_data)
        assert res.draft.draft_id == "DRAFT-CUSTOM-1"
        assert res.draft.title == "Custom Mock Title"

    def test_service_invalid_input_raises(self):
        service = ListingDraftGeneratorService()
        with pytest.raises(ValueError, match="input_data cannot be None"):
            service.generate_listing_draft(None)
