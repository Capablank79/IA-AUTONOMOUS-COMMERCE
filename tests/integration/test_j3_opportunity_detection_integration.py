import os
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict, Any

from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    ObservationSourceType,
    NormalizedPrice,
    ObservedSellerInfo,
    ObservedCompetitionInfo,
)
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType
from src.infrastructure.persistence.data.json.market_observation_repository import JsonMarketObservationRepository
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    OpportunityDetectionCriteria,
)
from src.domain.opportunity_detection.engine import OpportunityDetectionEngine
from src.infrastructure.persistence.data.json.opportunity_repository import JsonOpportunityRepository
from src.application.opportunity_detection.service import OpportunityDetectionService


def _generate_test_observation(
    obs_id: str,
    entity_id: str,
    title: str = "Smart Wireless Speaker",
    price: Decimal = Decimal("45000.00"),
    comp_price: Optional[Decimal] = Decimal("60000.00"),
    sold: int = 80,
    comp_count: int = 2,
    status: ObservationStatus = ObservationStatus.SUCCESS,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> MarketObservation:
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    return MarketObservation(
        observation_id=obs_id,
        source="MERCADOLIBRE_LIVE",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id=entity_id,
        title=title,
        category="AUDIO",
        product_sku=f"SKU-{entity_id}",
        price=NormalizedPrice(amount=price, currency="CLP"),
        sold_quantity=sold,
        stock=30,
        competition_info=ObservedCompetitionInfo(
            total_competitors=comp_count,
            lowest_competitor_price=NormalizedPrice(amount=comp_price, currency="CLP") if comp_price else None,
        ),
        status=status,
        provenance="LIVE",
        confidence=Confidence.HIGH,
        signal_type=SignalType.OBSERVED,
        correlation_id=f"corr-{obs_id}",
        raw_payload=raw_payload or {},
    )


def test_j3_integration_market_observation_to_opportunity_persistence(tmp_path):
    """
    Demuestra el flujo integral de J.3:
    J.2 MARKET OBSERVATION
    ↓
    J.3 OPPORTUNITY DETECTION
    ↓
    OPPORTUNITY
    ↓
    PERSIST
    ↓
    RELOAD
    """
    obs_dir = tmp_path / "observations"
    opp_dir = tmp_path / "opportunities"

    obs_repo = JsonMarketObservationRepository(obs_dir)
    opp_repo = JsonOpportunityRepository(opp_dir / "opportunities.json")

    detection_service = OpportunityDetectionService(
        opportunity_repository=opp_repo,
        observation_repository=obs_repo,
        detection_engine=OpportunityDetectionEngine(),
    )

    # 1. Simular persistencia de observaciones desde J.2
    obs1 = _generate_test_observation("obs-100", "PROD-AUDIO-1", price=Decimal("45000.00"), comp_price=Decimal("60000.00"), sold=80, comp_count=2)
    obs2 = _generate_test_observation("obs-101", "PROD-AUDIO-2", price=Decimal("15000.00"), comp_price=Decimal("14000.00"), sold=2, comp_count=20)
    obs_repo.save_all([obs1, obs2])

    assert len(obs_repo.list_all()) == 2

    # 2. Ejecutar detección desde repositorio J.2
    opportunities = detection_service.detect_from_repository()
    assert len(opportunities) == 1  # Sólo PROD-AUDIO-1 califica con score >= 30.0 (PROD-AUDIO-2 queda descartado)

    opp = opportunities[0]
    assert opp.canonical_product_id == "PROD-AUDIO-1"
    assert opp.status == OpportunityStatus.VALID
    assert opp.derived_metrics.price_gap_ratio == Decimal("0.2500")
    assert opp.derived_metrics.opportunity_score is not None
    assert opp.derived_metrics.opportunity_score >= Decimal("30.0")

    # 3. Verificar persistencia durable y recarga tras reiniciar servicio
    del detection_service
    del opp_repo

    reloaded_repo = JsonOpportunityRepository(opp_dir / "opportunities.json")
    reloaded_opp = reloaded_repo.get_by_id(opp.opportunity_id)
    assert reloaded_opp is not None
    assert reloaded_opp.canonical_product_id == "PROD-AUDIO-1"
    assert reloaded_opp.observed_metrics.observed_price.amount == Decimal("45000.00")
    assert reloaded_opp.source_observation_ids == ("obs-100",)


# ============================================================
# ESCENARIOS E2E J.3 (A - I)
# ============================================================

def test_scenario_a_valid_opportunity(tmp_path):
    """Escenario A — Valid Opportunity: valid observations -> opportunity detected."""
    opp_repo = JsonOpportunityRepository(tmp_path / "opps.json")
    service = OpportunityDetectionService(opportunity_repository=opp_repo)

    obs = _generate_test_observation("obs-a1", "PROD-A", price=Decimal("20000.00"), comp_price=Decimal("30000.00"), sold=100, comp_count=1)
    results = service.process_observations([obs])

    assert len(results) == 1
    assert results[0].status == OpportunityStatus.VALID
    assert results[0].derived_metrics.opportunity_score > Decimal("50.0")


def test_scenario_b_no_opportunity(tmp_path):
    """Escenario B — No Opportunity: observations fail criteria -> no opportunity."""
    opp_repo = JsonOpportunityRepository(tmp_path / "opps.json")
    service = OpportunityDetectionService(opportunity_repository=opp_repo)

    # Precio desfavorable, ventas nulas, alta competencia
    obs = _generate_test_observation("obs-b1", "PROD-B", price=Decimal("50000.00"), comp_price=Decimal("40000.00"), sold=1, comp_count=25)
    results = service.process_observations([obs], criteria=OpportunityDetectionCriteria(min_score=Decimal("40.0")))

    assert len(results) == 0


def test_scenario_c_unknown(tmp_path):
    """Escenario C — UNKNOWN: critical observation field UNKNOWN -> no false opportunity."""
    opp_repo = JsonOpportunityRepository(tmp_path / "opps.json")
    service = OpportunityDetectionService(opportunity_repository=opp_repo)

    now = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-c1",
        source="TEST",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="PROD-C",
        price=None,  # UNKNOWN
        sold_quantity=None,  # UNKNOWN
        stock=None,
        status=ObservationStatus.SUCCESS,
        provenance="LIVE",
        confidence=Confidence.UNKNOWN,
        signal_type=SignalType.OBSERVED,
        correlation_id="corr-c",
    )
    results = service.process_observations([obs])
    assert len(results) == 1
    assert results[0].status == OpportunityStatus.INSUFFICIENT_DATA
    assert results[0].observed_metrics.observed_price is None


