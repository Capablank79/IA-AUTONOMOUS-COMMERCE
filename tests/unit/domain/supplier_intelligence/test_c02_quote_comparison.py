import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import List

from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierEvidence,
    SupplierCandidate,
    ProductMatch,
    ProductMatchGrade,
    SupplierStatus,
    SupplierReadiness,
    EvidenceProvenanceType,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
    ConfirmedQuote,
    PriceTier,
    MOQInfo,
    MOQType,
    QuoteFreshness,
    QuoteComparabilityStatus,
    QuoteConflictStatus,
    CommercialQuote,
    QuoteConflict,
    QuoteScenarioEvaluation,
    SupplierQuoteComparisonItem,
    BestCommercialCandidate,
    QuoteComparisonResult,
)
from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.services import (
    QuoteNormalizer,
    QuoteComparator,
    SupplierScorer,
    SupplierNormalizer,
)


def _make_candidate(
    supplier_id: str,
    name: str,
    sku: str = "SKU-TEST",
    wholesale_price: Decimal = Decimal("15000"),
    currency: str = "CLP",
    moq: int = 5,
    shipping_cost: Decimal = Decimal("2500"),
    lead_time_days: int = 3,
    stock_available: bool = True,
    confidence: Confidence = Confidence.HIGH,
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE,
    product_match_grade: ProductMatchGrade = ProductMatchGrade.EXACT_MATCH,
    raw_payload: dict = None,
) -> SupplierCandidate:
    sup = Supplier(
        supplier_id=supplier_id,
        name=name,
        source="TEST_SOURCE",
        source_type=provenance_type,
        location=SupplierLocation(country="Chile", city="Santiago"),
        contact=SupplierContact(name="Contact", email="info@test.com"),
        product_reference=SupplierProductReference(sku=sku, title=name, brand="BrandX"),
    )
    evi = SupplierEvidence(
        supplier_id=supplier_id,
        sku=sku,
        wholesale_price=wholesale_price,
        currency=currency,
        minimum_order_quantity=moq,
        stock_available=stock_available,
        shipping_cost=shipping_cost,
        lead_time_days=lead_time_days,
        confidence=confidence,
        signal_type=SignalType.OBSERVED,
        provenance_type=provenance_type,
        source="TEST_SOURCE",
        raw_payload=raw_payload or {},
    )
    match = ProductMatch(
        grade=product_match_grade,
        confidence=confidence,
        matched_fields=("brand", "model"),
        discrepancies=(),
        details="Match verified in tests",
    )
    return SupplierCandidate(
        supplier=sup,
        evidence=evi,
        product_match=match,
        readiness=SupplierReadiness.EVALUATED,
    )


# 1. Quote Model & Immutability
def test_quote_model_and_immutability():
    moq = MOQInfo(quantity=10, moq_type=MOQType.SKU)
    tier1 = PriceTier(min_quantity=1, max_quantity=9, unit_price=Decimal("20000"), currency="CLP")
    tier2 = PriceTier(min_quantity=10, max_quantity=None, unit_price=Decimal("18000"), currency="CLP")
    
    quote = CommercialQuote(
        quote_id="Q-001",
        supplier_id="SUP-001",
        sku="SKU-123",
        unit_price=Decimal("20000"),
        currency="CLP",
        moq=moq,
        price_tiers=(tier1, tier2),
        shipping_cost=Decimal("3000"),
        lead_time_days=2,
        stock_available=True,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.FIXTURE,
    )

    assert quote.quote_id == "Q-001"
    assert quote.supplier_id == "SUP-001"
    assert quote.moq.quantity == 10
    assert len(quote.price_tiers) == 2
    assert quote.price_tiers[1].unit_price == Decimal("18000")

    # Inmutabilidad
    with pytest.raises(Exception):
        quote.unit_price = Decimal("15000")


