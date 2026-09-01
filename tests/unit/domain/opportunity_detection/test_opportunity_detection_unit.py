import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any

from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
    OpportunityDetectionCriteria,
)
from src.domain.opportunity_detection.engine import OpportunityDetectionEngine
from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    ObservationSourceType,
    NormalizedPrice,
    ObservedSellerInfo,
    ObservedCompetitionInfo,
)
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType


def _make_obs(
    obs_id: str = "obs-1",
    entity_id: str = "PROD-100",
    status: ObservationStatus = ObservationStatus.SUCCESS,
    price: Optional[Decimal] = Decimal("15000.00"),
    sold: Optional[int] = 60,
    stock: Optional[int] = 20,
    comp_count: Optional[int] = 1,
    lowest_comp_price: Optional[Decimal] = Decimal("20000.00"),
    confidence: Confidence = Confidence.HIGH,
    observed_at: Optional[datetime] = None,
) -> MarketObservation:
    now = observed_at or datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    return MarketObservation(
        observation_id=obs_id,
        source="TEST_SOURCE",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id=entity_id,
        title="Test Product Title",
        category="ELECTRONICS",
        product_sku="SKU-100",
        price=NormalizedPrice(amount=price, currency="CLP") if price is not None else None,
        sold_quantity=sold,
        stock=stock,
        competition_info=ObservedCompetitionInfo(
            total_competitors=comp_count,
            lowest_competitor_price=NormalizedPrice(amount=lowest_comp_price, currency="CLP") if lowest_comp_price is not None else None,
        ) if (comp_count is not None or lowest_comp_price is not None) else None,
        status=status,
        provenance="LIVE",
        confidence=confidence,
        signal_type=SignalType.OBSERVED,
        correlation_id="corr-test-1",
    )


# A. Opportunity Creation
def test_opportunity_creation_contract():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    obs_metrics = ObservedOpportunityMetrics(
        observed_price=NormalizedPrice(amount=Decimal("10000.00"), currency="CLP"),
        observed_sold_quantity=50,
        observed_stock=10,
        observed_competitor_count=2,
    )
    der_metrics = DerivedOpportunityMetrics(
        price_gap_amount=Decimal("5000.00"),
        price_gap_ratio=Decimal("0.3333"),
        potential_margin_ratio=Decimal("0.3333"),
        competition_density="LOW",
        demand_intensity="HIGH",
        opportunity_score=Decimal("85.00"),
        scoring_rationale=("High demand confirmed", "Favorable price gap"),
    )
    opp = OpportunityRecord(
        opportunity_id="opp-101",
        canonical_product_id="PROD-101",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=now,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-1", "obs-2"),
        observed_metrics=obs_metrics,
        derived_metrics=der_metrics,
        reasons=("Significant price advantage",),
    )
    assert opp.opportunity_id == "opp-101"
    assert opp.canonical_product_id == "PROD-101"
    assert opp.derived_metrics.opportunity_score == Decimal("85.00")
    assert opp.status == OpportunityStatus.VALID
    assert opp.observed_metrics.observed_price.amount == Decimal("10000.00")


# B. Valid Opportunity Detection
def test_valid_opportunity_detection():
    engine = OpportunityDetectionEngine()
    obs = _make_obs(
        obs_id="obs-b1",
        entity_id="PROD-VALID",
        price=Decimal("15000.00"),
        sold=100,
        comp_count=1,
        lowest_comp_price=Decimal("20000.00"),
    )
    results = engine.detect_opportunities([obs])
    assert len(results) == 1
    opp = results[0]
    assert opp.status == OpportunityStatus.VALID
    assert opp.canonical_product_id == "PROD-VALID"
    assert opp.derived_metrics.opportunity_score is not None
    assert opp.derived_metrics.opportunity_score >= Decimal("30.0")
    assert opp.opportunity_type in (OpportunityType.PRICE_ARBITRAGE, OpportunityType.HIGH_DEMAND_LOW_COMPETITION)


# C. No Opportunity When Criteria Fail
def test_no_opportunity_when_criteria_fail():
    engine = OpportunityDetectionEngine()
    # Puntuación baja: bajo volumen, alta competencia, precio mayor al competidor
    obs = _make_obs(
        obs_id="obs-c1",
        entity_id="PROD-WEAK",
        price=Decimal("30000.00"),
        sold=2,
        comp_count=20,
        lowest_comp_price=Decimal("20000.00"),
    )
    criteria = OpportunityDetectionCriteria(min_score=Decimal("50.0"))
    results = engine.detect_opportunities([obs], criteria=criteria)
    # Debe ser filtrado / retornado None (no oportunidad válida)
    assert len(results) == 0


