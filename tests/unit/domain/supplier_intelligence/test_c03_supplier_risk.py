import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

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
    ConfirmedQuote,
    CommercialQuote,
    ShippingMethod,
    ShippingOption,
    ShippingComparabilityStatus,
    SLAStatus,
    SLARecord,
    SupplierObservationEvent,
    HistoricalPerformanceProfile,
    PerformanceTrend,
    ReliabilityEvaluation,
    RiskLevel,
    SupplierRiskDimension,
    SupplierRiskProfile,
    SupplierRiskComparisonItem,
    BestSupplierCandidate,
    SupplierRiskEvaluationResult,
    SupplierRejectionReason,
)
from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.services import (
    LeadTimeAnalyzer,
    ShippingAnalyzer,
    HistoricalPerformanceAnalyzer,
    ReliabilityEvaluator,
    SupplierRiskEngine,
    SupplierRiskComparator,
    QuoteNormalizer,
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
    raw_payload: Dict[str, Any] = None,
) -> SupplierCandidate:
    sup = Supplier(
        supplier_id=supplier_id,
        name=name,
        source="CATALOG",
        source_type=provenance_type,
        status=SupplierStatus.VERIFIED,
        location=SupplierLocation(country="Chile", city="Santiago"),
    )
    ev = SupplierEvidence(
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
        source="CATALOG_TEST",
        raw_payload=raw_payload or {},
    )
    pm = ProductMatch(
        grade=ProductMatchGrade.EXACT_MATCH,
        confidence=confidence,
        matched_fields=("sku", "title"),
        discrepancies=(),
        details="Match verified in unit test",
    )
    return SupplierCandidate(
        supplier=sup,
        evidence=ev,
        product_match=pm,
        readiness=SupplierReadiness.EVALUATED,
    )


# =========================================================================
# 1. LEAD TIME INTELLIGENCE & VARIABILITY & STATISTICS & NO FABRICATION
# =========================================================================

def test_lead_time_single_observation_no_fabrication():
    """Si solo existe un valor observado, no inventar varianza ni distribución."""
    lt_profile = LeadTimeAnalyzer.analyze_lead_time(
        observed_days=5,
        historical_events=(),
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.FIXTURE,
    )
    assert lt_profile.observed_days == 5
    assert lt_profile.min_days == 5
    assert lt_profile.max_days == 5
    assert lt_profile.historical_avg_days == 5.0
    assert lt_profile.historical_variance_days is None
    assert lt_profile.on_time_rate is None
    assert "HISTORICAL_VARIABILITY_UNKNOWN" in lt_profile.unknowns


def test_lead_time_none_observed_returns_unknown():
    """Si no hay días observados ni eventos, todo es UNKNOWN sin fabricar ceros."""
    lt_profile = LeadTimeAnalyzer.analyze_lead_time(
        observed_days=None,
        historical_events=(),
    )
    assert lt_profile.observed_days is None
    assert lt_profile.min_days is None
    assert lt_profile.max_days is None
    assert lt_profile.historical_avg_days is None
    assert lt_profile.confidence == Confidence.UNKNOWN
    assert "LEAD_TIME_UNKNOWN" in lt_profile.unknowns


def test_lead_time_with_sufficient_history_calculates_deterministic_stats():
    """Con historial suficiente, calcula promedio, varianza determinista y on_time_rate."""
    events = [
        SupplierObservationEvent(
            event_id="EV-01",
            supplier_id="SUP-01",
            metric="lead_time_days",
            observed_value=4,
            timestamp=datetime.now(timezone.utc) - timedelta(days=20),
        ),
        SupplierObservationEvent(
            event_id="EV-02",
            supplier_id="SUP-01",
            metric="lead_time_days",
            observed_value=6,
            timestamp=datetime.now(timezone.utc) - timedelta(days=10),
        ),
        SupplierObservationEvent(
            event_id="EV-03",
            supplier_id="SUP-01",
            metric="lead_time_days",
            observed_value=5,
            timestamp=datetime.now(timezone.utc) - timedelta(days=2),
        ),
    ]

    lt_profile = LeadTimeAnalyzer.analyze_lead_time(
        observed_days=5,
        historical_events=events,
        confidence=Confidence.HIGH,
    )
    assert lt_profile.observed_days == 5
    assert lt_profile.min_days == 4
    assert lt_profile.max_days == 6
    assert lt_profile.historical_avg_days == 5.0
    assert lt_profile.historical_variance_days == pytest.approx(0.67, rel=1e-1)
    assert lt_profile.on_time_rate == pytest.approx(2.0 / 3.0, rel=1e-2)
    assert "HISTORICAL_VARIABILITY_UNKNOWN" not in lt_profile.unknowns


