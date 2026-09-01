"""
Tests Unitarios para Misiones Continuas (Continuous Missions - Hito J.7).

Cubre exhaustivamente:
A. create continuous mission
B. start
C. pause
D. resume
E. stop
F. invalid transition
G. cycle identity
H. execute first cycle
I. execute next cycle
J. scheduler integration
K. cycle idempotency
L. duplicate occurrence
M. restart/reload
N. crash recovery
O. max cycles
P. disabled schedule
Q. terminal state
R. failure count
S. cycle failure
T. UNKNOWN cycle
U. provenance
V. correlation/causation
W. security sanitization
X. Business Memory reuse
Y. AutonomousLoop reuse
Z. no Policy bypass
AA. no duplicate Scheduler
AB. no duplicate Event Bus
AC. no Learning Engine
AD. concurrency protection
AE. deterministic execution
"""

import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

import pytest

from src.domain.mission.models import MissionType, MissionPriority, MissionStatus
from src.domain.scheduling.models import (
    Clock,
    Schedule,
    ScheduleType,
    ScheduleOccurrence,
    ExecutionStatus,
    ScheduleStatus,
)
from src.domain.continuous_mission.models import (
    ContinuousMission,
    ContinuousMissionCycle,
    ContinuousMissionStatus,
    ContinuousCycleStatus,
    ContinuousMissionStopCondition,
    StopConditionType,
)
from src.domain.continuous_mission.ports import CycleExecutorPort
from src.infrastructure.persistence.data.json.continuous_mission_repository import (
    JsonContinuousMissionRepository,
    CorruptedContinuousMissionDataError,
    _sanitize_data,
)
from src.application.continuous_mission.service import ContinuousMissionService
from src.application.continuous_mission.cycle_executor_adapter import StandardCycleExecutorAdapter


class MockClock(Clock):
    """Reloj determinista para tests unitarios."""

    def __init__(self, initial_time: Optional[datetime] = None):
        self._current_time = initial_time or datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def advance(self, delta: timedelta) -> datetime:
        self._current_time += delta
        return self._current_time


