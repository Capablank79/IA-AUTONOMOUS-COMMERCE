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
    TrendSignal,
    ReviewSignal,
    Review,
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
    MultichannelContent,
    ListingGenerationInput,
    ListingGenerationResult,
)
from src.domain.publication.services import (
    CustomerPainMiningEngine,
    DeterministicListingGenerator,
)


class TestListingGenerationModels:
    def test_seo_keyword_validation(self):
        kw = SEOKeyword(
            keyword="mini aspiradora inalambrica",
            source_type=KeywordSourceType.OBSERVED,
            relevance_score=0.95,
            search_volume_observed=1500,
            provenance_id="trend_01",
        )
        assert kw.keyword == "mini aspiradora inalambrica"
        assert kw.source_type == KeywordSourceType.OBSERVED
        assert kw.relevance_score == 0.95
        assert kw.search_volume_observed == 1500

    def test_seo_keyword_invalid_raises(self):
        with pytest.raises(ValueError, match="SEOKeyword keyword cannot be empty"):
            SEOKeyword(keyword="  ", source_type=KeywordSourceType.PROPOSED)

        with pytest.raises(ValueError, match="relevance_score must be between 0.0 and 1.0"):
            SEOKeyword(keyword="test", source_type=KeywordSourceType.PROPOSED, relevance_score=1.5)

        with pytest.raises(ValueError, match="search_volume_observed cannot be negative"):
            SEOKeyword(keyword="test", source_type=KeywordSourceType.PROPOSED, search_volume_observed=-10)

    def test_customer_pain_point_validation(self):
        pain = CustomerPainPoint(
            pain_id="pain_01",
            category=CustomerPainCategory.BATTERY,
            complaint_summary="La batería dura menos de 10 minutos",
            frequency="FREQUENT",
            severity=9,
            evidence_count=5,
            source_review_ids=("rev_101", "rev_102"),
            confidence=Confidence.HIGH,
        )
        assert pain.category == CustomerPainCategory.BATTERY
        assert pain.severity == 9
        assert len(pain.source_review_ids) == 2

    def test_customer_pain_invalid_raises(self):
        with pytest.raises(ValueError, match="pain_id cannot be empty"):
            CustomerPainPoint(pain_id="", category=CustomerPainCategory.OTHER, complaint_summary="error")

        with pytest.raises(ValueError, match="complaint_summary cannot be empty"):
            CustomerPainPoint(pain_id="p1", category=CustomerPainCategory.OTHER, complaint_summary="  ")

        with pytest.raises(ValueError, match="severity must be between 1 and 10"):
            CustomerPainPoint(pain_id="p1", category=CustomerPainCategory.OTHER, complaint_summary="err", severity=15)

    def test_channel_constraint_defaults_and_validation(self):
        constraint = ChannelContentConstraint(max_title_length=60, max_description_length=4000)
        assert constraint.max_title_length == 60
        assert "el mejor" in constraint.forbidden_terms
        assert "100% garantizado" in constraint.forbidden_terms

        with pytest.raises(ValueError, match="max_title_length must be positive"):
            ChannelContentConstraint(max_title_length=0)


class TestCustomerPainMiningEngine:
    @pytest.fixture
    def review_signal(self):
        now = datetime.now(timezone.utc)
        return ReviewSignal(
            item_id="MLC-ITEM-123",
            total_reviews=3,
            average_rating=2.0,
            reviews=[
                Review(external_id="r1", rating=1, text="Muy mala batería, no dura nada la carga", date=now, reviewable_object="MLC-ITEM-123"),
                Review(external_id="r2", rating=2, text="El material de plástico es muy frágil", date=now, reviewable_object="MLC-ITEM-123"),
                Review(external_id="r3", rating=5, text="Excelente producto, llegó rápido", date=now, reviewable_object="MLC-ITEM-123"),
            ],
            paging={},
            observed_at=now,
            confidence=Confidence.HIGH,
        )

    def test_extract_pains_from_reviews(self, review_signal):
        engine = CustomerPainMiningEngine()
        pains = engine.extract_pains_from_reviews(review_signal)

        # Debe ignorar la review rating 5 y extraer sólo las negativas
        assert len(pains) == 2
        categories = {p.category for p in pains}
        assert CustomerPainCategory.BATTERY in categories
        assert CustomerPainCategory.QUALITY in categories

    def test_synthesize_differentiation_only_with_evidence(self):
        engine = CustomerPainMiningEngine()
        pains = (
            CustomerPainPoint(
                pain_id="p1",
                category=CustomerPainCategory.BATTERY,
                complaint_summary="Poca batería",
            ),
            CustomerPainPoint(
                pain_id="p2",
                category=CustomerPainCategory.QUALITY,
                complaint_summary="Material malo",
            ),
        )

        # Caso A: Nuestro producto tiene atributo de batería verificado ("battery_life": "45 minutos"), pero no de material
        verified_attrs = {"battery_life": "45 minutos", "color": "Negro"}
        diff = engine.synthesize_differentiation_strategy(pains, verified_attrs)

        # Debe generar unmet_needs para ambos, pero differential_claim SÓLO para batería
        assert len(diff.unmet_needs_addressed) == 2
        assert len(diff.differential_claims) == 1
        assert "45 minutos" in diff.differential_claims[0]
        assert "pain_category_BATTERY" in diff.product_truth_mapping
        assert "pain_category_QUALITY" not in diff.product_truth_mapping