# =========================================================================
# 2. SHIPPING INTELLIGENCE & ZONES & COMPARABILITY & UNKNOWN
# =========================================================================

def test_shipping_analyzer_extracts_explicit_fields():
    """Extrae costos, método, carrier y zonas explícitas."""
    payload = {
        "shipping_cost": 3500,
        "shipping_method": "EXPRESS",
        "origin_zone": "Santiago",
        "destination_zone": "Santiago",
        "carrier": "Starken",
        "estimated_transit_days": 2,
        "is_free_shipping": False,
    }
    cand = _make_candidate("SUP-01", "Dist 1", raw_payload=payload)
    quote = QuoteNormalizer.from_evidence(cand.evidence)

    ship = ShippingAnalyzer.from_quote_and_payload(quote, payload)
    assert ship.shipping_cost == Decimal("3500")
    assert ship.method == ShippingMethod.EXPRESS
    assert ship.carrier == "Starken"
    assert ship.destination_zone == "Santiago"
    assert ship.is_free_shipping_observed is False
    assert ship.estimated_transit_days == 2


def test_shipping_free_only_when_observed():
    """Free shipping solo se marca si costo es 0 o bandera is_free es explícita."""
    payload = {"shipping_cost": 0, "is_free_shipping": True}
    cand = _make_candidate("SUP-02", "Dist 2", raw_payload=payload)
    quote = QuoteNormalizer.from_evidence(cand.evidence)

    ship = ShippingAnalyzer.from_quote_and_payload(quote, payload)
    assert ship.shipping_cost == Decimal("0")
    assert ship.is_free_shipping_observed is True


def test_shipping_unknown_cost_does_not_assume_free():
    """Si no hay evidencia de shipping, no asumir 0 ni free shipping."""
    payload = {"origin_zone": "Santiago"}
    cand = _make_candidate("SUP-03", "Dist 3", shipping_cost=None, raw_payload=payload)
    # Sobrescribir evidence para que shipping_cost sea None
    ev_no_ship = SupplierEvidence(
        supplier_id="SUP-03",
        sku="SKU-TEST",
        wholesale_price=Decimal("15000"),
        shipping_cost=None,
        currency="CLP",
        confidence=Confidence.HIGH,
        signal_type=SignalType.OBSERVED,
        provenance_type=EvidenceProvenanceType.FIXTURE,
        source="TEST",
    )
    cand_no_ship = SupplierCandidate(
        supplier=cand.supplier,
        evidence=ev_no_ship,
        product_match=cand.product_match,
        readiness=SupplierReadiness.EVALUATED,
    )
    quote = QuoteNormalizer.from_evidence(cand_no_ship.evidence)

    ship = ShippingAnalyzer.from_quote_and_payload(quote, payload)
    assert ship.shipping_cost is None
    assert ship.is_free_shipping_observed is False
    assert "SHIPPING_COST_UNKNOWN" in ship.unknowns


def test_shipping_comparability_different_zones_marked_not_comparable():
    """Comparar proveedores con destinos incompatibles se marca como NOT_COMPARABLE_ZONE."""
    ship_a = ShippingOption(
        shipping_cost=Decimal("2000"),
        currency="CLP",
        destination_zone="Santiago (RM)",
    )
    ship_b = ShippingOption(
        shipping_cost=Decimal("5000"),
        currency="CLP",
        destination_zone="Punta Arenas (Magallanes)",
    )

    status, reasons = ShippingAnalyzer.check_comparability(ship_a, ship_b)
    assert status == ShippingComparabilityStatus.NOT_COMPARABLE_ZONE
    assert len(reasons) > 0


def test_shipping_comparability_identical_zones_comparable():
    """Misma zona y divisa produce COMPARABLE."""
    ship_a = ShippingOption(shipping_cost=Decimal("2000"), currency="CLP", destination_zone="Santiago")
    ship_b = ShippingOption(shipping_cost=Decimal("2500"), currency="CLP", destination_zone="Santiago")

    status, reasons = ShippingAnalyzer.check_comparability(ship_a, ship_b)
    assert status == ShippingComparabilityStatus.COMPARABLE


# =========================================================================
# 3. HISTORICAL PERFORMANCE & TREND DETECTION
# =========================================================================

