"""
Tests unitarios exhaustivos para J.4 Change Detection (Escenarios A a AC).

A. price change
B. stock change
C. availability change
D. sold quantity change
E. competition change
F. categorical transition
G. no change
H. UNKNOWN previous
I. UNKNOWN current
J. both UNKNOWN
K. numeric delta
L. percentage delta
M. previous zero
N. temporal ordering
O. equal timestamp
P. out-of-order input
Q. deterministic previous state
R. duplicate observation
S. idempotent replay
T. provenance
U. evidence references
V. correlation
W. sensitive data exclusion
X. restart/reload
Y. opportunity status change
Z. opportunity score change
AA. source failure
AB. J.4 does not call marketplace
AC. J.4 does not create Decision/Action/Alert/Event
"""

import pytest
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import shutil
import json

from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    ObservationSourceType,
    NormalizedPrice,
    ObservedSellerInfo,
    ObservedCompetitionInfo,
)
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
)
from src.domain.change_detection.models import (
    ChangeRecord,
    ChangeSubjectType,
    ChangeType,
    ChangeSignificance,
    ObservedChangeField,
    DerivedChangeDelta,
)
from src.domain.change_detection.engine import (
    ChangeDetectionEngine,
    TemporalOrderViolationError,
    InvalidSubjectComparisonError,
)
from src.infrastructure.persistence.data.json.change_repository import (
    JsonChangeRecordRepository,
    CorruptedChangeRecordDataError,
)
from src.application.change_detection.service import ChangeDetectionService
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="test_j4_unit_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def engine():
    return ChangeDetectionEngine()


def _make_obs(
    obs_id: str,
    entity_id: str,
    timestamp: datetime,
    price_amt: Optional[str] = None,
    stock: Optional[int] = None,
    sold_qty: Optional[int] = None,
    availability: Optional[str] = "IN_STOCK",
    status: ObservationStatus = ObservationStatus.SUCCESS,
    competitors: Optional[int] = None,
    seller_id: Optional[str] = "seller-123",
) -> MarketObservation:
    price = NormalizedPrice(amount=Decimal(price_amt), currency="CLP") if price_amt is not None else None
    comp_info = ObservedCompetitionInfo(total_competitors=competitors) if competitors is not None else None
    seller_info = ObservedSellerInfo(seller_id=seller_id) if seller_id is not None else None

    return MarketObservation(
        observation_id=obs_id,
        source="MERCADOLIBRE_LIVE",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=timestamp,
        collected_at=timestamp,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id=entity_id,
        status=status,
        price=price,
        stock=stock,
        sold_quantity=sold_qty,
        availability=availability,
        competition_info=comp_info,
        seller_info=seller_info,
    )


# --- Tests A to AC ---

def test_a_price_change(engine):
    """A. price change: Price goes from 100 to 90 -> PRICE_CHANGED."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    change = engine.compare_observations(obs0, obs1)
    assert change.change_type == ChangeType.PRICE_CHANGED
    assert "price" in change.changed_fields
    assert len(change.derived_deltas) == 1
    assert change.derived_deltas[0].numeric_delta == Decimal("-10.00")
    assert change.derived_deltas[0].percentage_delta == Decimal("-10.00")


def test_b_stock_change(engine):
    """B. stock change: Stock goes from 10 to 5 -> STOCK_CHANGED."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00", stock=10)
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="100.00", stock=5)

    change = engine.compare_observations(obs0, obs1)
    assert change.change_type == ChangeType.STOCK_CHANGED
    assert "stock" in change.changed_fields
    assert change.derived_deltas[0].numeric_delta == Decimal("-5")