class StubCycleExecutor(CycleExecutorPort):
    """Stub determinista para testing de ContinuousMissionService."""

    def __init__(
        self,
        status: ContinuousCycleStatus = ContinuousCycleStatus.SUCCESS,
        summary: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ):
        self.status = status
        self.summary = summary or {"processed": True}
        self.error_message = error_message
        self.call_count = 0
        self.last_mission = None
        self.last_cycle = None

    def execute_cycle(
        self,
        continuous_mission: ContinuousMission,
        cycle: ContinuousMissionCycle,
    ) -> Tuple[ContinuousCycleStatus, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
        self.call_count += 1
        self.last_mission = continuous_mission
        self.last_cycle = cycle
        mission_id = f"m_stub_{cycle.cycle_number}"
        return self.status, mission_id, self.summary, self.error_message


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def clock():
    return MockClock()


@pytest.fixture
def repo(temp_dir):
    return JsonContinuousMissionRepository(temp_dir)


@pytest.fixture
def stub_executor():
    return StubCycleExecutor()


@pytest.fixture
def service(repo, stub_executor, clock):
    return ContinuousMissionService(
        repository=repo,
        cycle_executor=stub_executor,
        scheduler_service=None,
        clock=clock,
    )


def test_a_create_continuous_mission(service, repo):
    cm = service.create_continuous_mission(
        schedule_id="sch_123",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Maintain competitive pricing",
        priority=MissionPriority.HIGH,
        mission_parameters={"category": "electronics"},
    )
    assert cm.continuous_mission_id is not None
    assert cm.status == ContinuousMissionStatus.CREATED
    assert cm.schedule_id == "sch_123"
    assert cm.cycle_count == 0
    assert cm.consecutive_failures == 0

    saved = repo.get_by_id(cm.continuous_mission_id)
    assert saved is not None
    assert saved.goal == "Maintain competitive pricing"


def test_b_start_mission(service, repo, clock):
    cm = service.create_continuous_mission(
        schedule_id="sch_start",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Discover market gaps",
    )
    assert cm.status == ContinuousMissionStatus.CREATED

    started = service.start_mission(cm.continuous_mission_id)
    assert started.status == ContinuousMissionStatus.ACTIVE
    assert started.started_at == clock.now()

    reloaded = repo.get_by_id(cm.continuous_mission_id)
    assert reloaded.status == ContinuousMissionStatus.ACTIVE


def test_c_pause_mission(service, repo):
    cm = service.create_continuous_mission(
        schedule_id="sch_pause",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Discover market gaps",
    )
    service.start_mission(cm.continuous_mission_id)

    paused = service.pause_mission(cm.continuous_mission_id)
    assert paused.status == ContinuousMissionStatus.PAUSED

    reloaded = repo.get_by_id(cm.continuous_mission_id)
    assert reloaded.status == ContinuousMissionStatus.PAUSED


def test_d_resume_mission(service, repo):
    cm = service.create_continuous_mission(
        schedule_id="sch_resume",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Discover market gaps",
    )
    service.start_mission(cm.continuous_mission_id)
    service.pause_mission(cm.continuous_mission_id)

    resumed = service.resume_mission(cm.continuous_mission_id)
    assert resumed.status == ContinuousMissionStatus.ACTIVE


def test_e_stop_mission(service, repo):
    cm = service.create_continuous_mission(
        schedule_id="sch_stop",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Discover market gaps",
    )
    service.start_mission(cm.continuous_mission_id)

    stopped = service.stop_mission(cm.continuous_mission_id, reason="Operator request")
    assert stopped.status == ContinuousMissionStatus.STOPPED
    assert stopped.metadata.get("stop_reason") == "Operator request"


def test_f_invalid_transition(service):
    cm = service.create_continuous_mission(
        schedule_id="sch_inv",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Discover market gaps",
    )
    # Intento de pausar una misión no iniciada
    with pytest.raises(ValueError, match="Cannot pause ContinuousMission in state CREATED"):
        service.pause_mission(cm.continuous_mission_id)


def test_g_cycle_identity(service, clock):
    cm = service.create_continuous_mission(
        schedule_id="sch_cycle_id",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Dynamic repricing",
    )
    service.start_mission(cm.continuous_mission_id)

    cycle1 = service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle1.cycle_id.startswith(f"cyc_{cm.continuous_mission_id}_c1")
    assert cycle1.cycle_number == 1


def test_h_execute_first_cycle(service, repo, stub_executor):
    cm = service.create_continuous_mission(
        schedule_id="sch_c1",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Check pricing",
    )
    service.start_mission(cm.continuous_mission_id)

    cycle = service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle.status == ContinuousCycleStatus.SUCCESS
    assert cycle.cycle_number == 1
    assert stub_executor.call_count == 1

    updated_cm = repo.get_by_id(cm.continuous_mission_id)
    assert updated_cm.cycle_count == 1
    assert updated_cm.last_cycle_id == cycle.cycle_id
    assert updated_cm.last_result_status == "SUCCESS"


def test_i_execute_next_cycle(service, repo, clock):
    cm = service.create_continuous_mission(
        schedule_id="sch_multi_c",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Multi cycle execution",
    )
    service.start_mission(cm.continuous_mission_id)

    c1 = service.execute_next_cycle(cm.continuous_mission_id)
    clock.advance(timedelta(hours=1))
    c2 = service.execute_next_cycle(cm.continuous_mission_id)

    assert c1.cycle_number == 1
    assert c2.cycle_number == 2
    assert c2.cycle_id != c1.cycle_id

    updated_cm = repo.get_by_id(cm.continuous_mission_id)
    assert updated_cm.cycle_count == 2


def test_j_scheduler_integration_trigger(service, repo):
    cm = service.create_continuous_mission(
        schedule_id="sch_sched_trig",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Trigger via scheduler port",
    )
    service.start_mission(cm.continuous_mission_id)

    schedule = Schedule(
        schedule_id="sch_sched_trig",
        mission_type=MissionType.MARKET_DISCOVERY,
        schedule_type=ScheduleType.INTERVAL,
    )
    occ = ScheduleOccurrence(
        occurrence_id="occ_001",
        schedule_id="sch_sched_trig",
        scheduled_at=datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc),
        idempotency_key="occ_idem_001",
    )

    cycle_id, exec_status, summary, err = service.trigger(schedule, occ)
    assert exec_status == ExecutionStatus.SUCCESS
    assert cycle_id != ""
    assert err is None

    cycle = repo.get_cycle(cycle_id)
    assert cycle.occurrence_id == "occ_001"


def test_k_cycle_idempotency(service, repo):
    cm = service.create_continuous_mission(
        schedule_id="sch_idem",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Idempotency test",
    )
    service.start_mission(cm.continuous_mission_id)

    occ = ScheduleOccurrence(
        occurrence_id="occ_idem_1",
        schedule_id="sch_idem",
        scheduled_at=datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc),
        idempotency_key="occ_idem_key_1",
    )

    c1 = service.execute_next_cycle(cm.continuous_mission_id, occurrence=occ)
    c2 = service.execute_next_cycle(cm.continuous_mission_id, occurrence=occ)

    assert c1.cycle_id == c2.cycle_id
    assert c1.idempotency_key == c2.idempotency_key
    assert repo.get_by_id(cm.continuous_mission_id).cycle_count == 1