def test_historical_performance_no_events_returns_insufficient_history():
    """Sin eventos, la tendencia es INSUFFICIENT_HISTORY sin fabricar métricas."""
    profile = HistoricalPerformanceAnalyzer.build_profile("SUP-01", events=())
    assert profile.observation_count == 0
    assert profile.lead_time_trend == PerformanceTrend.INSUFFICIENT_HISTORY
    assert profile.on_time_delivery_rate is None
    assert profile.cancellation_rate is None
    assert profile.incident_count == 0
    assert "HISTORICAL_EVENTS_UNKNOWN" in profile.unknowns


def test_historical_performance_detects_improving_trend():
    """Detecta tendencia IMPROVING si los lead times recientes son significativamente menores que los pasados."""
    past = datetime.now(timezone.utc) - timedelta(days=60)
    recent = datetime.now(timezone.utc) - timedelta(days=5)

    events = [
        SupplierObservationEvent("EV-1", "SUP-01", "lead_time_days", 10, timestamp=past),
        SupplierObservationEvent("EV-2", "SUP-01", "lead_time_days", 9, timestamp=past + timedelta(days=5)),
        SupplierObservationEvent("EV-3", "SUP-01", "lead_time_days", 4, timestamp=recent),
        SupplierObservationEvent("EV-4", "SUP-01", "lead_time_days", 3, timestamp=recent + timedelta(days=1)),
    ]

    profile = HistoricalPerformanceAnalyzer.build_profile("SUP-01", events=events)
    assert profile.observation_count == 4
    assert profile.lead_time_trend == PerformanceTrend.IMPROVING


def test_historical_performance_detects_deteriorating_trend():
    """Detecta tendencia DETERIORATING si los lead times se alargan."""
    past = datetime.now(timezone.utc) - timedelta(days=60)
    recent = datetime.now(timezone.utc) - timedelta(days=5)

    events = [
        SupplierObservationEvent("EV-1", "SUP-01", "lead_time_days", 2, timestamp=past),
        SupplierObservationEvent("EV-2", "SUP-01", "lead_time_days", 3, timestamp=past + timedelta(days=5)),
        SupplierObservationEvent("EV-3", "SUP-01", "lead_time_days", 8, timestamp=recent),
        SupplierObservationEvent("EV-4", "SUP-01", "lead_time_days", 10, timestamp=recent + timedelta(days=1)),
    ]

    profile = HistoricalPerformanceAnalyzer.build_profile("SUP-01", events=events)
    assert profile.lead_time_trend == PerformanceTrend.DETERIORATING


# =========================================================================
# 4. SUPPLIER RELIABILITY & SLA COMPLIANCE
# =========================================================================

def test_sla_compliance_deterministic_evaluation():
    """Evalúa SLA explícito deterministamente."""
    events = [
        SupplierObservationEvent("EV-1", "SUP-01", "sla_on_time_delivery", 1.0, timestamp=datetime.now(timezone.utc)),
        SupplierObservationEvent("EV-2", "SUP-01", "sla_on_time_delivery", 1.0, timestamp=datetime.now(timezone.utc)),
        SupplierObservationEvent("EV-3", "SUP-01", "sla_on_time_delivery", 0.0, timestamp=datetime.now(timezone.utc)),
    ]
    history = HistoricalPerformanceAnalyzer.build_profile("SUP-01", events=events)
    cand = _make_candidate("SUP-01", "Dist SLA")
    quote = QuoteNormalizer.from_evidence(cand.evidence)

    rel = ReliabilityEvaluator.evaluate(cand, history=history, quote=quote)
    assert rel.sla_compliance_rate == pytest.approx(2.0 / 3.0, rel=1e-2)
    assert len(rel.known_factors) > 0


def test_reliability_no_data_is_unknown_not_assumed():
    """Sin datos históricos ni SLA, el score de confiabilidad es UNKNOWN / None."""
    history = HistoricalPerformanceAnalyzer.build_profile("SUP-01", events=())
    cand = _make_candidate("SUP-01", "Dist Sin Historial")
    rel = ReliabilityEvaluator.evaluate(cand, history=history)

    assert rel.sla_compliance_rate is None
    assert rel.reliability_score is None
    assert "INSUFFICIENT_DATA_FOR_RELIABILITY_SCORE" in rel.unknown_factors


# =========================================================================
# 5. SUPPLIER RISK ENGINE & DETERMINISTIC SCORING & REJECTION
# =========================================================================