def test_c_availability_change(engine):
    """C. availability change: IN_STOCK -> OUT_OF_STOCK -> AVAILABILITY_CHANGED, CRITICAL."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, availability="IN_STOCK")
    obs1 = _make_obs("obs-1", "MLC-100", t1, availability="OUT_OF_STOCK")

    change = engine.compare_observations(obs0, obs1)
    assert change.change_type == ChangeType.AVAILABILITY_CHANGED
    assert "availability" in change.changed_fields
    assert change.significance == ChangeSignificance.CRITICAL


def test_d_sold_quantity_change(engine):
    """D. sold quantity change: 100 -> 120 -> SOLD_QUANTITY_CHANGED."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, sold_qty=100)
    obs1 = _make_obs("obs-1", "MLC-100", t1, sold_qty=120)

    change = engine.compare_observations(obs0, obs1)
    assert change.change_type == ChangeType.SOLD_QUANTITY_CHANGED
    assert "sold_quantity" in change.changed_fields
    assert change.derived_deltas[0].numeric_delta == Decimal("20")
    assert change.derived_deltas[0].percentage_delta == Decimal("20.00")


def test_e_competition_change(engine):
    """E. competition change: 3 competitors -> 5 competitors -> COMPETITION_CHANGED."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, competitors=3)
    obs1 = _make_obs("obs-1", "MLC-100", t1, competitors=5)

    change = engine.compare_observations(obs0, obs1)
    assert change.change_type == ChangeType.COMPETITION_CHANGED
    assert "competition" in change.changed_fields
    assert change.derived_deltas[0].numeric_delta == Decimal("2")


def test_f_categorical_transition(engine):
    """F. categorical transition: seller change or status change without fabricating numeric delta."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, seller_id="seller-A")
    obs1 = _make_obs("obs-1", "MLC-100", t1, seller_id="seller-B")

    change = engine.compare_observations(obs0, obs1)
    assert change.change_type == ChangeType.SELLER_CHANGED
    assert "seller" in change.changed_fields
    assert len(change.derived_deltas) == 0  # No numeric delta fabricated


def test_g_no_change(engine):
    """G. no change: identical values produce NO_CHANGE."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00", stock=10)
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="100.00", stock=10)

    change = engine.compare_observations(obs0, obs1)
    assert change.change_type == ChangeType.NO_CHANGE
    assert len(change.changed_fields) == 0
    assert change.significance == ChangeSignificance.NONE


def test_h_unknown_previous(engine):
    """H. UNKNOWN previous: price was UNKNOWN at T0, now 100 at T1 -> no numeric delta fabricated."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt=None)
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="100.00")

    change = engine.compare_observations(obs0, obs1)
    assert "price" in change.changed_fields
    obs_price = [c for c in change.observed_changes if c.field_name == "price"][0]
    assert obs_price.is_previous_unknown is True
    assert obs_price.is_current_unknown is False
    assert change.derived_deltas[0].is_valid_delta is False
    assert change.derived_deltas[0].numeric_delta is None


def test_i_unknown_current(engine):
    """I. UNKNOWN current: price was 100 at T0, now UNKNOWN at T1 -> no numeric delta fabricated."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt=None)

    change = engine.compare_observations(obs0, obs1)
    assert "price" in change.changed_fields
    obs_price = [c for c in change.observed_changes if c.field_name == "price"][0]
    assert obs_price.is_previous_unknown is False
    assert obs_price.is_current_unknown is True
    assert change.derived_deltas[0].is_valid_delta is False


def test_j_both_unknown(engine):
    """J. both UNKNOWN: price is None at T0 and T1 -> not considered changed."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt=None)
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt=None)

    change = engine.compare_observations(obs0, obs1)
    assert "price" not in change.changed_fields


def test_k_numeric_delta(engine):
    """K. numeric delta: 50 -> 75 = delta +25."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="50.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="75.00")

    change = engine.compare_observations(obs0, obs1)
    delta = change.derived_deltas[0]
    assert delta.numeric_delta == Decimal("25.00")


def test_l_percentage_delta(engine):
    """L. percentage delta: 50 -> 75 = +50.00%."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="50.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="75.00")

    change = engine.compare_observations(obs0, obs1)
    delta = change.derived_deltas[0]
    assert delta.percentage_delta == Decimal("50.00")