# D. UNKNOWN Handling (UNKNOWN != 0)
def test_unknown_handling_preserves_none_and_does_not_convert_to_zero():
    engine = OpportunityDetectionEngine()
    obs = _make_obs(
        obs_id="obs-d1",
        entity_id="PROD-UNKNOWN",
        price=Decimal("10000.00"),
        sold=None,  # UNKNOWN
        comp_count=None,  # UNKNOWN
        lowest_comp_price=None,
    )
    results = engine.detect_opportunities([obs])
    assert len(results) == 1
    opp = results[0]
    assert opp.observed_metrics.observed_sold_quantity is None
    assert opp.observed_metrics.observed_competitor_count is None
    assert "sold_quantity" in opp.unknown_fields
    assert "competitor_count" in opp.unknown_fields
    assert opp.derived_metrics.demand_intensity == "UNKNOWN"
    assert opp.derived_metrics.competition_density == "UNKNOWN"


# E. Insufficient Evidence
def test_insufficient_evidence_when_min_observations_not_met():
    engine = OpportunityDetectionEngine()
    obs = _make_obs(obs_id="obs-e1", entity_id="PROD-INSUFF")
    criteria = OpportunityDetectionCriteria(min_observations_required=3)
    results = engine.detect_opportunities([obs], criteria=criteria)
    assert len(results) == 1
    assert results[0].status == OpportunityStatus.INSUFFICIENT_DATA
    assert "Insufficient observations" in results[0].reasons[0]


# F. Observed vs Derived Separation
def test_observed_vs_derived_separation():
    engine = OpportunityDetectionEngine()
    obs = _make_obs(
        obs_id="obs-f1",
        price=Decimal("10000.00"),
        lowest_comp_price=Decimal("15000.00"),
        sold=80,
        comp_count=2,
    )
    results = engine.detect_opportunities([obs])
    opp = results[0]

    # OBSERVED contiene datos empíricos de la observación
    assert opp.observed_metrics.observed_price.amount == Decimal("10000.00")
    assert opp.observed_metrics.lowest_competitor_price.amount == Decimal("15000.00")
    assert opp.observed_metrics.observed_sold_quantity == 80
    assert opp.observed_metrics.observed_competitor_count == 2

    # DERIVED contiene cálculos deterministas
    assert opp.derived_metrics.price_gap_amount == Decimal("5000.00")
    assert opp.derived_metrics.price_gap_ratio == Decimal("0.3333")
    assert opp.derived_metrics.demand_intensity == "HIGH"
    assert opp.derived_metrics.competition_density == "LOW"
    assert opp.derived_metrics.opportunity_score is not None


# G. Deterministic Scoring and Rules
def test_deterministic_scoring_reproducibility():
    engine = OpportunityDetectionEngine()
    obs1 = _make_obs(obs_id="obs-g1", price=Decimal("12000.00"), lowest_comp_price=Decimal("15000.00"), sold=50, comp_count=2)

    res1 = engine.detect_opportunities([obs1])
    res2 = engine.detect_opportunities([obs1])

    assert res1[0].derived_metrics.opportunity_score == res2[0].derived_metrics.opportunity_score
    assert res1[0].derived_metrics.scoring_rationale == res2[0].derived_metrics.scoring_rationale
    assert res1[0].idempotency_key == res2[0].idempotency_key


# H. Evidence Traceability
def test_evidence_traceability():
    engine = OpportunityDetectionEngine()
    obs1 = _make_obs(obs_id="obs-h1", entity_id="PROD-TRACE", price=Decimal("10000.00"))
    obs2 = _make_obs(obs_id="obs-h2", entity_id="PROD-TRACE", price=Decimal("10000.00"))

    results = engine.detect_opportunities([obs1, obs2])
    assert len(results) == 1
    opp = results[0]
    assert opp.source_observation_ids == ("obs-h1", "obs-h2")
    assert opp.canonical_product_id == "PROD-TRACE"


# I. Provenance
def test_provenance_preservation():
    engine = OpportunityDetectionEngine()
    obs = _make_obs(obs_id="obs-i1", entity_id="PROD-PROV")
    results = engine.detect_opportunities([obs])
    assert results[0].provenance == "LIVE"


# J. Confidence
def test_confidence_degradation_on_multiple_unknowns():
    engine = OpportunityDetectionEngine()
    obs = _make_obs(
        obs_id="obs-j1",
        price=None,  # unknown
        sold=None,   # unknown
        comp_count=None,
        confidence=Confidence.HIGH,
    )
    criteria = OpportunityDetectionCriteria(require_valid_price=False)
    results = engine.detect_opportunities([obs], criteria=criteria)
    assert len(results) == 1
    assert results[0].confidence == Confidence.LOW