def test_scenario_d_insufficient_evidence(tmp_path):
    """Escenario D — Insufficient Evidence: insufficient evidence -> INSUFFICIENT_DATA/UNKNOWN."""
    opp_repo = JsonOpportunityRepository(tmp_path / "opps.json")
    service = OpportunityDetectionService(opportunity_repository=opp_repo)

    obs = _generate_test_observation("obs-d1", "PROD-D")
    criteria = OpportunityDetectionCriteria(min_observations_required=5)
    results = service.process_observations([obs], criteria=criteria)

    assert len(results) == 1
    assert results[0].status == OpportunityStatus.INSUFFICIENT_DATA
    assert results[0].confidence == Confidence.UNKNOWN


def test_scenario_e_duplicate_replay(tmp_path):
    """Escenario E — Duplicate Replay: same observation processed twice -> one opportunity."""
    opp_repo = JsonOpportunityRepository(tmp_path / "opps.json")
    service = OpportunityDetectionService(opportunity_repository=opp_repo)

    obs = _generate_test_observation("obs-e1", "PROD-E")
    service.process_observations([obs])
    service.process_observations([obs])

    all_opps = opp_repo.list_all()
    assert len(all_opps) == 1


def test_scenario_f_restart(tmp_path):
    """Escenario F — Restart: persist -> destroy service -> reload -> opportunity retained."""
    file_path = tmp_path / "opps_restart.json"
    repo1 = JsonOpportunityRepository(file_path)
    service1 = OpportunityDetectionService(opportunity_repository=repo1)

    obs = _generate_test_observation("obs-f1", "PROD-F")
    service1.process_observations([obs])

    del service1
    del repo1

    repo2 = JsonOpportunityRepository(file_path)
    reloaded = repo2.list_by_product("PROD-F")
    assert len(reloaded) == 1
    assert reloaded[0].canonical_product_id == "PROD-F"


def test_scenario_g_sensitive_data(tmp_path):
    """Escenario G — Sensitive Data: observation metadata contains secret -> persisted opportunity sanitized."""
    file_path = tmp_path / "opps_sec.json"
    repo = JsonOpportunityRepository(file_path)
    service = OpportunityDetectionService(opportunity_repository=repo)

    obs = _generate_test_observation(
        "obs-g1",
        "PROD-G",
        raw_payload={
            "api_key": "secret-key-12345",
            "oauth_token": "token-9999",
            "safe_tag": "tag-value",
        }
    )
    service.process_observations([obs])

    raw_json = file_path.read_text(encoding="utf-8")
    assert "secret-key-12345" not in raw_json
    assert "token-9999" not in raw_json


def test_scenario_h_traceability(tmp_path):
    """Escenario H — Traceability: opportunity -> observation -> source -> timestamp -> provenance."""
    opp_repo = JsonOpportunityRepository(tmp_path / "opps.json")
    service = OpportunityDetectionService(opportunity_repository=opp_repo)

    obs = _generate_test_observation("obs-h1", "PROD-H")
    results = service.process_observations([obs], correlation_id="trace-corr-123")

    opp = results[0]
    assert opp.source_observation_ids == ("obs-h1",)
    assert opp.correlation_id == "trace-corr-123"
    assert opp.provenance == "LIVE"
    assert opp.metadata.get("source") == "MERCADOLIBRE_LIVE"


def test_scenario_i_architecture_boundary(tmp_path):
    """
    Escenario I — Architecture Boundary:
    J.3 does NOT create decisions, execute actions, mutate policy or call marketplace directly.
    """
    opp_repo = JsonOpportunityRepository(tmp_path / "opps.json")
    service = OpportunityDetectionService(opportunity_repository=opp_repo)

    obs = _generate_test_observation("obs-i1", "PROD-I")
    results = service.process_observations([obs])

    opp = results[0]
    assert isinstance(opp, OpportunityRecord)
    # Verificar que no existen vínculos directos a DecisionRecord ni ActionRecord
    assert not hasattr(opp, "decision_record")
    assert not hasattr(opp, "action_record")
    assert not hasattr(opp, "policy_mutations")
