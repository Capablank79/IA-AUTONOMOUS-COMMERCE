"""
Tests de integración y E2E para J.4 Change Detection.

Cadena obligatoria:
J.2 OBSERVATION T0 -> PERSIST -> J.2 OBSERVATION T1 -> J.4 CHANGE DETECTION -> CHANGE RECORD -> PERSIST -> RELOAD
Y para Oportunidades:
J.3 OPPORTUNITY T0 -> OPPORTUNITY T1 -> J.4 CHANGE RECORD

Escenarios E2E:
- Escenario A — Price Change (T0 price 100, T1 price 90 -> detected change)
- Escenario B — No Change (same canonical state -> no false change)
- Escenario C — UNKNOWN (T0 known, T1 unknown -> no fabricated commercial delta)
- Escenario D — Restart (T0/T1/change persisted -> recreate services -> reload history)
- Escenario E — Duplicate (same T1 replayed -> one logical change)
- Escenario F — Out of Order (T2 before T1 -> deterministic safe behavior)
- Escenario G — Opportunity Change (opportunity status/score transition -> traceable change)
- Escenario H — Source Failure (timeout observation -> no false market change)
- Escenario I — Security (secret in metadata -> sanitized persistence)
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from decimal import Decimal
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
)
from src.domain.change_detection.engine import ChangeDetectionEngine
from src.infrastructure.persistence.data.json.change_repository import JsonChangeRecordRepository
from src.infrastructure.persistence.data.json.market_observation_repository import JsonMarketObservationRepository
from src.infrastructure.persistence.data.json.opportunity_repository import JsonOpportunityRepository
from src.application.change_detection.service import ChangeDetectionService
from src.domain.market_intelligence.models import Marketplace, Confidence


@pytest.fixture
def temp_environment():
    temp_dir = Path(tempfile.mkdtemp(prefix="test_j4_integration_"))
    obs_dir = temp_dir / "observations"
    opp_file = temp_dir / "opportunities" / "opportunities.json"
    change_dir = temp_dir / "changes"

    obs_repo = JsonMarketObservationRepository(storage_dir=obs_dir)
    opp_repo = JsonOpportunityRepository(file_path=opp_file)
    change_repo = JsonChangeRecordRepository(storage_dir=change_dir)
    engine = ChangeDetectionEngine()

    service = ChangeDetectionService(
        engine=engine,
        change_repository=change_repo,
        observation_repository=obs_repo,
        opportunity_repository=opp_repo,
    )

    yield {
        "root": temp_dir,
        "obs_dir": obs_dir,
        "opp_file": opp_file,
        "change_dir": change_dir,
        "obs_repo": obs_repo,
        "opp_repo": opp_repo,
        "change_repo": change_repo,
        "engine": engine,
        "service": service,
    }

    shutil.rmtree(temp_dir, ignore_errors=True)


def _create_obs(
    obs_id: str,
    entity_id: str,
    timestamp: datetime,
    price_amt: str | None = None,
    stock: int | None = None,
    status: ObservationStatus = ObservationStatus.SUCCESS,
    raw_payload: dict | None = None,
) -> MarketObservation:
    price = NormalizedPrice(amount=Decimal(price_amt), currency="CLP") if price_amt is not None else None
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
        raw_payload=raw_payload or {},
    )


def test_j4_integration_full_pipeline_flow(temp_environment):
    """
    J.2 OBSERVATION T0 -> PERSIST
    -> J.2 OBSERVATION T1 -> J.4 CHANGE DETECTION
    -> CHANGE RECORD -> PERSIST -> RELOAD
    """
    service = temp_environment["service"]
    obs_repo = temp_environment["obs_repo"]
    change_repo = temp_environment["change_repo"]

    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    obs0 = _create_obs("obs-0", "MLC-001", t0, price_amt="100.00", stock=10)
    obs_repo.save(obs0)

    obs1 = _create_obs("obs-1", "MLC-001", t1, price_amt="90.00", stock=8)
    obs_repo.save(obs1)

    changes = service.detect_observation_changes(
        observations=[obs1],
        correlation_id="corr-integ-1",
    )

    assert len(changes) == 1
    ch = changes[0]
    assert ch.subject_id == "MLC-001"
    assert ch.previous_reference == "obs-0"
    assert ch.current_reference == "obs-1"
    assert "price" in ch.changed_fields
    assert "stock" in ch.changed_fields

    # Reload from fresh repo instance
    reloaded_repo = JsonChangeRecordRepository(storage_dir=temp_environment["change_dir"])
    persisted_change = reloaded_repo.get_by_id(ch.change_id)
    assert persisted_change is not None
    assert persisted_change.change_id == ch.change_id
    assert persisted_change.derived_deltas[0].numeric_delta == Decimal("-10.00")
    assert persisted_change.derived_deltas[1].numeric_delta == Decimal("-2")


# === E2E SCENARIOS ===

def test_e2e_scenario_a_price_change(temp_environment):
    """Escenario A — Price Change: T0 price 100 -> T1 price 90 -> detected change."""
    service = temp_environment["service"]
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    obs0 = _create_obs("obs-a0", "ITEM-A", t0, price_amt="100.00")
    obs1 = _create_obs("obs-a1", "ITEM-A", t1, price_amt="90.00")

    changes = service.detect_observation_changes([obs0, obs1], correlation_id="corr-scen-a")
    assert len(changes) == 2
    assert changes[1].change_type == ChangeType.PRICE_CHANGED
    assert changes[1].derived_deltas[0].numeric_delta == Decimal("-10.00")
    assert changes[1].derived_deltas[0].percentage_delta == Decimal("-10.00")


def test_e2e_scenario_b_no_change(temp_environment):
    """Escenario B — No Change: same canonical state -> no false change."""
    service = temp_environment["service"]
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    obs0 = _create_obs("obs-b0", "ITEM-B", t0, price_amt="100.00", stock=5)
    obs1 = _create_obs("obs-b1", "ITEM-B", t1, price_amt="100.00", stock=5)

    changes = service.detect_observation_changes([obs0, obs1], correlation_id="corr-scen-b")
    assert len(changes) == 2
    assert changes[1].change_type == ChangeType.NO_CHANGE
    assert len(changes[1].changed_fields) == 0


def test_e2e_scenario_c_unknown(temp_environment):
    """Escenario C — UNKNOWN: T0 known, T1 unknown -> no fabricated commercial delta."""
    service = temp_environment["service"]
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    obs0 = _create_obs("obs-c0", "ITEM-C", t0, price_amt="100.00", stock=10)
    obs1 = _create_obs("obs-c1", "ITEM-C", t1, price_amt=None, stock=None)

    changes = service.detect_observation_changes([obs0, obs1], correlation_id="corr-scen-c")
    assert len(changes) == 2
    assert changes[1].change_type in (ChangeType.UNKNOWN_TRANSITION, ChangeType.MULTIPLE_CHANGES)
    assert "price" in changes[1].unknown_fields
    assert "stock" in changes[1].unknown_fields
    # Deltas can be present to describe UNKNOWN transition, but numeric_delta and percentage_delta must be None and is_valid_delta False
    for delta in changes[1].derived_deltas:
        assert delta.numeric_delta is None
        assert delta.percentage_delta is None
        assert delta.is_valid_delta is False


def test_e2e_scenario_d_restart(temp_environment):
    """Escenario D — Restart: T0/T1/change persisted -> recreate services -> reload history."""
    change_dir = temp_environment["change_dir"]
    obs_dir = temp_environment["obs_dir"]
    opp_file = temp_environment["opp_file"]

    # Session 1: persist T0 and T1 and detect change
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    obs0 = _create_obs("obs-d0", "ITEM-D", t0, price_amt="100.00")
    obs1 = _create_obs("obs-d1", "ITEM-D", t1, price_amt="90.00")

    s1_obs_repo = JsonMarketObservationRepository(storage_dir=obs_dir)
    s1_obs_repo.save(obs0)
    s1_obs_repo.save(obs1)

    s1_change_repo = JsonChangeRecordRepository(storage_dir=change_dir)
    s1_service = ChangeDetectionService(
        engine=ChangeDetectionEngine(),
        change_repository=s1_change_repo,
        observation_repository=s1_obs_repo,
        opportunity_repository=JsonOpportunityRepository(file_path=opp_file),
    )
    changes_s1 = s1_service.detect_observation_changes([obs1])
    assert len(changes_s1) == 1

    # Destroy and recreate services (Session 2 / Restart)
    s2_obs_repo = JsonMarketObservationRepository(storage_dir=obs_dir)
    s2_change_repo = JsonChangeRecordRepository(storage_dir=change_dir)
    s2_service = ChangeDetectionService(
        engine=ChangeDetectionEngine(),
        change_repository=s2_change_repo,
        observation_repository=s2_obs_repo,
        opportunity_repository=JsonOpportunityRepository(file_path=opp_file),
    )

    t2 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    obs2 = _create_obs("obs-d2", "ITEM-D", t2, price_amt="80.00")
    s2_obs_repo.save(obs2)

    changes_s2 = s2_service.detect_observation_changes([obs2])
    assert len(changes_s2) == 1
    assert changes_s2[0].previous_reference == "obs-d1"
    assert changes_s2[0].current_reference == "obs-d2"
    assert changes_s2[0].derived_deltas[0].numeric_delta == Decimal("-10.00")


def test_e2e_scenario_e_duplicate(temp_environment):
    """Escenario E — Duplicate: same T1 replayed -> one logical change (idempotency)."""
    service = temp_environment["service"]
    change_repo = temp_environment["change_repo"]
    obs_repo = temp_environment["obs_repo"]

    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    obs0 = _create_obs("obs-e0", "ITEM-E", t0, price_amt="100.00")
    obs1 = _create_obs("obs-e1", "ITEM-E", t1, price_amt="90.00")
    obs_repo.save(obs0)
    obs_repo.save(obs1)

    # First run
    c1 = service.detect_observation_changes([obs1])
    # Replay same T1
    c2 = service.detect_observation_changes([obs1])

    assert len(c1) == 1
    assert len(c2) == 1
    assert c1[0].idempotency_key == c2[0].idempotency_key

    all_changes = change_repo.list_by_subject(ChangeSubjectType.MARKET_OBSERVATION, "ITEM-E")
    assert len(all_changes) == 1


def test_e2e_scenario_f_out_of_order(temp_environment):
    """Escenario F — Out of Order: T2 before T1 sequence is reordered deterministically."""
    service = temp_environment["service"]
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    obs0 = _create_obs("obs-f0", "ITEM-F", t0, price_amt="100.00")
    obs1 = _create_obs("obs-f1", "ITEM-F", t1, price_amt="90.00")
    obs2 = _create_obs("obs-f2", "ITEM-F", t2, price_amt="80.00")

    # Pass in shuffled order: obs2, obs0, obs1
    changes = service.detect_observation_changes([obs2, obs0, obs1])
    assert len(changes) == 3
    assert changes[1].previous_reference == "obs-f0"
    assert changes[1].current_reference == "obs-f1"
    assert changes[2].previous_reference == "obs-f1"
    assert changes[2].current_reference == "obs-f2"


def test_e2e_scenario_g_opportunity_change(temp_environment):
    """Escenario G — Opportunity Change: opportunity status/score transition -> traceable change."""
    service = temp_environment["service"]
    opp_repo = temp_environment["opp_repo"]

    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    opp0 = OpportunityRecord(
        opportunity_id="opp-g0",
        canonical_product_id="PROD-G",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=t0,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-g0",),
        observed_metrics=ObservedOpportunityMetrics(observations_count=1),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("80.0")),
    )
    opp_repo.save(opp0)

    opp1 = OpportunityRecord(
        opportunity_id="opp-g1",
        canonical_product_id="PROD-G",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=t1,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-g1",),
        observed_metrics=ObservedOpportunityMetrics(observations_count=2),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("95.0")),
    )
    opp_repo.save(opp1)

    changes = service.detect_opportunity_changes([opp1], correlation_id="corr-opp-g")
    assert len(changes) == 1
    assert changes[0].change_type == ChangeType.OPPORTUNITY_SCORE_CHANGED
    assert changes[0].derived_deltas[0].numeric_delta == Decimal("15.0")
    assert changes[0].subject_type == ChangeSubjectType.OPPORTUNITY


def test_e2e_scenario_h_source_failure(temp_environment):
    """Escenario H — Source Failure: timeout observation -> no false market change."""
    service = temp_environment["service"]
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    obs0 = _create_obs("obs-h0", "ITEM-H", t0, price_amt="100.00", status=ObservationStatus.SUCCESS)
    obs1 = _create_obs("obs-h1", "ITEM-H", t1, price_amt=None, status=ObservationStatus.TIMEOUT)

    changes = service.detect_observation_changes([obs0, obs1])
    assert len(changes) == 2
    assert changes[1].change_type == ChangeType.SOURCE_STATUS_CHANGED
    assert "status" in changes[1].changed_fields
    assert len(changes[1].derived_deltas) == 0


def test_e2e_scenario_i_security(temp_environment):
    """Escenario I — Security: secret in metadata -> sanitized persistence."""
    service = temp_environment["service"]
    change_repo = temp_environment["change_repo"]

    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)

    obs0 = _create_obs("obs-i0", "ITEM-I", t0, price_amt="100.00")
    obs1 = _create_obs("obs-i1", "ITEM-I", t1, price_amt="80.00")

    # Engine generates change record with sensitive token in metadata
    change = temp_environment["engine"].compare_observations(
        obs0,
        obs1,
        correlation_id="corr-sec-1",
        metadata={
            "api_key": "SECRET_KEY_12345",
            "access_token": "OAUTH_TOKEN_999",
            "public_metric": "ok",
        },
    )

    change_repo.save(change)

    # Read raw JSON file on disk to verify secrets are redacted
    record_file = temp_environment["change_dir"] / f"{change.change_id}.json"
    assert record_file.exists()

    with open(record_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["metadata"]["api_key"] == "[REDACTED]"
    assert data["metadata"]["access_token"] == "[REDACTED]"
    assert data["metadata"]["public_metric"] == "ok"