# 2. Price Tiers Evaluation & Conflict in Tiers
def test_price_tiers_volume_discount_resolution():
    tiers = (
        PriceTier(min_quantity=1, max_quantity=9, unit_price=Decimal("10000"), currency="CLP"),
        PriceTier(min_quantity=10, max_quantity=49, unit_price=Decimal("8500"), currency="CLP"),
        PriceTier(min_quantity=50, max_quantity=None, unit_price=Decimal("7000"), currency="CLP"),
    )
    quote = CommercialQuote(
        quote_id="Q-TIERS",
        supplier_id="SUP-TIERS",
        sku="SKU-TIERS",
        unit_price=Decimal("10000"),
        currency="CLP",
        moq=MOQInfo(quantity=1),
        price_tiers=tiers,
    )

    # Qty = 5 -> tier 1
    assert quote.get_unit_price_for_quantity(5) == Decimal("10000")
    # Qty = 10 -> tier 2
    assert quote.get_unit_price_for_quantity(10) == Decimal("8500")
    # Qty = 50 -> tier 3
    assert quote.get_unit_price_for_quantity(50) == Decimal("7000")
    # Qty = 100 -> tier 3 (abierto)
    assert quote.get_unit_price_for_quantity(100) == Decimal("7000")


# 3. MOQ Intelligence (Known, Unknown, Types)
def test_moq_modeling_and_intelligence():
    # Known SKU MOQ
    moq_known = MOQInfo(quantity=50, moq_type=MOQType.SKU)
    assert moq_known.is_known is True
    assert moq_known.quantity == 50

    # Unknown MOQ
    moq_unknown = MOQInfo(quantity=None, moq_type=MOQType.UNKNOWN)
    assert moq_unknown.is_known is False
    assert moq_unknown.quantity is None

    # Anti-fabricación: Unknown MOQ != 1
    assert moq_unknown.quantity != 1


# 4. Currency and No Fabricated FX
def test_quote_currency_comparability_restriction():
    quote_clp = CommercialQuote(
        quote_id="Q-CLP",
        supplier_id="SUP-CLP",
        sku="SKU-1",
        unit_price=Decimal("10000"),
        currency="CLP",
        moq=MOQInfo(quantity=1),
    )
    quote_usd = CommercialQuote(
        quote_id="Q-USD",
        supplier_id="SUP-USD",
        sku="SKU-1",
        unit_price=Decimal("10"),
        currency="USD",
        moq=MOQInfo(quantity=1),
    )

    status, reasons = QuoteComparator.check_comparability(quote_clp, quote_usd)
    assert status == QuoteComparabilityStatus.NOT_COMPARABLE
    assert any("CURRENCY_MISMATCH" in r for r in reasons)
    assert any("No fabricated FX conversion permitted" in r for r in reasons)


# 5. Shipping & Lead Time & Availability
def test_shipping_lead_time_availability_known_and_unknown():
    # Con shipping y lead time conocidos
    quote_complete = CommercialQuote(
        quote_id="Q-COMPLETE",
        supplier_id="SUP-01",
        sku="SKU-01",
        unit_price=Decimal("12000"),
        currency="CLP",
        moq=MOQInfo(quantity=2),
        shipping_cost=Decimal("3500"),
        lead_time_days=4,
        stock_available=True,
    )
    assert quote_complete.shipping_cost == Decimal("3500")
    assert quote_complete.lead_time_days == 4
    assert quote_complete.stock_available is True
    assert len(quote_complete.unknowns) == 0

    # Con shipping y lead time UNKNOWN
    quote_partial = CommercialQuote(
        quote_id="Q-PARTIAL",
        supplier_id="SUP-02",
        sku="SKU-01",
        unit_price=Decimal("12000"),
        currency="CLP",
        moq=MOQInfo(quantity=None, moq_type=MOQType.UNKNOWN),
        shipping_cost=None,
        lead_time_days=None,
        stock_available=None,
        unknowns=("SHIPPING_COST_UNKNOWN", "LEAD_TIME_UNKNOWN", "MOQ_UNKNOWN", "AVAILABILITY_UNKNOWN"),
    )
    assert quote_partial.shipping_cost is None
    assert quote_partial.shipping_cost != Decimal("0")
    assert quote_partial.lead_time_days is None
    assert quote_partial.stock_available is None
    assert "SHIPPING_COST_UNKNOWN" in quote_partial.unknowns