def test_m_previous_zero(engine):
    """M. previous zero: 0 -> 10 sold quantity handles zero division without crashing."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, sold_qty=0)
    obs1 = _make_obs("obs-1", "MLC-100", t1, sold_qty=10)

    change = engine.compare_observations(obs0, obs1)
    delta = change.derived_deltas[0]
    assert delta.numeric_delta == Decimal("10")
    assert delta.percentage_delta is None  # Div zero avoided


def test_n_temporal_ordering(engine):
    """N. temporal ordering: previous observed_at > current observed_at raises error."""
    t0 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    with pytest.raises(TemporalOrderViolationError):
        engine.compare_observations(obs0, obs1)


def test_o_equal_timestamp(engine):
    """O. equal timestamp with different observation_ids raises TemporalOrderViolationError for ambiguity."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t0, price_amt="90.00")

    with pytest.raises(TemporalOrderViolationError):
        engine.compare_observations(obs0, obs1)


def test_p_out_of_order_input(temp_dir, engine):
    """P. out-of-order input: Service reorders observations chronologically before evaluating changes."""
    repo = JsonChangeRecordRepository(temp_dir)
    service = ChangeDetectionService(change_repository=repo, engine=engine)

    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="110.00")
    obs2 = _make_obs("obs-2", "MLC-100", t2, price_amt="120.00")

    # Pass in jumbled order: [obs2, obs0, obs1]
    changes = service.detect_observation_changes([obs2, obs0, obs1])
    assert len(changes) == 3
    # First is baseline for obs0
    assert changes[0].current_reference == "obs-0"
    # Second is change obs0 -> obs1
    assert changes[1].previous_reference == "obs-0"
    assert changes[1].current_reference == "obs-1"
    # Third is change obs1 -> obs2
    assert changes[2].previous_reference == "obs-1"
    assert changes[2].current_reference == "obs-2"


def test_q_deterministic_previous_state(engine):
    """Q. deterministic previous state: Same observations yield exactly identical ChangeRecord fields."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    c1 = engine.compare_observations(obs0, obs1, correlation_id="c-1")
    c2 = engine.compare_observations(obs0, obs1, correlation_id="c-1")

    assert c1.change_type == c2.change_type
    assert c1.changed_fields == c2.changed_fields
    assert c1.idempotency_key == c2.idempotency_key


def test_r_duplicate_observation(engine):
    """R. duplicate observation: Evaluating same observation produces NO_CHANGE."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")

    change = engine.compare_observations(obs0, obs0)
    assert change.change_type == ChangeType.NO_CHANGE


def test_s_idempotent_replay(temp_dir, engine):
    """S. idempotent replay: Saving the same change multiple times stores exactly 1 record."""
    repo = JsonChangeRecordRepository(temp_dir)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    change = engine.compare_observations(obs0, obs1)
    repo.save(change)
    repo.save(change)

    all_records = repo.list_all()
    assert len(all_records) == 1


def test_t_provenance(engine):
    """T. provenance: ChangeRecord correctly marks provenance as DERIVED."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    change = engine.compare_observations(obs0, obs1)
    assert change.provenance == "DERIVED"


def test_u_evidence_references(engine):
    """U. evidence references: ChangeRecord preserves references to source observations."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    change = engine.compare_observations(obs0, obs1)
    assert "obs-0" in change.evidence_references
    assert "obs-1" in change.evidence_references


