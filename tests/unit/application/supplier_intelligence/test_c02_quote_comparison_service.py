import pytest
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierEvidence,
    SupplierCandidate,
    ProductMatch,
    ProductMatchGrade,
    SupplierReadiness,
    EvidenceProvenanceType,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
    PriceTier,
    MOQInfo,
    MOQType,
    CommercialQuote,
    QuoteFreshness,
    QuoteComparabilityStatus,
    BestCommercialCandidate,
)
from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.market_intelligence.models import (
    MarketEvidence,
    MarketListing,
    Marketplace,
    Money,
    Confidence,
    SignalType,
)
from src.domain.opportunity.models import Opportunity, OpportunityReadiness, EvidenceSufficiency
from src.infrastructure.persistence.data.json.supplier_repository import JsonSupplierRepository
from src.infrastructure.suppliers.directory_supplier_source import DirectorySupplierSource
from src.application.supplier_intelligence.supplier_discovery_action_executor import (
    SupplierDiscoveryActionExecutor,
)
from src.domain.supplier_intelligence.services import QuoteNormalizer, QuoteComparator


@pytest.fixture
def sample_opportunity():
    listing = MarketListing(
        external_id="MLC12345678",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Kingston SSD A400 480GB SATA3",
        price=Money(amount=Decimal("38990"), currency="CLP"),
        sold_quantity=120,
        available_quantity=50,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Hard Drives",
    )
    evidence = MarketEvidence(
        listing=listing,
    )
    return Opportunity(
        opportunity_id="OPP-TEST-002",
        product_id="PROD-SSD-480",
        title="Kingston SSD A400 480GB SATA3",
        listing=listing,
        evidence=evidence,
        score=Decimal("85.0"),
        confidence=Confidence.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        readiness=OpportunityReadiness.READY,
        provenance={"brand": "Kingston", "model": "A400", "sku": "SA400S37/480G"},
    )


@pytest.fixture
def supplier_sources():
    repo_root = Path(__file__).resolve().parents[4]
    data_dir = repo_root / "data" / "suppliers"
    return [DirectorySupplierSource(directory_path=data_dir)]


def test_supplier_discovery_action_executor_compare_operation(sample_opportunity, supplier_sources):
    executor = SupplierDiscoveryActionExecutor(
        sources=supplier_sources,
        target_opportunity=sample_opportunity,
    )
    state = LoopState(mission_id="M-C02-01", iteration=1, goal="Discover & Compare", current_target=sample_opportunity.title)

    # 1. Discover
    disc_dec = LoopDecision(action=LoopAction.CONTINUE, target=sample_opportunity.title, reason="Discovering candidates")
    disc_res = executor.execute(disc_dec, state)
    assert disc_res["status"] == "SUCCESS"
    assert disc_res["raw_candidates_count"] >= 3

    # 2. Compare via ActionExecutor
    comp_dec = LoopDecision(
        action=LoopAction.CONTINUE,
        target=sample_opportunity.title,
        parameters={"operation": "COMPARE", "target_market_price": 40000, "analysis_quantities": [1, 5, 20]},
        reason="Compare commercial quotes of discovered candidates",
    )
    comp_res = executor.execute(comp_dec, state)
    assert comp_res["status"] == "SUCCESS"
    assert comp_res["operation"] == "COMPARE"
    assert comp_res["candidates_compared"] >= 3
    assert len(comp_res["ranked_items"]) >= 3
    
    # Verificar items rankeados
    top_item = comp_res["ranked_items"][0]
    assert top_item["rank"] == 1
    assert "currency" in top_item
    assert "scenarios" in top_item
    assert len(top_item["scenarios"]) == 3

    # Best commercial candidate
    best_cand = comp_res["best_commercial_candidate"]
    assert best_cand is not None
    assert "supplier_id" in best_cand
    assert "why_best" in best_cand
    assert "commercial_score" in best_cand


def test_executor_multi_currency_and_unknowns_handling(sample_opportunity, supplier_sources):
    executor = SupplierDiscoveryActionExecutor(
        sources=supplier_sources,
        target_opportunity=sample_opportunity,
    )
    state = LoopState(mission_id="M-C02-02", iteration=1, goal="Investigate & Compare", current_target=sample_opportunity.title)
    
    # Descubrir inicial
    executor.execute(
        LoopDecision(action=LoopAction.CONTINUE, target=sample_opportunity.title, reason="Initial discovery"),
        state,
    )

    # Añadir un candidato con moneda USD para probar aislamiento de monedas
    cand_usd = SupplierCandidate(
        supplier=Supplier(
            supplier_id="SUP-USD-EXT",
            name="Overseas Supplier",
            source="OVERSEAS_API",
            source_type=EvidenceProvenanceType.LIVE,
            location=SupplierLocation(country="USA"),
            contact=SupplierContact(name="Sales", email="sales@overseas.com"),
            product_reference=SupplierProductReference(sku="SA400S37/480G", title="Kingston SSD 480GB", brand="Kingston"),
        ),
        evidence=SupplierEvidence(
            supplier_id="SUP-USD-EXT",
            sku="SA400S37/480G",
            wholesale_price=Decimal("25.00"),
            currency="USD",
            minimum_order_quantity=50,
            stock_available=True,
            confidence=Confidence.HIGH,
            signal_type=SignalType.OBSERVED,
            provenance_type=EvidenceProvenanceType.LIVE,
            source="OVERSEAS_API",
        ),
        product_match=ProductMatch(grade=ProductMatchGrade.EXACT_MATCH, confidence=Confidence.HIGH, matched_fields=("brand", "sku")),
        readiness=SupplierReadiness.EVALUATED,
    )
    executor._cached_candidates["SUP-USD-EXT"] = cand_usd

    comp_res = executor.execute(
        LoopDecision(
            action=LoopAction.CONTINUE,
            target=sample_opportunity.title,
            parameters={"operation": "COMPARE", "target_market_price": 40000},
            reason="Compare multi-currency quotes",
        ),
        state,
    )

    assert comp_res["status"] == "SUCCESS"
    assert len(comp_res["non_comparable_reasons"]) > 0
    assert any("Multiple currencies" in r for r in comp_res["non_comparable_reasons"])
    
    usd_item = next(it for it in comp_res["ranked_items"] if it["supplier_id"] == "SUP-USD-EXT")
    assert usd_item["currency"] == "USD"
    assert usd_item["comparability"] == QuoteComparabilityStatus.NOT_COMPARABLE.value