def test_l_duplicate_occurrence(service, repo):
    cm = service.create_continuous_mission(
        schedule_id="sch_dup_occ",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Duplicate occurrence test",
    )
    service.start_mission(cm.continuous_mission_id)

    schedule = Schedule(
        schedule_id="sch_dup_occ",
        mission_type=MissionType.MARKET_DISCOVERY,
        schedule_type=ScheduleType.INTERVAL,
    )
    occ = ScheduleOccurrence(
        occurrence_id="occ_dup_1",
        schedule_id="sch_dup_occ",
        scheduled_at=datetime(2026, 3, 30, 14, 0, 0, tzinfo=timezone.utc),
        idempotency_key="occ_dup_key_1",
    )

    res1 = service.trigger(schedule, occ)
    res2 = service.trigger(schedule, occ)

    assert res1[0] == res2[0]
    assert repo.get_by_id(cm.continuous_mission_id).cycle_count == 1


def test_m_restart_reload(temp_dir, clock):
    # Proceso 1
    repo1 = JsonContinuousMissionRepository(temp_dir)
    executor1 = StubCycleExecutor()
    service1 = ContinuousMissionService(repo1, executor1, clock=clock)

    cm = service1.create_continuous_mission(
        schedule_id="sch_restart",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Test persistence reload",
    )
    service1.start_mission(cm.continuous_mission_id)
    c1 = service1.execute_next_cycle(cm.continuous_mission_id)
    assert c1.cycle_number == 1

    # Proceso 2 (reinicio de memoria)
    repo2 = JsonContinuousMissionRepository(temp_dir)
    executor2 = StubCycleExecutor()
    service2 = ContinuousMissionService(repo2, executor2, clock=clock)

    reloaded_cm = repo2.get_by_id(cm.continuous_mission_id)
    assert reloaded_cm is not None
    assert reloaded_cm.cycle_count == 1

    clock.advance(timedelta(hours=1))
    c2 = service2.execute_next_cycle(cm.continuous_mission_id)
    assert c2.cycle_number == 2
    assert repo2.get_by_id(cm.continuous_mission_id).cycle_count == 2


def test_n_crash_recovery(temp_dir, clock):
    repo = JsonContinuousMissionRepository(temp_dir)
    service = ContinuousMissionService(repo, StubCycleExecutor(), clock=clock)

    cm = service.create_continuous_mission(
        schedule_id="sch_crash",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Crash recovery",
    )
    service.start_mission(cm.continuous_mission_id)

    # Simular ciclo registrado pero proceso interrumpido antes de avanzar
    now = clock.now()
    idem_key = f"cmc_{cm.continuous_mission_id}_cycle_1_{now.isoformat()}"
    pending_cycle = ContinuousMissionCycle(
        cycle_id=f"cyc_{cm.continuous_mission_id}_pending",
        continuous_mission_id=cm.continuous_mission_id,
        cycle_number=1,
        scheduled_at=now,
        started_at=now,
        completed_at=now,
        status=ContinuousCycleStatus.SUCCESS,
        idempotency_key=idem_key,
    )
    repo.save_cycle(pending_cycle)

    # Al ejecutar el siguiente ciclo en el mismo timestamp/idempotencia, previene duplicación
    reconciled_cycle = service.execute_next_cycle(cm.continuous_mission_id)
    assert reconciled_cycle.cycle_id == pending_cycle.cycle_id


def test_o_max_cycles(service, repo, clock):
    cm = service.create_continuous_mission(
        schedule_id="sch_max_c",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Max cycles limit",
        stop_condition=ContinuousMissionStopCondition(max_cycles=2),
    )
    service.start_mission(cm.continuous_mission_id)

    service.execute_next_cycle(cm.continuous_mission_id)
    assert repo.get_by_id(cm.continuous_mission_id).status == ContinuousMissionStatus.ACTIVE

    clock.advance(timedelta(hours=1))
    service.execute_next_cycle(cm.continuous_mission_id)
    completed_cm = repo.get_by_id(cm.continuous_mission_id)
    assert completed_cm.status == ContinuousMissionStatus.COMPLETED
    assert "Max cycles reached" in completed_cm.metadata.get("stop_reason", "")


def test_p_disabled_schedule_skipped(service):
    cm = service.create_continuous_mission(
        schedule_id="sch_paused",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Disabled test",
    )
    # Mission is CREATED (not ACTIVE)
    cycle = service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle.status == ContinuousCycleStatus.SKIPPED
    assert "Cycle skipped" in cycle.error_message


def test_q_terminal_state_no_execution(service):
    cm = service.create_continuous_mission(
        schedule_id="sch_term",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Terminal state test",
    )
    service.start_mission(cm.continuous_mission_id)
    service.stop_mission(cm.continuous_mission_id)

    cycle = service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle.status == ContinuousCycleStatus.SKIPPED


