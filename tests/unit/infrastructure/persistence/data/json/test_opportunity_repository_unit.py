import pytest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
)
from src.infrastructure.persistence.data.json.opportunity_repository import (
    JsonOpportunityRepository,
    CorruptedOpportunityDataError,
)
from src.domain.market_monitoring.models import NormalizedPrice
from src.domain.market_intelligence.models import Marketplace, Confidence


def _create_sample_opportunity(
    opp_id: str = "opp-test-1",
    product_id: str = "PROD-1",
    opp_type: OpportunityType = OpportunityType.PRICE_ARBITRAGE,
    status: OpportunityStatus = OpportunityStatus.VALID,
    score: Decimal = Decimal("75.50"),
) -> OpportunityRecord:
    obs_m = ObservedOpportunityMetrics(
        observed_price=NormalizedPrice(amount=Decimal("15000.00"), currency="CLP"),
        observed_sold_quantity=100,
        observed_stock=50,
        observed_competitor_count=3,
        lowest_competitor_price=NormalizedPrice(amount=Decimal("20000.00"), currency="CLP"),
    )
    der_m = DerivedOpportunityMetrics(
        price_gap_amount=Decimal("5000.00"),
        price_gap_ratio=Decimal("0.2500"),
        potential_margin_ratio=Decimal("0.2500"),
        competition_density="LOW",
        demand_intensity="HIGH",
        opportunity_score=score,
        scoring_rationale=("Price gap advantage", "High demand volume"),
    )
    return OpportunityRecord(
        opportunity_id=opp_id,
        canonical_product_id=product_id,
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
        opportunity_type=opp_type,
        status=status,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-1", "obs-2"),
        observed_metrics=obs_m,
        derived_metrics=der_m,
        category="ELECTRONICS",
        title="Sample Product Title",
        product_sku="SKU-1234",
        idempotency_key=f"idemp-{opp_id}",
        reasons=("Valid commercial opportunity",),
    )


def test_repository_save_and_get_by_id(tmp_path):
    repo = JsonOpportunityRepository(tmp_path / "repo.json")
    opp = _create_sample_opportunity(opp_id="opp-100")

    repo.save(opp)

    retrieved = repo.get_by_id("opp-100")
    assert retrieved is not None
    assert retrieved.opportunity_id == "opp-100"
    assert retrieved.canonical_product_id == "PROD-1"
    assert retrieved.derived_metrics.opportunity_score == Decimal("75.50")
    assert retrieved.observed_metrics.observed_price.amount == Decimal("15000.00")
    assert retrieved.observed_metrics.lowest_competitor_price.amount == Decimal("20000.00")


def test_repository_save_all_and_idempotency(tmp_path):
    repo = JsonOpportunityRepository(tmp_path / "repo.json")
    opp1 = _create_sample_opportunity(opp_id="opp-1", product_id="P1")
    opp2 = _create_sample_opportunity(opp_id="opp-2", product_id="P2")

    added_count = repo.save_all([opp1, opp2])
    assert added_count == 2
    assert len(repo.list_all()) == 2

    # Repetir guardado idéntico -> no duplica
    added_second = repo.save_all([opp1, opp2])
    assert added_second == 0
    assert len(repo.list_all()) == 2


def test_repository_get_by_idempotency_key(tmp_path):
    repo = JsonOpportunityRepository(tmp_path / "repo.json")
    opp = _create_sample_opportunity(opp_id="opp-idemp-1")
    repo.save(opp)

    retrieved = repo.get_by_idempotency_key("idemp-opp-idemp-1")
    assert retrieved is not None
    assert retrieved.opportunity_id == "opp-idemp-1"


def test_repository_list_by_product_type_status(tmp_path):
    repo = JsonOpportunityRepository(tmp_path / "repo.json")
    opp1 = _create_sample_opportunity(opp_id="opp-1", product_id="PROD-A", opp_type=OpportunityType.PRICE_ARBITRAGE, status=OpportunityStatus.VALID)
    opp2 = _create_sample_opportunity(opp_id="opp-2", product_id="PROD-A", opp_type=OpportunityType.HIGH_DEMAND_LOW_COMPETITION, status=OpportunityStatus.VALID)
    opp3 = _create_sample_opportunity(opp_id="opp-3", product_id="PROD-B", opp_type=OpportunityType.PRICE_ARBITRAGE, status=OpportunityStatus.INSUFFICIENT_DATA)

    repo.save_all([opp1, opp2, opp3])

    by_prod = repo.list_by_product("PROD-A")
    assert len(by_prod) == 2

    by_type = repo.list_by_type(OpportunityType.PRICE_ARBITRAGE)
    assert len(by_type) == 2

    by_status = repo.list_by_status(OpportunityStatus.INSUFFICIENT_DATA)
    assert len(by_status) == 1
    assert by_status[0].opportunity_id == "opp-3"


def test_repository_corrupted_file(tmp_path):
    file_path = tmp_path / "corrupted.json"
    file_path.write_text("{ broken json content", encoding="utf-8")

    repo = JsonOpportunityRepository(file_path)
    with pytest.raises(CorruptedOpportunityDataError):
        repo.list_all()
