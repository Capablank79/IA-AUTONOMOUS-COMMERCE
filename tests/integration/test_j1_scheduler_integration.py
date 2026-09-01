import pytest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from unittest.mock import MagicMock
from decimal import Decimal

from src.domain.market_intelligence.models import (
    MarketListing,
    Marketplace,
    Money,
    MarketSnapshot,
    SearchCriteria,
)
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionStatus,
    MissionPriority,
    MissionResult,
    MissionTraceEntry,
)
from src.domain.scheduling.models import (
    DeterministicClock,
    Schedule,
    ScheduleConfig,
    ScheduleOccurrence,
    ScheduleStatus,
    ScheduleType,
    ExecutionStatus,
    MissedExecutionPolicy,
)
from src.domain.scheduling.ports import MissionTriggerPort
from src.infrastructure.persistence.data.json.mission_repository import JsonMissionRepository
from src.infrastructure.persistence.data.json.schedule_repository import JsonScheduleRepository
from src.application.mission.orchestrator import BasicMissionOrchestrator
from src.application.scheduling.service import SchedulerService
from src.application.scheduling.trigger_adapter import MissionOrchestratorTriggerAdapter


def create_mock_market_data_source():
    mock_data_source = MagicMock()
    listings = [
        MarketListing(
            external_id="ML-1001",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Smartwatch Pro",
            price=Money(amount=Decimal("50000"), currency="CLP"),
            sold_quantity=50,
            available_quantity=10,
            seller_id="SELLER-1",
            condition="new",
            shipping_info={"free_shipping": True},
            category="ELECTRONICS",
        )
    ]
    criteria = SearchCriteria(query="test", marketplace=Marketplace.MERCADO_LIBRE)
    snapshot = MarketSnapshot(
        snapshot_id="snap-j1-test",
        timestamp=datetime.now(timezone.utc),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=listings,
        total_results=1,
    )
    mock_data_source.fetch_snapshot.return_value = snapshot
    return mock_data_source


@pytest.fixture
def integrated_setup(tmp_path):
    storage_missions = tmp_path / "missions_data"
    storage_schedules = tmp_path / "schedules_data"

    mission_repo = JsonMissionRepository(storage_missions)
    schedule_repo = JsonScheduleRepository(storage_schedules)

    mock_market_ds = create_mock_market_data_source()
    orchestrator = BasicMissionOrchestrator(
        repository=mission_repo,
        market_data_source=mock_market_ds,
    )

    trigger_adapter = MissionOrchestratorTriggerAdapter(
        orchestrator=orchestrator,
        mission_repository=mission_repo,
    )

    clock = DeterministicClock(datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc))

    scheduler = SchedulerService(
        repository=schedule_repo,
        trigger=trigger_adapter,
        clock=clock,
    )

    return {
        "mission_repo": mission_repo,
        "schedule_repo": schedule_repo,
        "mock_market_ds": mock_market_ds,
        "orchestrator": orchestrator,
        "trigger_adapter": trigger_adapter,
        "clock": clock,
        "scheduler": scheduler,
        "tmp_path": tmp_path,
    }


