import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping
from types import MappingProxyType

from src.domain.market_intelligence.models import (
    Confidence,
    MarketListing,
    Marketplace,
    Money,
    MarketEvidence,
)
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
)
from src.domain.publication.generation_models import (
    KeywordSourceType,
    SEOKeyword,
    SEOStrategy,
    CustomerPainCategory,
    CustomerPainPoint,
    UnmetNeed,
    DifferentiationStrategy,
    ClaimProvenanceType,
    ClaimProvenance,
    ListingFactGrounding,
    ChannelContentConstraint,
)
from src.domain.publication.validation_models import (
    ValidationStatus,
    FindingSeverity,
    ValidationDimension,
    ValidationFinding,
    QualityScoreBreakdown,
    ListingValidationContext,
    ListingValidationResult,
)
from src.domain.publication.validation_engine import DeterministicListingValidator


class TestValidationModels:
    def test_validation_finding_creation(self):
        finding = ValidationFinding(
            dimension=ValidationDimension.TITLE_CONSTRAINTS,
            severity=FindingSeverity.ERROR,
            code="TITLE_TOO_LONG",
            message="Title exceeds max length 60",
            field_name="title",
            details={"actual": 65, "max": 60},
            suggested_action="Shorten title",
        )
        assert finding.dimension == ValidationDimension.TITLE_CONSTRAINTS
        assert finding.severity == FindingSeverity.ERROR
        assert finding.code == "TITLE_TOO_LONG"
        assert finding.field_name == "title"
        assert finding.suggested_action == "Shorten title"

    def test_validation_finding_invalid_raises(self):
        with pytest.raises(ValueError, match="ValidationFinding code cannot be empty"):
            ValidationFinding(
                dimension=ValidationDimension.REQUIRED_FIELDS,
                severity=FindingSeverity.ERROR,
                code="",
                message="some message",
            )
        with pytest.raises(ValueError, match="ValidationFinding message cannot be empty"):
            ValidationFinding(
                dimension=ValidationDimension.REQUIRED_FIELDS,
                severity=FindingSeverity.ERROR,
                code="ERR",
                message="   ",
            )

    def test_quality_score_breakdown_ranges(self):
        breakdown = QualityScoreBreakdown(
            completeness_score=100.0,
            factuality_score=100.0,
            seo_score=85.0,
            readability_score=90.0,
            policy_compliance_score=100.0,
            differentiation_score=80.0,
            channel_compliance_score=100.0,
            overall_score=94.5,
        )
        assert breakdown.overall_score == 94.5

        with pytest.raises(ValueError, match="Quality score components must be between 0.0 and 100.0"):
            QualityScoreBreakdown(
                completeness_score=150.0,
                factuality_score=100.0,
                seo_score=85.0,
                readability_score=90.0,
                policy_compliance_score=100.0,
                differentiation_score=80.0,
                channel_compliance_score=100.0,
                overall_score=94.5,
            )

    def test_listing_validation_result_immutability(self):
        score = QualityScoreBreakdown(
            completeness_score=100.0,
            factuality_score=100.0,
            seo_score=100.0,
            readability_score=100.0,
            policy_compliance_score=100.0,
            differentiation_score=100.0,
            channel_compliance_score=100.0,
            overall_score=100.0,
        )
        result = ListingValidationResult(
            draft_id="draft_123",
            channel_id="MERCADO_LIBRE",
            status=ValidationStatus.VALID,
            is_valid=True,
            quality_score=score,
        )
        assert result.draft_id == "draft_123"
        assert result.is_valid is True
        assert result.status == ValidationStatus.VALID


