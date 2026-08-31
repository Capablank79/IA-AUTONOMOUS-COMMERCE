import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from src.domain.market_intelligence.models import (
    Confidence,
    MarketListing,
    Marketplace,
    Money,
    MarketEvidence,
    TrendSignal,
    ReviewSignal,
)
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
)
from src.domain.publication.generation_models import (
    ListingGenerationInput,
    SEOStrategy,
    SEOKeyword,
    KeywordSourceType,
    CustomerPainPoint,
    CustomerPainCategory,
    DifferentiationStrategy,
    ChannelContentConstraint,
    ClaimProvenance,
    ClaimProvenanceType,
    ListingFactGrounding,
)
from src.domain.publication.services import (
    DeterministicListingGenerator,
    CustomerPainMiningEngine,
)
from src.domain.publication.validation_models import (
    ValidationStatus,
    FindingSeverity,
    ListingValidationContext,
    ListingValidationResult,
)
from src.domain.publication.validation_engine import DeterministicListingValidator
from src.application.publication.listing_validator_service import ListingQualityValidatorService
from src.application.tool.catalog import register_standard_commerce_tools
from src.domain.tool.registry import ToolRegistry


class TestListingValidatorIntegration:
    @pytest.fixture
    def channel(self):
        return SalesChannel(
            channel_id="ml_cl",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
        )

    def test_full_pipeline_g1_to_g2(self, channel):
        # Step 1: Run G.1 Generator
        gen_input = ListingGenerationInput(
            product_id="prod_compresor_01",
            title="Mini Compresor de Aire Portatil Digital",
            price=Decimal("22990"),
            currency="CLP",
            available_quantity=25,
            channel=channel,
            category_id="MLC7890",
            attributes={
                "brand": "AirPro",
                "max_psi": 150,
                "voltage": 12,
                "display": "digital LCD",
                "accuracy": "calibrado +-0.5 PSI",
                "condition": "new",
            },
            images=("https://img.cdn.com/comp1.jpg", "https://img.cdn.com/comp2.jpg"),
            customer_pains=(
                CustomerPainPoint(
                    pain_id="p1",
                    category=CustomerPainCategory.QUALITY,
                    complaint_summary="El manometro no marca bien la presion",
                    severity=8,
                ),
            ),
            seo_keywords=(
                SEOKeyword(
                    keyword="inflador electrico bicicleta",
                    source_type=KeywordSourceType.OBSERVED,
                    relevance_score=0.9,
                    search_volume_observed=2000,
                ),
            ),
            constraints=ChannelContentConstraint(max_title_length=60),
        )

        generator = DeterministicListingGenerator()
        gen_result = generator.generate(gen_input)

        draft = gen_result.draft
        assert draft.product_reference_id == "prod_compresor_01"

        # Step 2: Run G.2 Validator
        validator_service = ListingQualityValidatorService()
        validation_context = ListingValidationContext(
            draft=draft,
            product_truth_attributes=dict(gen_input.attributes),
            grounding=gen_result.grounding,
            seo_strategy=gen_result.seo_strategy,
            differentiation_strategy=gen_result.differentiation_strategy,
            channel_constraints=gen_input.constraints,
        )

        val_result = validator_service.validate_listing(validation_context)

        # Assertions
        assert val_result.status == ValidationStatus.VALID
        assert val_result.is_valid is True
        assert len(val_result.violations) == 0
        assert val_result.quality_score.overall_score >= 80.0
        assert val_result.quality_score.factuality_score == 100.0
        assert val_result.quality_score.policy_compliance_score == 100.0

    def test_pipeline_catches_unsupported_claim_injection(self, channel):
        # Malicious / hallucinated claim added into draft
        tampered_draft = ListingDraft(
            draft_id="draft_tampered",
            product_reference_id="prod_compresor_01",
            channel=channel,
            title="Compresor 150PSI Con Garantia Vitalicia y Bateria Nuclear",
            description="Inflador portatil que nunca se descarga y dura 50 anos.",
            price=Decimal("22990"),
            currency="CLP",
            available_quantity=10,
            category_id="MLC7890",
        )

        validator_service = ListingQualityValidatorService()
        val_context = ListingValidationContext(
            draft=tampered_draft,
            product_truth_attributes={"brand": "AirPro", "max_psi": 150},
        )

        val_result = validator_service.validate_listing(val_context)
        assert val_result.status in (ValidationStatus.BLOCKED, ValidationStatus.INVALID)
        assert val_result.is_valid is False
        assert any(f.code in ("PROHIBITED_CLAIM_TERM", "UNGROUNDED_CRITICAL_ATTRIBUTE", "PRODUCT_TRUTH_MISMATCH", "UNVERIFIED_ATTRIBUTE_CLAIM", "IMAGES_MISSING") for f in val_result.violations)

    def test_tool_catalog_includes_g2_validator(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)

        tool = registry.get("validate_listing_quality")
        assert tool is not None
        assert tool.tool_id == "validate_listing_quality"
        assert tool.capability == "LISTING_VALIDATION"
        assert "draft_id" in [f.name for f in tool.input_contract.fields]
        assert "status" in [f.name for f in tool.output_contract.fields]
        assert "is_valid" in [f.name for f in tool.output_contract.fields]