class TestDeterministicListingGenerator:
    @pytest.fixture
    def channel(self):
        return SalesChannel(
            channel_id="CH_MERCADOLIBRE_CL",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
            currency="CLP",
        )

    @pytest.fixture
    def valid_input(self, channel):
        return ListingGenerationInput(
            product_id="PROD-ASPIRADORA-X1",
            title="Mini Aspiradora Portátil Recargable",
            brand="CleanTech",
            model="X1-Pro",
            price=Decimal("29990"),
            currency="CLP",
            available_quantity=20,
            channel=channel,
            attributes={
                "battery_life": "40 min uso continuo",
                "suction_power": "8000 Pa",
                "weight": "350g",
                "color": "Negro",
            },
            customer_pains=(
                CustomerPainPoint(
                    pain_id="cp1",
                    category=CustomerPainCategory.BATTERY,
                    complaint_summary="Competidores duran poco",
                    severity=8,
                ),
            ),
            seo_keywords=(
                SEOKeyword(
                    keyword="aspiradora auto",
                    source_type=KeywordSourceType.OBSERVED,
                    relevance_score=0.95,
                ),
            ),
        )

    def test_full_listing_generation_flow(self, valid_input):
        generator = DeterministicListingGenerator()
        result = generator.generate(valid_input)

        assert isinstance(result, ListingGenerationResult)
        assert isinstance(result.draft, ListingDraft)
        assert result.draft.product_reference_id == "PROD-ASPIRADORA-X1"
        assert result.draft.price == Decimal("29990")

        # Verificar título
        assert "CleanTech" in result.draft.title
        assert len(result.draft.title) <= 60

        # Grounding y Procedencia
        assert result.grounding is not None
        assert len(result.grounding.claims_provenance) > 0
        assert "battery_life" in result.grounding.verified_attributes

        # Diferenciación por dolor de cliente fundamentada
        assert len(result.differentiation_strategy.differential_claims) > 0
        assert "40 min uso continuo" in result.differentiation_strategy.differential_claims[0]

        # Variantes Multicanal generadas
        assert len(result.multichannel_variants) >= 2
        mkt_variant = next(v for v in result.multichannel_variants if v.channel_type == SalesChannelType.MARKETPLACE)
        social_variant = next(v for v in result.multichannel_variants if v.channel_type == SalesChannelType.SOCIAL_COMMERCE)
        assert mkt_variant.channel_id == "CH_MERCADOLIBRE_CL"
        assert social_variant.channel_id == "instagram_feed"
        assert "✨" in social_variant.body

    def test_forbidden_claims_sanitized_and_omitted(self, channel):
        # Input con término prohibido en atributos
        input_data = ListingGenerationInput(
            product_id="PROD-FAKE-01",
            title="Aspiradora 100% garantizado el mejor",
            price=Decimal("15000"),
            currency="CLP",
            available_quantity=5,
            channel=channel,
            attributes={
                "calidad": "100% garantizado el mejor del mercado",
                "potencia": "500W",
            },
        )
        generator = DeterministicListingGenerator()
        result = generator.generate(input_data)

        # El título generado debe haber sido sanitizado de "100% garantizado" y "el mejor"
        assert "100% garantizado" not in result.draft.title.lower()
        assert "el mejor" not in result.draft.title.lower()

        # El atributo con claim prohibido debe haber sido omitido de los bullets
        assert len(result.grounding.unsupported_claims_omitted) > 0
        assert not any("100% garantizado" in b for b in result.grounding.inferred_benefits)

    def test_missing_data_treated_as_unknown_or_omitted(self, channel):
        # Input con campos nulos o no provistos
        input_data = ListingGenerationInput(
            product_id="PROD-MINIMAL",
            title="Lámpara LED Escritorio",
            price=Decimal("12000"),
            currency="CLP",
            available_quantity=10,
            channel=channel,
            brand=None,
            model=None,
            attributes={},
        )
        generator = DeterministicListingGenerator()
        result = generator.generate(input_data)

        assert result.draft.title == "Lámpara LED Escritorio"
        assert "brand" not in result.grounding.verified_attributes
        assert "model" not in result.grounding.verified_attributes
        # No inventa bullets inexistentes
        assert len(result.differentiation_strategy.differential_claims) == 0

    def test_seo_strategy_with_market_evidence(self, channel):
        now = datetime.now(timezone.utc)
        listing = MarketListing(
            external_id="EXT-1",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Lampara Led",
            price=Money(amount=Decimal("10000"), currency="CLP"),
            sold_quantity=10,
            available_quantity=5,
            seller_id="SELL-1",
            condition="new",
            shipping_info={},
            category="MLC123",
        )
        evidence = MarketEvidence(
            listing=listing,
            trend_signals=[
                TrendSignal(keyword="lampara escritorio usb", rank=1, matched=True, trend_score=Decimal("0.95")),
            ],
            confidence=Confidence.HIGH,
        )
        input_data = ListingGenerationInput(
            product_id="P-01",
            title="Lampara Flex",
            price=Decimal("15000"),
            currency="CLP",
            available_quantity=2,
            channel=channel,
            market_evidence=evidence,
        )
        generator = DeterministicListingGenerator()
        result = generator.generate(input_data)

        assert len(result.seo_strategy.primary_keywords) >= 1
        assert result.seo_strategy.primary_keywords[0].keyword == "lampara escritorio usb"
        assert result.seo_strategy.primary_keywords[0].source_type == KeywordSourceType.OBSERVED
        assert "lampara escritorio usb" in result.seo_strategy.search_terms