def test_j1_scheduler_integration_flow(integrated_setup):
    """
    Integration Test J.1:
    CREATE SCHEDULE
    → PERSIST
    → SCHEDULER OBSERVES DUE SCHEDULE
    → TRIGGER EXISTING MISSION
    → MISSION EXECUTION
    → RESULT
    → PERSIST
    → RELOAD
    → NEXT OCCURRENCE
    """
    scheduler = integrated_setup["scheduler"]
    clock = integrated_setup["clock"]
    schedule_repo = integrated_setup["schedule_repo"]
    mission_repo = integrated_setup["mission_repo"]

    # 1. CREATE SCHEDULE
    sched = scheduler.create_schedule(
        schedule_id="sched-integ-1",
        mission_type=MissionType.MARKET_DISCOVERY,
        mission_parameters={"query": "reloj inteligente"},
        interval_seconds=1800,  # 30 minutos
    )
    assert sched.schedule_id == "sched-integ-1"

    # 2. PERSIST VERIFICATION
    persisted_sched = schedule_repo.get_by_id("sched-integ-1")
    assert persisted_sched is not None
    assert persisted_sched.total_runs == 0

    # 3. SCHEDULER OBSERVES DUE SCHEDULE & TRIGGERS MISSION
    occurrences = scheduler.tick()
    assert len(occurrences) == 1
    occ = occurrences[0]
    assert occ.status == ExecutionStatus.SUCCESS
    assert occ.mission_id is not None

    # 4. MISSION EXECUTION & RESULT VERIFICATION
    mission = mission_repo.get_by_id(occ.mission_id)
    assert mission is not None
    assert mission.status == MissionStatus.COMPLETED

    res = mission_repo.get_result(occ.mission_id)
    assert res is not None
    assert res.status == MissionStatus.COMPLETED

    # 5. RELOAD VERIFICATION AFTER RESTART
    storage_schedules = integrated_setup["tmp_path"] / "schedules_data"
    storage_missions = integrated_setup["tmp_path"] / "missions_data"

    new_schedule_repo = JsonScheduleRepository(storage_schedules)
    new_mission_repo = JsonMissionRepository(storage_missions)
    new_mock_ds = create_mock_market_data_source()
    new_orchestrator = BasicMissionOrchestrator(
        repository=new_mission_repo,
        market_data_source=new_mock_ds,
    )
    new_trigger = MissionOrchestratorTriggerAdapter(
        orchestrator=new_orchestrator,
        mission_repository=new_mission_repo,
    )
    reloaded_scheduler = SchedulerService(
        repository=new_schedule_repo,
        trigger=new_trigger,
        clock=clock,
    )

    reloaded_sched = reloaded_scheduler.get_schedule("sched-integ-1")
    assert reloaded_sched is not None
    assert reloaded_sched.total_runs == 1
    assert reloaded_sched.next_run_at == clock.now() + timedelta(seconds=1800)

    # 6. ADVANCE CLOCK & TRIGGER NEXT OCCURRENCE
    clock.advance(1800)
    next_occs = reloaded_scheduler.tick()
    assert len(next_occs) == 1
    next_occ = next_occs[0]
    assert next_occ.status == ExecutionStatus.SUCCESS
    assert next_occ.mission_id != occ.mission_id

    # Verify updated total runs
    final_sched = reloaded_scheduler.get_schedule("sched-integ-1")
    assert final_sched.total_runs == 2


# ============================================================
# E2E SCENARIOS (A - F)
# ============================================================

def test_e2e_scenario_a_happy_path(integrated_setup):
    """
    Escenario A — Happy Path
    Schedule: "ejecutar misión X cada intervalo"
    → schedule persisted
    → scheduler detects due execution
    → existing mission triggered
    → mission executes
    → result persisted.
    """
    scheduler = integrated_setup["scheduler"]
    mission_repo = integrated_setup["mission_repo"]

    sched = scheduler.create_schedule(
        schedule_id="sched-e2e-a",
        mission_type=MissionType.MARKET_DISCOVERY,
        mission_parameters={"query": "gamer mouse"},
        interval_seconds=600,
    )
    assert sched.status == ScheduleStatus.ACTIVE

    occs = scheduler.tick()
    assert len(occs) == 1
    assert occs[0].status == ExecutionStatus.SUCCESS
    mission_res = mission_repo.get_result(occs[0].mission_id)
    assert mission_res is not None
    assert mission_res.status == MissionStatus.COMPLETED


def test_e2e_scenario_b_duplicate_replay(integrated_setup):
    """
    Escenario B — Duplicate Replay
    same occurrence processed twice
    → one mission trigger
    → second invocation ignored/idempotent.
    """
    scheduler = integrated_setup["scheduler"]
    sched = scheduler.create_schedule(
        schedule_id="sched-e2e-b",
        mission_type=MissionType.MARKET_DISCOVERY,
        mission_parameters={"query": "keyboard"},
        interval_seconds=600,
    )

    # First execution
    occs1 = scheduler.tick()
    assert len(occs1) == 1

    # Duplicate tick at same time without clock advance
    occs2 = scheduler.tick()
    assert len(occs2) == 0  # Not due again yet