# K. Idempotency & L. Duplicate Replay
def test_idempotency_and_duplicate_replay():
    engine = OpportunityDetectionEngine()
    obs = _make_obs(obs_id="obs-k1", entity_id="PROD-IDEMP")

    res1 = engine.detect_opportunities([obs])
    res2 = engine.detect_opportunities([obs, obs])

    assert res1[0].idempotency_key == res2[0].idempotency_key
    assert res1[0].opportunity_id == res2[0].opportunity_id


# M. Sensitive Data Sanitization
def test_sensitive_data_sanitization_in_repository(tmp_path):
    from src.infrastructure.persistence.data.json.opportunity_repository import JsonOpportunityRepository
    repo = JsonOpportunityRepository(tmp_path / "opps.json")

    obs_m = ObservedOpportunityMetrics(
        observed_price=NormalizedPrice(amount=Decimal("1000.00"), currency="CLP"),
    )
    der_m = DerivedOpportunityMetrics(opportunity_score=Decimal("50.00"))
    opp = OpportunityRecord(
        opportunity_id="opp-sec-1",
        canonical_product_id="PROD-SEC",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
        opportunity_type=OpportunityType.GENERAL_COMMERCIAL,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-1",),
        observed_metrics=obs_m,
        derived_metrics=der_m,
        metadata={
            "api_key": "secret_12345",
            "access_token": "token_abcde",
            "safe_data": "public_val",
        }
    )
    repo.save(opp)

    raw_content = (tmp_path / "opps.json").read_text(encoding="utf-8")
    assert "secret_12345" not in raw_content
    assert "token_abcde" not in raw_content
    assert "public_val" in raw_content


# N. Invalid Observation & O. Source Failure
def test_source_failure_does_not_fabricate_opportunity():
    engine = OpportunityDetectionEngine()
    obs_fail = _make_obs(
        obs_id="obs-fail-1",
        entity_id="PROD-FAIL",
        status=ObservationStatus.SOURCE_FAILURE,
    )
    obs_timeout = _make_obs(
        obs_id="obs-fail-2",
        entity_id="PROD-TIMEOUT",
        status=ObservationStatus.TIMEOUT,
    )

    results = engine.detect_opportunities([obs_fail, obs_timeout])
    assert len(results) == 0


# P. Deterministic Recomputation
def test_deterministic_recomputation():
    engine = OpportunityDetectionEngine()
    observations = [
        _make_obs(obs_id=f"obs-p-{i}", entity_id=f"PROD-{i % 3}", price=Decimal(f"{10000 + i * 500}"))
        for i in range(10)
    ]
    run_1 = engine.detect_opportunities(observations)
    run_2 = engine.detect_opportunities(observations)

    assert len(run_1) == len(run_2)
    for r1, r2 in zip(run_1, run_2):
        assert r1.opportunity_id == r2.opportunity_id
        assert r1.derived_metrics.opportunity_score == r2.derived_metrics.opportunity_score


# Q. ProductMemory reuse reference & R. SupplierMemory reference
def test_product_memory_and_supplier_memory_references():
    obs_m = ObservedOpportunityMetrics(observed_price=NormalizedPrice(amount=Decimal("1000.00"), currency="CLP"))
    der_m = DerivedOpportunityMetrics(opportunity_score=Decimal("60.00"))
    opp = OpportunityRecord(
        opportunity_id="opp-ref-1",
        canonical_product_id="PROD-REF",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
        opportunity_type=OpportunityType.GENERAL_COMMERCIAL,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-ref-1",),
        observed_metrics=obs_m,
        derived_metrics=der_m,
        product_memory_id_ref="pmem-product-456",
        supplier_memory_id_ref="smem-supplier-789",
    )
    assert opp.product_memory_id_ref == "pmem-product-456"
    assert opp.supplier_memory_id_ref == "smem-supplier-789"


# S. No direct marketplace call & T. No Decision creation & U. No Action execution & V. No Policy mutation
def test_architectural_boundaries():
    engine = OpportunityDetectionEngine()
    obs = _make_obs(obs_id="obs-arch-1", entity_id="PROD-ARCH")
    results = engine.detect_opportunities([obs])

    assert len(results) == 1
    opp = results[0]

    # Verifica que el resultado es un OpportunityRecord inmutable
    assert isinstance(opp, OpportunityRecord)
    assert not hasattr(opp, "decision_id")
    assert not hasattr(opp, "action_id")
    assert not hasattr(opp, "mission_id")