def test_risk_engine_dimensions_and_scoring():
    """Evalúa las 5 dimensiones y calcula un score ponderado determinista."""
    cand = _make_candidate("SUP-01", "Dist 1", lead_time_days=3, shipping_cost=Decimal("2000"), stock_available=True)
    quote = QuoteNormalizer.from_evidence(cand.evidence)
    lt_profile = LeadTimeAnalyzer.analyze_lead_time(3)
    ship_opt = ShippingAnalyzer.from_quote_and_payload(quote)
    history = HistoricalPerformanceAnalyzer.build_profile("SUP-01")
    rel = ReliabilityEvaluator.evaluate(cand, history)

    risk_prof = SupplierRiskEngine.evaluate_risk(cand, quote, lt_profile, ship_opt, rel, history)

    assert risk_prof.overall_risk_score is not None
    assert Decimal("0") <= risk_prof.overall_risk_score <= Decimal("100")
    assert risk_prof.operational_risk.dimension_name == "OPERATIONAL_RISK"
    assert risk_prof.logistics_risk.dimension_name == "LOGISTICS_RISK"
    assert risk_prof.availability_risk.dimension_name == "AVAILABILITY_RISK"
    assert risk_prof.evidence_risk.dimension_name == "EVIDENCE_RISK"
    assert risk_prof.commercial_risk.dimension_name == "COMMERCIAL_RISK"
    assert risk_prof.is_reject_recommended is False


def test_risk_engine_recommends_reject_on_out_of_stock():
    """Si stock_available es False confirmado, availability_risk es CRITICAL y recomienda REJECT."""
    cand = _make_candidate("SUP-OUT", "Dist Sin Stock", stock_available=False)
    quote = QuoteNormalizer.from_evidence(cand.evidence)
    lt_profile = LeadTimeAnalyzer.analyze_lead_time(3)
    ship_opt = ShippingAnalyzer.from_quote_and_payload(quote)
    history = HistoricalPerformanceAnalyzer.build_profile("SUP-OUT")
    rel = ReliabilityEvaluator.evaluate(cand, history)

    risk_prof = SupplierRiskEngine.evaluate_risk(cand, quote, lt_profile, ship_opt, rel, history)

    assert risk_prof.availability_risk.risk_level == RiskLevel.CRITICAL
    assert risk_prof.is_reject_recommended is True
    assert SupplierRejectionReason.OUT_OF_STOCK in risk_prof.rejection_reasons


def test_risk_engine_recommends_reject_on_recurrent_incidents():
    """Si el historial registra 3 o más incidentes graves, operational_risk es CRITICAL y recomienda REJECT."""
    events = [
        SupplierObservationEvent("EV-1", "SUP-INC", "incident", "defective_batch", timestamp=datetime.now(timezone.utc)),
        SupplierObservationEvent("EV-2", "SUP-INC", "incident", "delayed_2_weeks", timestamp=datetime.now(timezone.utc)),
        SupplierObservationEvent("EV-3", "SUP-INC", "incident", "wrong_items", timestamp=datetime.now(timezone.utc)),
    ]
    cand = _make_candidate("SUP-INC", "Dist Problematico")
    quote = QuoteNormalizer.from_evidence(cand.evidence)
    lt_profile = LeadTimeAnalyzer.analyze_lead_time(3)
    ship_opt = ShippingAnalyzer.from_quote_and_payload(quote)
    history = HistoricalPerformanceAnalyzer.build_profile("SUP-INC", events=events)
    rel = ReliabilityEvaluator.evaluate(cand, history)

    risk_prof = SupplierRiskEngine.evaluate_risk(cand, quote, lt_profile, ship_opt, rel, history)

    assert risk_prof.operational_risk.risk_level == RiskLevel.CRITICAL
    assert risk_prof.is_reject_recommended is True
    assert SupplierRejectionReason.HIGH_OPERATIONAL_RISK in risk_prof.rejection_reasons


# =========================================================================
# 6. SUPPLIER RISK COMPARATOR & BEST KNOWN SELECTION
# =========================================================================