def test_e2e_scenario_c_restart(integrated_setup):
    """
    Escenario C — Restart
    schedule persisted
    → process destroyed
    → scheduler recreated
    → schedule reloaded
    → next occurrence continues.
    """
    scheduler = integrated_setup["scheduler"]
    clock = integrated_setup["clock"]
    tmp_path = integrated_setup["tmp_path"]

    scheduler.create_schedule(
        schedule_id="sched-e2e-c",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=300,
    )
    scheduler.tick()

    # Recreate process
    fresh_repo = JsonScheduleRepository(tmp_path / "schedules_data")
    fresh_mission_repo = JsonMissionRepository(tmp_path / "missions_data")
    fresh_ds = create_mock_market_data_source()
    fresh_orch = BasicMissionOrchestrator(repository=fresh_mission_repo, market_data_source=fresh_ds)
    fresh_trigger = MissionOrchestratorTriggerAdapter(fresh_orch, fresh_mission_repo)
    fresh_scheduler = SchedulerService(fresh_repo, fresh_trigger, clock=clock)

    reloaded = fresh_scheduler.get_schedule("sched-e2e-c")
    assert reloaded is not None
    assert reloaded.total_runs == 1

    clock.advance(300)
    occs = fresh_scheduler.tick()
    assert len(occs) == 1
    assert occs[0].status == ExecutionStatus.SUCCESS


def test_e2e_scenario_d_disabled(integrated_setup):
    """
    Escenario D — Disabled
    disabled schedule
    → no mission execution.
    """
    scheduler = integrated_setup["scheduler"]
    scheduler.create_schedule(
        schedule_id="sched-e2e-d",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=300,
    )
    scheduler.disable_schedule("sched-e2e-d")

    occs = scheduler.tick()
    assert len(occs) == 0


def test_e2e_scenario_e_unknown(integrated_setup):
    """
    Escenario E — UNKNOWN
    mission trigger/result UNKNOWN
    → UNKNOWN preserved
    → no false SUCCESS.
    """
    # Orchestrator sin market_data_source produce un bloqueo (BLOCKED / UNKNOWN)
    mission_repo = integrated_setup["mission_repo"]
    orch_without_ds = BasicMissionOrchestrator(repository=mission_repo)
    trigger = MissionOrchestratorTriggerAdapter(orch_without_ds, mission_repo)
    scheduler = SchedulerService(integrated_setup["schedule_repo"], trigger, clock=integrated_setup["clock"])

    scheduler.create_schedule(
        schedule_id="sched-e2e-e",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=300,
    )

    occs = scheduler.tick()
    assert len(occs) == 1
    # Cuando está bloqueado por falta de data source, el trigger adapter lo mapea a UNKNOWN fielmente
    assert occs[0].status == ExecutionStatus.UNKNOWN
    assert "blocked" in occs[0].error.lower()


def test_e2e_scenario_f_failure(integrated_setup):
    """
    Escenario F — Failure
    trigger raises transient failure
    → failure recorded
    → scheduler remains alive
    → no corruption of future schedule.
    """
    class FailingTrigger(MissionTriggerPort):
        def trigger(self, schedule, occurrence):
            raise RuntimeError("Transient connection reset by peer")

    scheduler = SchedulerService(
        integrated_setup["schedule_repo"],
        FailingTrigger(),
        clock=integrated_setup["clock"],
    )

    scheduler.create_schedule(
        schedule_id="sched-e2e-f",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=300,
    )

    # El tick no debe hacer crashear el servicio
    occs = scheduler.tick()
    assert len(occs) == 1
    assert occs[0].status == ExecutionStatus.FAILED
    assert "Transient connection reset" in occs[0].error

    # Scheduler sigue vivo y el schedule tiene su próximo run calculado
    sched = scheduler.get_schedule("sched-e2e-f")
    assert sched.next_run_at > integrated_setup["clock"].now()