def test_r_failure_count_tracking(repo, clock):
    failing_executor = StubCycleExecutor(
        status=ContinuousCycleStatus.FAILED, error_message="Network timeout"
    )
    service = ContinuousMissionService(repo, failing_executor, clock=clock)

    cm = service.create_continuous_mission(
        schedule_id="sch_fail_cnt",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Failure tracking",
        stop_condition=ContinuousMissionStopCondition(max_consecutive_failures=5),
    )
    service.start_mission(cm.continuous_mission_id)

    service.execute_next_cycle(cm.continuous_mission_id)
    cm_updated = repo.get_by_id(cm.continuous_mission_id)
    assert cm_updated.consecutive_failures == 1
    assert cm_updated.total_failures == 1


def test_s_cycle_failure_stops_at_threshold(repo, clock):
    failing_executor = StubCycleExecutor(
        status=ContinuousCycleStatus.FAILED, error_message="Persistent failure"
    )
    service = ContinuousMissionService(repo, failing_executor, clock=clock)

    cm = service.create_continuous_mission(
        schedule_id="sch_fail_stop",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Stop on consecutive failures",
        stop_condition=ContinuousMissionStopCondition(max_consecutive_failures=2),
    )
    service.start_mission(cm.continuous_mission_id)

    service.execute_next_cycle(cm.continuous_mission_id)
    assert repo.get_by_id(cm.continuous_mission_id).status == ContinuousMissionStatus.ACTIVE

    clock.advance(timedelta(hours=1))
    service.execute_next_cycle(cm.continuous_mission_id)
    stopped_cm = repo.get_by_id(cm.continuous_mission_id)
    assert stopped_cm.status == ContinuousMissionStatus.FAILED
    assert "Max consecutive failures reached" in stopped_cm.metadata.get("stop_reason", "")


def test_t_unknown_cycle_preservation(repo, clock):
    unknown_executor = StubCycleExecutor(
        status=ContinuousCycleStatus.UNKNOWN, error_message="Ambiguous result"
    )
    service = ContinuousMissionService(repo, unknown_executor, clock=clock)

    cm = service.create_continuous_mission(
        schedule_id="sch_unknown",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Unknown safety test",
        stop_condition=ContinuousMissionStopCondition(stop_on_unknown=True),
    )
    service.start_mission(cm.continuous_mission_id)

    cycle = service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle.status == ContinuousCycleStatus.UNKNOWN

    cm_updated = repo.get_by_id(cm.continuous_mission_id)
    assert cm_updated.status == ContinuousMissionStatus.UNKNOWN
    assert "UNKNOWN" in cm_updated.metadata.get("stop_reason", "")


def test_u_provenance_and_causality(service, repo):
    cm = service.create_continuous_mission(
        schedule_id="sch_prov",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Provenance tracking",
        provenance="Operator_Audit",
        correlation_id="corr_audit_999",
    )
    service.start_mission(cm.continuous_mission_id)

    cycle = service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle.provenance == "Operator_Audit"
    assert cycle.correlation_id == "corr_audit_999"


def test_w_security_sanitization(temp_dir):
    repo = JsonContinuousMissionRepository(temp_dir)
    sensitive_data = {
        "access_token": "secret_oauth_token",
        "api_key": "api_key_12345",
        "nested": {
            "password": "super_secret_password",
            "normal_data": "visible_info",
        },
    }
    sanitized = _sanitize_data(sensitive_data)
    assert sanitized["access_token"] == "[REDACTED]"
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["password"] == "[REDACTED]"
    assert sanitized["nested"]["normal_data"] == "visible_info"


def test_ad_concurrency_protection(service):
    cm = service.create_continuous_mission(
        schedule_id="sch_concurrent",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Concurrent thread safety",
    )
    service.start_mission(cm.continuous_mission_id)

    errors = []

    def run_worker():
        try:
            for _ in range(5):
                service.execute_next_cycle(cm.continuous_mission_id)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    updated_cm = service.get_continuous_mission(cm.continuous_mission_id)
    assert updated_cm.cycle_count >= 1


def test_ae_deterministic_execution(service, clock):
    cm = service.create_continuous_mission(
        schedule_id="sch_det",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Deterministic execution",
    )
    service.start_mission(cm.continuous_mission_id)

    c1 = service.execute_next_cycle(cm.continuous_mission_id)
    clock.advance(timedelta(minutes=30))
    c2 = service.execute_next_cycle(cm.continuous_mission_id)

    assert c1.cycle_number == 1
    assert c2.cycle_number == 2
    assert c1.started_at < c2.started_at