def test_v_correlation(engine):
    """V. correlation: ChangeRecord preserves correlation_id across comparisons."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    change = engine.compare_observations(obs0, obs1, correlation_id="test-corr-1234")
    assert change.correlation_id == "test-corr-1234"


def test_w_sensitive_data_exclusion(temp_dir, engine):
    """W. sensitive data exclusion: Sanitizes secrets from metadata when saving."""
    repo = JsonChangeRecordRepository(temp_dir)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    change = engine.compare_observations(obs0, obs1)
    # Inject metadata with sensitive key
    object.__setattr__(change, "metadata", {"api_key": "SECRET-123", "safe_note": "public"})
    repo.save(change)

    # Check raw JSON on disk
    file_path = temp_dir / f"{change.change_id}.json"
    with open(file_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["metadata"]["api_key"] == "[REDACTED]"
    assert raw["metadata"]["safe_note"] == "public"


def test_x_restart_reload(temp_dir, engine):
    """X. restart/reload: Destroy repo, re-instantiate, reload records without data loss."""
    repo1 = JsonChangeRecordRepository(temp_dir)
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    change = engine.compare_observations(obs0, obs1)
    repo1.save(change)

    # Destroy and recreate repo
    del repo1
    repo2 = JsonChangeRecordRepository(temp_dir)
    loaded = repo2.get_by_id(change.change_id)

    assert loaded is not None
    assert loaded.change_id == change.change_id
    assert loaded.change_type == ChangeType.PRICE_CHANGED
    assert loaded.derived_deltas[0].numeric_delta == Decimal("-10.00")


def test_y_opportunity_status_change(engine):
    """Y. opportunity status change: VALID -> INVALID detected on OpportunityRecord."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    opp0 = OpportunityRecord(
        opportunity_id="opp-0",
        canonical_product_id="PROD-1",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=t0,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-0",),
        observed_metrics=ObservedOpportunityMetrics(observations_count=1),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("80.0")),
    )
    opp1 = OpportunityRecord(
        opportunity_id="opp-1",
        canonical_product_id="PROD-1",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=t1,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.INVALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-1",),
        observed_metrics=ObservedOpportunityMetrics(observations_count=1),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("80.0")),
    )

    change = engine.compare_opportunities(opp0, opp1)
    assert change.change_type == ChangeType.OPPORTUNITY_STATUS_CHANGED
    assert "status" in change.changed_fields


def test_z_opportunity_score_change(engine):
    """Z. opportunity score change: 80.0 -> 60.0 detected with numeric delta -20.0."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    opp0 = OpportunityRecord(
        opportunity_id="opp-0",
        canonical_product_id="PROD-1",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=t0,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-0",),
        observed_metrics=ObservedOpportunityMetrics(observations_count=1),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("80.0")),
    )
    opp1 = OpportunityRecord(
        opportunity_id="opp-1",
        canonical_product_id="PROD-1",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=t1,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-1",),
        observed_metrics=ObservedOpportunityMetrics(observations_count=1),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("60.0")),
    )

    change = engine.compare_opportunities(opp0, opp1)
    assert change.change_type == ChangeType.OPPORTUNITY_SCORE_CHANGED
    assert "opportunity_score" in change.changed_fields
    assert change.derived_deltas[0].numeric_delta == Decimal("-20.0")


def test_aa_source_failure(engine):
    """AA. source failure: Timeout / failure does not fabricate commercial price drop."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00", status=ObservationStatus.SUCCESS)
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt=None, status=ObservationStatus.TIMEOUT)

    change = engine.compare_observations(obs0, obs1)
    assert change.change_type == ChangeType.SOURCE_STATUS_CHANGED
    assert "status" in change.changed_fields
    # Ensure no fabricated price drop delta
    price_deltas = [d for d in change.derived_deltas if d.field_name == "price"]
    assert len(price_deltas) == 0


def test_ab_j4_does_not_call_marketplace():
    """AB. J.4 does not call marketplace directly: verifies modules do not import or invoke http clients."""
    import src.domain.change_detection.engine as cde
    import src.application.change_detection.service as cds
    import inspect

    # Verify no HTTP clients in imports
    assert "requests" not in sys_modules_check(cde)
    assert "urllib" not in sys_modules_check(cde)
    assert "httpx" not in sys_modules_check(cde)
    assert "requests" not in sys_modules_check(cds)


def test_ac_j4_does_not_create_decision_or_alert(engine):
    """AC. J.4 does not create Decision/Action/Alert/Event: output is strictly ChangeRecord."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _make_obs("obs-0", "MLC-100", t0, price_amt="100.00")
    obs1 = _make_obs("obs-1", "MLC-100", t1, price_amt="90.00")

    result = engine.compare_observations(obs0, obs1)
    assert isinstance(result, ChangeRecord)
    assert not hasattr(result, "action_type")
    assert not hasattr(result, "decision_type")
    assert not hasattr(result, "alert_level")


def sys_modules_check(module):
    return [name for name in dir(module)]
