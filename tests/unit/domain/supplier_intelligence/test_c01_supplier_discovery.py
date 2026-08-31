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
    SupplierStatus,
    SupplierReadiness,
    SupplierRejectionReason,
    EvidenceProvenanceType,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
    ConfirmedQuote,
    BestKnownSupplier,
)
from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.services import (
    SupplierNormalizer,
    ProductMatcher,
    SupplierScorer,
)
from src.infrastructure.persistence.data.json.supplier_repository import JsonSupplierRepository
from src.infrastructure.suppliers.directory_supplier_source import DirectorySupplierSource
from src.application.supplier_intelligence.supplier_discovery_action_executor import (
    SupplierDiscoveryActionExecutor,
)


def test_supplier_models_and_immutability():
    loc = SupplierLocation(country="Chile", city="Santiago")
    contact = SupplierContact(name="Juan", email="juan@proveedor.cl", phone="+56912345678", website="proveedor.cl")
    prod_ref = SupplierProductReference(sku="SKU-100", title="SSD Kingston", brand="Kingston", model="A400")

    supplier = Supplier(
        supplier_id="SUP-001",
        name="Proveedor Uno",
        source="LOCAL_CATALOG",
        source_type=EvidenceProvenanceType.FIXTURE,
        location=loc,
        contact=contact,
        product_reference=prod_ref,
    )

    assert supplier.supplier_id == "SUP-001"
    assert supplier.name == "Proveedor Uno"
    assert supplier.location.country == "Chile"

    # Verificar inmutabilidad
    with pytest.raises(Exception):
        supplier.name = "Cambio Ilegal"


def test_supplier_evidence_provenance_and_unknowns():
    evidence = SupplierEvidence(
        supplier_id="SUP-002",
        sku="SKU-200",
        wholesale_price=Decimal("15000"),
        shipping_cost=None,  # Unknown explícito
        lead_time_days=None,  # Unknown explícito
        confidence=Confidence.HIGH,
        signal_type=SignalType.OBSERVED,
        provenance_type=EvidenceProvenanceType.LIVE,
    )

    assert evidence.wholesale_price == Decimal("15000")
    assert evidence.shipping_cost is None
    assert evidence.lead_time_days is None
    assert evidence.provenance_type == EvidenceProvenanceType.LIVE


def test_product_matcher_exact_close_variant_uncertain_nomatch():
    # 1. Exact Match por SKU
    match_sku = ProductMatcher.match(
        target_title="Disco SSD Kingston A400 480GB",
        target_brand="Kingston",
        target_model="A400",
        target_sku="SA400S37/480G",
        supplier_sku="SA400S37/480G",
        supplier_title="Kingston SSD 480GB SA400S37/480G",
        supplier_brand="Kingston",
        supplier_model="A400",
    )
    assert match_sku.grade == ProductMatchGrade.EXACT_MATCH
    assert match_sku.confidence == Confidence.HIGH

    # 2. Exact Match por Brand + Model + Title overlap
    match_brand_model = ProductMatcher.match(
        target_title="Disco Solido Kingston A400 480GB SATA 2.5",
        target_brand="Kingston",
        target_model="A400",
        supplier_title="SSD Kingston A400 480GB Sata3",
        supplier_brand="Kingston",
        supplier_model="A400",
    )
    assert match_sku.grade in [ProductMatchGrade.EXACT_MATCH, ProductMatchGrade.CLOSE_MATCH]

    # 3. Variant Match
    match_var = ProductMatcher.match(
        target_title="SSD Kingston A400 480GB",
        target_brand="Kingston",
        supplier_title="SSD Kingston A400 960GB Negro",
        supplier_brand="Kingston",
    )
    assert match_var.grade in [ProductMatchGrade.VARIANT, ProductMatchGrade.CLOSE_MATCH]

    # 4. No Match
    match_no = ProductMatcher.match(
        target_title="SSD Kingston A400 480GB",
        target_brand="Kingston",
        supplier_title="Memoria RAM Corsair Vengeance 16GB",
        supplier_brand="Corsair",
    )
    assert match_no.grade == ProductMatchGrade.NO_MATCH