def test_comparator_selects_best_supplier_considering_risk_not_just_price():
    """
    Demuestra el PRINCIPIO FUNDAMENTAL:
    No confundir PRECIO con CONVENIENCIA ni BEST COMMERCIAL con BEST SUPPLIER.
    El Proveedor A es más barato ($10.000) pero tiene 3 incidentes (riesgo crítico / rechazado).
    El Proveedor B es un poco más caro ($12.000) pero tiene historial impecable, lead time rápido y bajo riesgo.
    El comparador debe rechazar A y seleccionar B como best_supplier_candidate.
    """
    cand_a = _make_candidate("SUP-A", "Barato Pero Peligroso", wholesale_price=Decimal("10000"), lead_time_days=10)
    cand_b = _make_candidate("SUP-B", "Confiable Y Seguro", wholesale_price=Decimal("12000"), lead_time_days=2)

    events_a = [
        SupplierObservationEvent("EV-1", "SUP-A", "incident", "broken_goods", timestamp=datetime.now(timezone.utc)),
        SupplierObservationEvent("EV-2", "SUP-A", "incident", "late_delivery", timestamp=datetime.now(timezone.utc)),
        SupplierObservationEvent("EV-3", "SUP-A", "incident", "unresponsive", timestamp=datetime.now(timezone.utc)),
    ]
    events_b = [
        SupplierObservationEvent("EV-4", "SUP-B", "lead_time_days", 2, timestamp=datetime.now(timezone.utc)),
        SupplierObservationEvent("EV-5", "SUP-B", "lead_time_days", 2, timestamp=datetime.now(timezone.utc)),
    ]

    histories = {
        "SUP-A": events_a,
        "SUP-B": events_b,
    }

    result = SupplierRiskComparator.evaluate_and_compare(
        candidates=[cand_a, cand_b],
        target_product_title="Test Product",
        target_market_price=Decimal("25000"),
        supplier_histories=histories,
    )

    assert result.best_supplier_candidate is not None
    assert result.best_supplier_candidate.supplier_id == "SUP-B"
    assert result.best_supplier_candidate.supplier_name == "Confiable Y Seguro"
    
    # Verificar que SUP-A está en la lista de rechazados
    rejected_ids = [r.supplier.supplier_id for r in result.rejected_candidates]
    assert "SUP-A" in rejected_ids


# =========================================================================
# 7. PROVENANCE, TEMPORALITY & ANTI-FABRICATION
# =========================================================================

def test_provenance_preservation_fixture_vs_live():
    """Toda evaluación conserva inmutablemente la procedencia (FIXTURE vs LIVE)."""
    cand_fixture = _make_candidate("SUP-FIX", "Dist Fixture", provenance_type=EvidenceProvenanceType.FIXTURE)
    cand_live = _make_candidate("SUP-LIVE", "Dist Live", provenance_type=EvidenceProvenanceType.LIVE)

    quote_fix = QuoteNormalizer.from_evidence(cand_fixture.evidence)
    quote_live = QuoteNormalizer.from_evidence(cand_live.evidence)

    assert quote_fix.provenance_type == EvidenceProvenanceType.FIXTURE
    assert quote_live.provenance_type == EvidenceProvenanceType.LIVE

    ship_fix = ShippingAnalyzer.from_quote_and_payload(quote_fix)
    assert ship_fix.provenance_type == EvidenceProvenanceType.FIXTURE


def test_unknown_handling_audit():
    """UNKNOWN ≠ 0, UNKNOWN ≠ GOOD, UNKNOWN ≠ BAD."""
    cand_unknown = _make_candidate("SUP-UNK", "Dist Unknown", lead_time_days=None, shipping_cost=None)
    ev_unk = SupplierEvidence(
        supplier_id="SUP-UNK",
        sku="SKU-TEST",
        wholesale_price=Decimal("15000"),
        shipping_cost=None,
        lead_time_days=None,
        currency="CLP",
        confidence=Confidence.UNKNOWN,
        signal_type=SignalType.OBSERVED,
        provenance_type=EvidenceProvenanceType.FIXTURE,
        source="TEST",
    )
    cand_unk_clean = SupplierCandidate(
        supplier=cand_unknown.supplier,
        evidence=ev_unk,
        product_match=cand_unknown.product_match,
        readiness=SupplierReadiness.EVALUATED,
    )
    quote = QuoteNormalizer.from_evidence(cand_unk_clean.evidence)
    lt_profile = LeadTimeAnalyzer.analyze_lead_time(None)
    ship = ShippingAnalyzer.from_quote_and_payload(quote)
    history = HistoricalPerformanceAnalyzer.build_profile("SUP-UNK")
    rel = ReliabilityEvaluator.evaluate(cand_unk_clean, history)
    risk_prof = SupplierRiskEngine.evaluate_risk(cand_unk_clean, quote, lt_profile, ship, rel, history)

    # Las incógnitas deben estar explícitamente declaradas
    assert "LEAD_TIME_UNKNOWN" in lt_profile.unknowns
    assert "SHIPPING_COST_UNKNOWN" in ship.unknowns
    assert "INSUFFICIENT_DATA_FOR_RELIABILITY_SCORE" in rel.unknown_factors
    assert len(risk_prof.unknowns) > 0
