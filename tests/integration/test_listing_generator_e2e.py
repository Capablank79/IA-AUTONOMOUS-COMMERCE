import pytest
from datetime import datetime, timezone
from decimal import Decimal

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
    CustomerPainCategory,
    CustomerPainPoint,
    ChannelContentConstraint,
    ListingGenerationInput,
    ListingGenerationResult,
)
from src.application.publication.listing_generator_service import ListingDraftGeneratorService
from src.domain.publication.services import DeterministicListingGenerator


class TestListingGeneratorE2EIntegration:
    """
    Test de integración End-to-End para el flujo G.1:
    Market Evidence + Product Truth + Customer Pain + SEO -> Listing Generator -> Structured ListingDraft.
    """

    def test_complete_e2e_generation_pipeline(self):
        # 1. Preparar Canal Comercial
        channel = SalesChannel(
            channel_id="CH_MERCADOLIBRE_CL",
            channel_type=SalesChannelType.MARKETPLACE,
            name="Mercado Libre Chile",
            region="CL",
            currency="CLP",
        )

        # 2. Preparar Evidencia de Mercado (Señales de Reseñas y Tendencias)
        now = datetime.now(timezone.utc)
        listing = MarketListing(
            external_id="MLC-COMP-999",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Aspiradora Portatil Auto Generica",
            price=Money(amount=Decimal("25000"), currency="CLP"),
            sold_quantity=150,
            available_quantity=20,
            seller_id="SELL-COMP-1",
            condition="new",
            shipping_info={},
            category="MLC1055",
        )
        review_signal = ReviewSignal(
            item_id="MLC-COMP-999",
            total_reviews=4,
            average_rating=2.2,
            reviews=[
                Review(
                    external_id="rev_01",
                    rating=1,
                    text="Pésima batería, sólo dura 5 minutos y se apaga",
                    date=now,
                    reviewable_object="MLC-COMP-999",
                ),
                Review(
                    external_id="rev_02",
                    rating=2,
                    text="Falta potencia para aspirar pelos de mascotas",
                    date=now,
                    reviewable_object="MLC-COMP-999",
                ),
            ],
            paging={},
            observed_at=now,
            confidence=Confidence.HIGH,
        )
        trend_signal = TrendSignal(
            keyword="mini aspiradora inalambrica potente",
            rank=1,
            matched=True,
            trend_score=Decimal("0.98"),
        )
        market_evidence = MarketEvidence(
            listing=listing,
            review_signals=[review_signal],
            trend_signals=[trend_signal],
            confidence=Confidence.HIGH,
        )

        # 3. Preparar Datos Factuales de Nuestro Producto (Product Truth)
        input_data = ListingGenerationInput(
            product_id="PROD-CLEANMAX-V2",
            title="Mini Aspiradora Portátil",
            brand="CleanMax",
            model="V2-Turbo",
            price=Decimal("34990"),
            currency="CLP",
            available_quantity=50,
            channel=channel,
            attributes={
                "battery_life": "45 minutos continuos",
                "suction_power": "12000 Pa ciclónica",
                "filter_type": "Filtro HEPA lavable",
                "accessories": "3 boquillas incluidas",
            },
            market_evidence=market_evidence,
            constraints=ChannelContentConstraint(max_title_length=60),
            supplier_context={"supplier_id": "SUPP-01", "lead_time_days": 2},
            economics_context={"unit_cost": 12000, "target_margin_pct": 45.0},
        )

        # 4. Ejecutar Servicio Generador de Listing
        service = ListingDraftGeneratorService()
        result: ListingGenerationResult = service.generate_listing_draft(input_data)

        # 5. Verificaciones Rigurosas del ListingDraft y Metadatos
        draft = result.draft
        assert draft.product_reference_id == "PROD-CLEANMAX-V2"
        assert draft.channel.channel_id == "CH_MERCADOLIBRE_CL"
        assert draft.price == Decimal("34990")
        assert draft.currency == "CLP"
        assert draft.available_quantity == 50

        # Verificación del Título: Contiene marca/modelo y respeta longitud
        assert len(draft.title) <= 60
        assert "CleanMax" in draft.title
        assert "V2-Turbo" in draft.title

        # Verificación de Bullets y Descripción
        assert len(draft.metadata["bullet_points"]) >= 3
        assert any("Filtro HEPA lavable" in b for b in draft.metadata["bullet_points"])

        # Verificación de Diferenciación Factual por Dolores de Clientes
        # Dolor de competidor: Batería y Potencia. Nuestro producto sí tiene atributos verificados para ambos
        assert len(result.differentiation_strategy.differential_claims) >= 2
        diff_texts = " ".join(result.differentiation_strategy.differential_claims)
        assert "45 minutos continuos" in diff_texts
        assert "12000 Pa ciclónica" in diff_texts

        # Verificación de Grounding y Trazabilidad (Provenance)
        assert result.grounding.verified_attributes["battery_life"] == "45 minutos continuos"
        assert result.grounding.verified_attributes["suction_power"] == "12000 Pa ciclónica"
        assert len(result.grounding.claims_provenance) > 0

        # Verificación de SEO: Palabra clave observada de alta relevancia incluida
        assert "mini aspiradora inalambrica potente" in result.seo_strategy.search_terms

        # Verificación Multicanal: Adaptaciones automáticas
        assert len(result.multichannel_variants) == 2
        ig_variant = next(v for v in result.multichannel_variants if v.channel_type == SalesChannelType.SOCIAL_COMMERCE)
        assert "#miniaspiradorainalambricapotente" in ig_variant.tags_or_keywords