class TestDeterministicListingValidator:
    @pytest.fixture
    def valid_draft(self):
        return ListingDraft(
            draft_id="draft_001",
            product_reference_id="prod_100",
            channel=SalesChannel(
                channel_id="ml_cl",
                channel_type=SalesChannelType.MARKETPLACE,
                name="Mercado Libre Chile",
            ),
            title="Aspiradora Portatil Inalambrica 120W Potente Filtro HEPA",
            description="Aspiradora portatil de alta potencia con motor de 120W y bateria recargable de 2000mAh. Incluye filtro HEPA lavable para maxima retencion de polvo.",
            price=Decimal("24990"),
            currency="CLP",
            available_quantity=15,
            category_id="MLC12345",
            attributes={
                "brand": "PowerClean",
                "model": "HV-120",
                "power_watts": 120,
                "battery_mah": 2000,
            },
            images=("https://img.cdn.com/item1.jpg", "https://img.cdn.com/item2.jpg"),
        )

    @pytest.fixture
    def valid_grounding(self):
        return ListingFactGrounding(
            verified_attributes={"power_watts": 120, "battery_mah": 2000, "brand": "PowerClean", "model": "HV-120"},
            claims_provenance=(
                ClaimProvenance(
                    claim_text="Motor de 120W",
                    provenance_type=ClaimProvenanceType.DERIVED,
                    source_field="spec_sheet",
                    confidence=Confidence.HIGH,
                ),
            ),
        )

    def test_valid_listing_passes_validation(self, valid_draft, valid_grounding):
        validator = DeterministicListingValidator()
        context = ListingValidationContext(
            draft=valid_draft,
            product_truth_attributes={"brand": "PowerClean", "model": "HV-120", "power_watts": 120, "battery_mah": 2000},
            grounding=valid_grounding,
            channel_constraints=ChannelContentConstraint(max_title_length=60),
        )
        result = validator.validate(context)
        assert result.status == ValidationStatus.VALID
        assert result.is_valid is True
        assert len(result.violations) == 0
        assert result.quality_score.overall_score >= 80.0

    def test_title_length_exceeded_raises_finding(self, valid_draft):
        long_title_draft = ListingDraft(
            draft_id="draft_long",
            product_reference_id="prod_100",
            channel=valid_draft.channel,
            title="A" * 75,
            description=valid_draft.description,
            price=valid_draft.price,
            currency="CLP",
            available_quantity=10,
            category_id="MLC123",
        )
        validator = DeterministicListingValidator()
        context = ListingValidationContext(
            draft=long_title_draft,
            channel_constraints=ChannelContentConstraint(max_title_length=60),
        )
        result = validator.validate(context)
        assert result.is_valid is False
        assert any(f.code == "TITLE_LENGTH_EXCEEDED" for f in result.violations)

    def test_prohibited_terms_in_title_or_description_blocks(self, valid_draft):
        bad_draft = ListingDraft(
            draft_id="draft_prohibited",
            product_reference_id="prod_100",
            channel=valid_draft.channel,
            title="Aspiradora 120W El Mejor Precio 100% Garantizado",
            description="La mejor aspiradora cura el asma y destruye bacterias de forma milagrosa.",
            price=valid_draft.price,
            currency="CLP",
            available_quantity=10,
            category_id="MLC123",
        )
        validator = DeterministicListingValidator()
        context = ListingValidationContext(draft=bad_draft)
        result = validator.validate(context)
        assert result.status == ValidationStatus.BLOCKED
        assert result.is_valid is False
        assert any(f.code == "PROHIBITED_CLAIM_TERM" for f in result.violations)
        assert any(f.code == "UNAUTHORIZED_MEDICAL_CLAIM" for f in result.violations)

    def test_unsupported_product_truth_attributes_blocks(self, valid_draft):
        # Listing claims battery 5000mah but product truth says 2000mah
        bad_attr_draft = ListingDraft(
            draft_id="draft_fake_attr",
            product_reference_id="prod_100",
            channel=valid_draft.channel,
            title="Aspiradora Portatil Inalambrica 120W",
            description="Aspiradora con bateria de 5000mAh",
            price=valid_draft.price,
            currency="CLP",
            available_quantity=10,
            category_id="MLC123",
            attributes={"battery_mah": 5000},
        )
        validator = DeterministicListingValidator()
        context = ListingValidationContext(
            draft=bad_attr_draft,
            product_truth_attributes={"battery_mah": 2000, "brand": "PowerClean"},
        )
        result = validator.validate(context)
        assert result.status == ValidationStatus.BLOCKED
        assert result.is_valid is False
        assert any(f.code == "PRODUCT_TRUTH_MISMATCH" for f in result.violations)

    def test_negative_or_zero_price_inventory_blocks(self, valid_draft):
        bad_price_draft = ListingDraft(
            draft_id="draft_bad_price",
            product_reference_id="prod_100",
            channel=valid_draft.channel,
            title="Aspiradora Portatil Inalambrica 120W",
            description="Aspiradora potente",
            price=Decimal("100"),
            currency="CLP",
            available_quantity=0,
            category_id="MLC123",
        )
        validator = DeterministicListingValidator()
        context = ListingValidationContext(draft=bad_price_draft)
        result = validator.validate(context)
        assert any(f.code == "INVENTORY_ZERO_OUT_OF_STOCK" for f in result.warnings)

    def test_duplicate_content_against_existing_catalog_flags_warning_or_blocker(self, valid_draft):
        validator = DeterministicListingValidator()
        context = ListingValidationContext(
            draft=valid_draft,
            product_truth_attributes={"brand": "PowerClean", "model": "HV-120", "power_watts": 120, "battery_mah": 2000},
            existing_catalog_titles=(
                "Aspiradora Portatil Inalambrica 120W Potente Filtro HEPA", # Exact duplicate
            ),
        )
        result = validator.validate(context)
        assert any(f.code == "EXACT_DUPLICATE_LISTING_TITLE" for f in result.violations)
        assert result.status == ValidationStatus.INVALID

    def test_keyword_stuffing_detection(self, valid_draft):
        stuffed_draft = ListingDraft(
            draft_id="draft_stuffed",
            product_reference_id="prod_100",
            channel=valid_draft.channel,
            title="Aspiradora aspiradora aspiradora aspiradora aspiradora aspiradora",
            description="aspiradora aspiradora aspiradora aspiradora aspiradora aspiradora aspiradora aspiradora aspiradora",
            price=valid_draft.price,
            currency="CLP",
            available_quantity=10,
            category_id="MLC123",
        )
        validator = DeterministicListingValidator()
        context = ListingValidationContext(draft=stuffed_draft)
        result = validator.validate(context)
        assert any(f.code == "KEYWORD_STUFFING_DETECTED" for f in result.violations)

    def test_customer_pain_differentiation_unsupported_claim(self, valid_draft):
        diff_strategy = DifferentiationStrategy(
            unmet_needs_addressed=(
                UnmetNeed(
                    need_id="n1",
                    description="Mayor duracion de bateria",
                ),
            ),
            differential_claims=(
                "Bateria de grafeno reforzada con 10 horas de duracion",
            ),
            evidence_backed=False,
            product_truth_mapping={"pain_category_battery": "attr:battery_mah=5000"},
        )
        validator = DeterministicListingValidator()
        context = ListingValidationContext(
            draft=valid_draft,
            product_truth_attributes={"brand": "PowerClean", "power_watts": 120, "battery_mah": 2000},
            differentiation_strategy=diff_strategy,
        )
        result = validator.validate(context)
        assert result.status == ValidationStatus.BLOCKED
        assert any(f.code == "DIFFERENTIATION_CLAIM_UNBACKED" for f in result.violations)