# 6. Provenance (LIVE vs FIXTURE vs MOCK vs DERIVED)
def test_quote_provenance_preservation():
    quote_fixture = CommercialQuote(
        quote_id="Q-FIX",
        supplier_id="SUP-FIX",
        sku="SKU-1",
        unit_price=Decimal("5000"),
        currency="CLP",
        moq=MOQInfo(quantity=1),
        provenance_type=EvidenceProvenanceType.FIXTURE,
    )
    assert quote_fixture.provenance_type == EvidenceProvenanceType.FIXTURE
    assert quote_fixture.provenance_type != EvidenceProvenanceType.LIVE


# 7. Freshness (CURRENT, EXPIRED, UNKNOWN_FRESHNESS)
def test_quote_freshness_lifecycle():
    now = datetime.now(timezone.utc)
    
    # 1. Vigente
    quote_valid = CommercialQuote(
        quote_id="Q-V1",
        supplier_id="SUP-1",
        sku="SKU-1",
        unit_price=Decimal("1000"),
        currency="CLP",
        moq=MOQInfo(quantity=1),
        observed_at=now - timedelta(days=2),
        valid_until=now + timedelta(days=5),
    )
    assert quote_valid.freshness == QuoteFreshness.FRESH

    # 2. Expirada
    quote_expired = CommercialQuote(
        quote_id="Q-V2",
        supplier_id="SUP-1",
        sku="SKU-1",
        unit_price=Decimal("1000"),
        currency="CLP",
        moq=MOQInfo(quantity=1),
        observed_at=now - timedelta(days=40),
        valid_until=now - timedelta(days=2),
    )
    assert quote_expired.freshness == QuoteFreshness.EXPIRED

    # 3. Observación antigua (> 30 días sin valid_until)
    quote_stale = CommercialQuote(
        quote_id="Q-V3",
        supplier_id="SUP-1",
        sku="SKU-1",
        unit_price=Decimal("1000"),
        currency="CLP",
        moq=MOQInfo(quantity=1),
        observed_at=now - timedelta(days=45),
        valid_until=None,
    )
    assert quote_stale.freshness == QuoteFreshness.STALE

    # 4. Observación muy antigua (> 90 días) -> EXPIRED
    quote_very_old = CommercialQuote(
        quote_id="Q-V4",
        supplier_id="SUP-1",
        sku="SKU-1",
        unit_price=Decimal("1000"),
        currency="CLP",
        moq=MOQInfo(quantity=1),
        observed_at=now - timedelta(days=100),
        valid_until=None,
    )
    assert quote_very_old.freshness == QuoteFreshness.EXPIRED


# 8. Conflict Detection & Deterministic Resolution
def test_quote_conflict_detection_and_resolution():
    now = datetime.now(timezone.utc)
    q_old = CommercialQuote(
        quote_id="Q-OLD",
        supplier_id="SUP-001",
        sku="SKU-100",
        unit_price=Decimal("20000"),
        currency="CLP",
        moq=MOQInfo(quantity=5),
        confidence=Confidence.MEDIUM,
        observed_at=now - timedelta(days=5),
    )
    q_new = CommercialQuote(
        quote_id="Q-NEW",
        supplier_id="SUP-001",
        sku="SKU-100",
        unit_price=Decimal("18000"),
        currency="CLP",
        moq=MOQInfo(quantity=10),
        confidence=Confidence.HIGH,
        observed_at=now - timedelta(hours=1),
    )

    conflicts = QuoteComparator.detect_conflicts([q_old, q_new])
    assert len(conflicts) == 1
    conf = conflicts[0]
    assert conf.supplier_id == "SUP-001"
    assert conf.conflict_type == "PRICE_OR_MOQ_DISCREPANCY"
    # Debe resolverse por mayor confianza (HIGH > MEDIUM)
    assert conf.resolution_status == QuoteConflictStatus.RESOLVED_BY_HIGHER_CONFIDENCE
    assert conf.resolved_quote_id == "Q-NEW"