def test_supplier_normalizer_and_deduplication():
    loc = SupplierLocation(country="Chile")
    sup1 = Supplier(
        supplier_id="SUP-A",
        name="Distribuidora Tech SpA",
        source="CATALOG_A",
        source_type=EvidenceProvenanceType.FIXTURE,
        location=loc,
        contact=SupplierContact(email="contacto@tech.cl"),
    )
    sup2 = Supplier(
        supplier_id="SUP-B",
        name="distribuidora tech s.p.a.",
        source="CATALOG_B",
        source_type=EvidenceProvenanceType.FIXTURE,
        location=loc,
        contact=SupplierContact(email="contacto@tech.cl"),
    )

    is_same, conf = SupplierNormalizer.are_same_supplier(sup1, sup2)
    assert is_same is True
    assert conf >= 0.85

    # Probar deduplicación
    cand1 = SupplierCandidate(
        supplier=sup1,
        evidence=SupplierEvidence(supplier_id="SUP-A", sku="SKU-1", wholesale_price=Decimal("20000"), confidence=Confidence.MEDIUM),
        product_match=ProductMatch(grade=ProductMatchGrade.EXACT_MATCH, confidence=Confidence.HIGH),
    )
    cand2 = SupplierCandidate(
        supplier=sup2,
        evidence=SupplierEvidence(
            supplier_id="SUP-B",
            sku="SKU-1",
            wholesale_price=Decimal("19500"),
            confidence=Confidence.HIGH,
            quote=ConfirmedQuote(quote_id="Q-1", wholesale_price=Decimal("19500"), shipping_cost=Decimal("1000"), lead_time_days=2),
        ),
        product_match=ProductMatch(grade=ProductMatchGrade.EXACT_MATCH, confidence=Confidence.HIGH),
    )

    deduped = SupplierNormalizer.deduplicate_candidates([cand1, cand2])
    assert len(deduped) == 1
    # Debe conservar cand2 por tener cotización confirmada y mayor confianza
    assert deduped[0].supplier.supplier_id == "SUP-B"


def test_supplier_scorer_deterministic_and_ranking():
    sup = Supplier(
        supplier_id="SUP-001",
        name="Proveedor Test",
        source="SRC",
        source_type=EvidenceProvenanceType.FIXTURE,
        status=SupplierStatus.VERIFIED,
    )
    evidence_good = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-A",
        wholesale_price=Decimal("18000"),
        stock_available=True,
        minimum_order_quantity=5,
        shipping_cost=Decimal("2000"),
        lead_time_days=2,
        confidence=Confidence.HIGH,
    )
    cand_good = SupplierCandidate(
        supplier=sup,
        evidence=evidence_good,
        product_match=ProductMatch(grade=ProductMatchGrade.EXACT_MATCH, confidence=Confidence.HIGH),
    )

    # Score determinista con target market price = 40000 CLP
    score_1 = SupplierScorer.calculate_score(cand_good, target_market_price=Decimal("40000"))
    score_2 = SupplierScorer.calculate_score(cand_good, target_market_price=Decimal("40000"))

    assert score_1.total_score == score_2.total_score
    assert score_1.total_score >= Decimal("70.0")
    assert score_1.match_score == Decimal("35.0")
    assert score_1.price_score == Decimal("25.0")  # 18000 / 40000 = 45% (< 60%)

    # Ranking
    cand_bad = SupplierCandidate(
        supplier=sup,
        evidence=SupplierEvidence(supplier_id="SUP-001", sku="SKU-B", wholesale_price=Decimal("38000"), stock_available=False),
        product_match=ProductMatch(grade=ProductMatchGrade.CLOSE_MATCH, confidence=Confidence.MEDIUM),
    )

    ranked = SupplierScorer.rank_candidates([cand_bad, cand_good], target_market_price=Decimal("40000"))
    assert len(ranked) == 2
    assert ranked[0].evidence.sku == "SKU-A"
    assert ranked[0].rank == 1
    assert ranked[1].rank == 2


def test_supplier_repository_json_roundtrip(tmp_path):
    repo = JsonSupplierRepository(tmp_path)
    loc = SupplierLocation(country="Chile", city="Santiago")
    supplier = Supplier(
        supplier_id="SUP-101",
        name="Mayorista Central",
        source="CATALOG",
        source_type=EvidenceProvenanceType.FIXTURE,
        location=loc,
        status=SupplierStatus.ACTIVE,
    )
    evidence = SupplierEvidence(
        supplier_id="SUP-101",
        sku="SKU-101",
        wholesale_price=Decimal("25000"),
        currency="CLP",
        minimum_order_quantity=10,
        stock_available=True,
        confidence=Confidence.HIGH,
    )

    repo.save_supplier(supplier)
    repo.save_evidence(evidence)

    retrieved_sup = repo.get_supplier("SUP-101")
    retrieved_evi = repo.get_evidence("SUP-101", "SKU-101")

    assert retrieved_sup is not None
    assert retrieved_sup.supplier_id == "SUP-101"
    assert retrieved_sup.name == "Mayorista Central"
    assert retrieved_sup.location.city == "Santiago"

    assert retrieved_evi is not None
    assert retrieved_evi.supplier_id == "SUP-101"
    assert retrieved_evi.wholesale_price == Decimal("25000")
    assert retrieved_evi.stock_available is True