# 9. Scenario Evaluation (QTY=1, QTY=MOQ, etc.)
def test_quote_scenario_evaluation():
    tiers = (
        PriceTier(min_quantity=1, max_quantity=9, unit_price=Decimal("10000"), currency="CLP"),
        PriceTier(min_quantity=10, max_quantity=None, unit_price=Decimal("8000"), currency="CLP"),
    )
    quote = CommercialQuote(
        quote_id="Q-SCENARIO",
        supplier_id="SUP-SCENARIO",
        sku="SKU-S",
        unit_price=Decimal("10000"),
        currency="CLP",
        moq=MOQInfo(quantity=5),
        price_tiers=tiers,
        shipping_cost=Decimal("2000"),
    )

    # Qty = 1: Below MOQ
    sc1 = QuoteComparator.evaluate_scenario(quote, quantity=1)
    assert sc1.scenario_quantity == 1
    assert sc1.unit_price == Decimal("10000")
    assert sc1.total_goods_cost == Decimal("10000")
    assert sc1.total_estimated_landed_subtotal == Decimal("12000")
    assert sc1.is_moq_satisfied is False

    # Qty = 10: MOQ Satisfied & Tier Applied
    sc10 = QuoteComparator.evaluate_scenario(quote, quantity=10)
    assert sc10.scenario_quantity == 10
    assert sc10.unit_price == Decimal("8000")
    assert sc10.total_goods_cost == Decimal("80000")
    assert sc10.total_estimated_landed_subtotal == Decimal("82000")
    assert sc10.is_moq_satisfied is True


# 10. Deterministic Candidate Comparison & Ranking
def test_deterministic_candidate_comparison_and_ranking():
    cand1 = _make_candidate(
        supplier_id="SUP-001",
        name="Proveedor Barato y Confiable",
        wholesale_price=Decimal("8000"),
        moq=2,
        shipping_cost=Decimal("1500"),
        lead_time_days=1,
        confidence=Confidence.HIGH,
    )
    cand2 = _make_candidate(
        supplier_id="SUP-002",
        name="Proveedor Caro",
        wholesale_price=Decimal("18000"),
        moq=20,
        shipping_cost=Decimal("5000"),
        lead_time_days=7,
        confidence=Confidence.LOW,
    )
    cand3 = _make_candidate(
        supplier_id="SUP-003",
        name="Proveedor Sin Stock",
        wholesale_price=Decimal("7000"),
        stock_available=False,
    )

    res = QuoteComparator.compare_candidates(
        candidates=[cand1, cand2, cand3],
        target_product_title="Test Product Alpha",
        target_market_price=Decimal("25000"),
    )

    assert len(res.ranked_items) == 3
    # Rank 1 debe ser SUP-001
    assert res.ranked_items[0].supplier.supplier_id == "SUP-001"
    assert res.ranked_items[0].rank == 1
    
    # Best commercial candidate
    best = res.best_commercial_candidate
    assert best is not None
    assert best.supplier_id == "SUP-001"
    assert best.unit_price == Decimal("8000")
    assert best.moq == 2
    assert "Ranked #1" in best.why_best


# 11. Anti-fabrication check: Unknown fields remain None
def test_anti_fabrication_unknown_values_not_zero_or_inferred():
    cand_unknown = _make_candidate(
        supplier_id="SUP-EMPTY",
        name="Proveedor Sin Precios",
        wholesale_price=None,
        moq=None,
        shipping_cost=None,
        lead_time_days=None,
    )

    quote = QuoteNormalizer.from_evidence(cand_unknown.evidence)
    assert quote.unit_price is None
    assert quote.unit_price != Decimal("0")
    assert quote.moq.quantity is None
    assert quote.moq.quantity != 1
    assert quote.shipping_cost is None
    assert quote.shipping_cost != Decimal("0")
    assert quote.lead_time_days is None
    assert "UNIT_PRICE_UNKNOWN" in quote.unknowns
    assert "MOQ_UNKNOWN" in quote.unknowns
    assert "SHIPPING_COST_UNKNOWN" in quote.unknowns
    assert "LEAD_TIME_UNKNOWN" in quote.unknowns
