import pytest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple

from src.domain.mission.models import MissionType, MissionPriority
from src.domain.scheduling.models import (
    Clock,
    SystemClock,
    DeterministicClock,
    Schedule,
    ScheduleConfig,
    ScheduleOccurrence,
    ScheduleStatus,
    ScheduleType,
    ExecutionStatus,
    MissedExecutionPolicy,
)
from src.domain.scheduling.ports import MissionTriggerPort, ScheduleRepository
from src.infrastructure.persistence.data.json.schedule_repository import JsonScheduleRepository
from src.application.scheduling.service import SchedulerService


class MockTrigger(MissionTriggerPort):
    def __init__(self, return_status: ExecutionStatus = ExecutionStatus.SUCCESS, return_error: Optional[str] = None):
        self.invocations = []
        self.return_status = return_status
        self.return_error = return_error

    def trigger(
        self, schedule: Schedule, occurrence: ScheduleOccurrence
    ) -> Tuple[str, ExecutionStatus, Optional[Dict[str, Any]], Optional[str]]:
        self.invocations.append((schedule, occurrence))
        mission_id = f"m_{schedule.schedule_id}_{len(self.invocations)}"
        summary = {"processed": True, "schedule_id": schedule.schedule_id}
        return mission_id, self.return_status, summary, self.return_error


@pytest.fixture
def temp_repo(tmp_path):
    return JsonScheduleRepository(tmp_path / "scheduler_storage")


@pytest.fixture
def fixed_clock():
    base_time = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    return DeterministicClock(base_time)


# A. create schedule
def test_a_create_schedule(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    sched = service.create_schedule(
        schedule_id="sched-1",
        mission_type=MissionType.MARKET_DISCOVERY,
        mission_parameters={"query": "electronics"},
        interval_seconds=300,
    )

    assert sched.schedule_id == "sched-1"
    assert sched.mission_type == MissionType.MARKET_DISCOVERY
    assert sched.status == ScheduleStatus.ACTIVE
    assert sched.config.interval_seconds == 300
    assert sched.next_run_at == fixed_clock.now()


# B. retrieve schedule
def test_b_retrieve_schedule(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    service.create_schedule(
        schedule_id="sched-2",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
    )

    retrieved = service.get_schedule("sched-2")
    assert retrieved is not None
    assert retrieved.schedule_id == "sched-2"
    assert retrieved.config.interval_seconds == 60


# C. enable schedule
def test_c_enable_schedule(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    service.create_schedule(
        schedule_id="sched-3",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
    )
    service.disable_schedule("sched-3")
    assert service.get_schedule("sched-3").status == ScheduleStatus.DISABLED

    enabled = service.enable_schedule("sched-3")
    assert enabled.status == ScheduleStatus.ACTIVE
    assert service.get_schedule("sched-3").status == ScheduleStatus.ACTIVE


# D. disable schedule
def test_d_disable_schedule(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    service.create_schedule(
        schedule_id="sched-4",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
    )

    disabled = service.disable_schedule("sched-4")
    assert disabled.status == ScheduleStatus.DISABLED
    assert service.get_schedule("sched-4").status == ScheduleStatus.DISABLED


# E. next_run_at calculation
def test_e_next_run_at(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    sched = service.create_schedule(
        schedule_id="sched-5",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=120,
    )
    assert sched.next_run_at == fixed_clock.now()

    # Ejecutar tick
    service.tick()
    updated = service.get_schedule("sched-5")
    assert updated.next_run_at == fixed_clock.now() + timedelta(seconds=120)


# F. interval calculation
def test_f_interval_calculation(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    sched = service.create_schedule(
        schedule_id="sched-6",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=3600,
    )

    service.tick()
    updated = service.get_schedule("sched-6")
    assert updated.next_run_at == fixed_clock.now() + timedelta(seconds=3600)
    assert updated.total_runs == 1


# G. immediate execution
def test_g_immediate_execution(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    service.create_schedule(
        schedule_id="sched-7",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=300,
    )

    occs = service.tick()
    assert len(occs) == 1
    assert len(trigger.invocations) == 1
    assert occs[0].status == ExecutionStatus.SUCCESS


# H. future execution
def test_h_future_execution(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    future_start = fixed_clock.now() + timedelta(minutes=10)
    service.create_schedule(
        schedule_id="sched-8",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=300,
        start_time=future_start,
    )

    # Tick en el presente: no debe ejecutarse
    occs = service.tick()
    assert len(occs) == 0
    assert len(trigger.invocations) == 0

    # Avanzar reloj 5 minutos: aún no
    fixed_clock.advance(300)
    occs = service.tick()
    assert len(occs) == 0

    # Avanzar otros 5 minutos (10 total): ahora sí
    fixed_clock.advance(300)
    occs = service.tick()
    assert len(occs) == 1
    assert len(trigger.invocations) == 1


# I. disabled schedule
def test_i_disabled_schedule(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    service.create_schedule(
        schedule_id="sched-9",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
    )
    service.disable_schedule("sched-9")

    occs = service.tick()
    assert len(occs) == 0
    assert len(trigger.invocations) == 0


# J. duplicate occurrence / K. idempotency
def test_j_k_duplicate_occurrence_and_idempotency(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    sched = service.create_schedule(
        schedule_id="sched-10",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
    )

    # Invocación 1
    occ1 = service._process_due_schedule(sched, fixed_clock.now())
    assert occ1 is not None
    assert len(trigger.invocations) == 1

    # Invocación 2 con el mismo instante exacto y schedule original (simulando replay)
    occ2 = service._process_due_schedule(sched, fixed_clock.now())
    assert occ2 is not None
    assert occ2.idempotency_key == occ1.idempotency_key
    # NO debe disparar una segunda vez
    assert len(trigger.invocations) == 1


# L. restart / reload
def test_l_restart_and_reload(tmp_path, fixed_clock):
    storage = tmp_path / "restart_storage"
    repo1 = JsonScheduleRepository(storage)
    trigger1 = MockTrigger()
    service1 = SchedulerService(repository=repo1, trigger=trigger1, clock=fixed_clock)

    service1.create_schedule(
        schedule_id="sched-restart",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=300,
    )
    service1.tick()
    assert len(trigger1.invocations) == 1

    # Simular reinicio creando una nueva instancia del servicio y repositorio desde disco
    del service1
    del repo1

    repo2 = JsonScheduleRepository(storage)
    trigger2 = MockTrigger()
    service2 = SchedulerService(repository=repo2, trigger=trigger2, clock=fixed_clock)

    reloaded = service2.get_schedule("sched-restart")
    assert reloaded is not None
    assert reloaded.total_runs == 1
    assert reloaded.status == ScheduleStatus.ACTIVE

    # Avanzar tiempo y verificar que continúa normalmente
    fixed_clock.advance(300)
    service2.tick()
    assert len(trigger2.invocations) == 1
    assert service2.get_schedule("sched-restart").total_runs == 2


# M. missed execution policy (SKIP vs bounded)
def test_m_missed_execution_policy(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    # Creamos schedule con intervalo de 60s y política SKIP
    sched = service.create_schedule(
        schedule_id="sched-missed",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
        missed_policy=MissedExecutionPolicy.SKIP,
    )
    # Ejecutamos primera vez
    service.tick()

    # Avanzamos reloj 1 hora (3600s = 60 intervalos perdidos)
    fixed_clock.advance(3600)

    # Tick posterior: debe ejecutar UNA sola vez y reajustar next_run_at hacia el futuro sin atorarse
    occs = service.tick()
    assert len(occs) == 1
    assert len(trigger.invocations) == 2  # Total invocations (1 inicial + 1 post-reinicio)
    updated = service.get_schedule("sched-missed")
    assert updated.next_run_at > fixed_clock.now()


# N. invalid schedule validation
def test_n_invalid_schedule(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    with pytest.raises(ValueError, match="interval_seconds must be a positive integer"):
        service.create_schedule(
            schedule_id="sched-invalid",
            mission_type=MissionType.MARKET_DISCOVERY,
            schedule_type=ScheduleType.INTERVAL,
            interval_seconds=-10,
        )


# O. timezone support
def test_o_timezone_handling(temp_repo):
    # Reloj configurado con timezone específico
    tz_santiago = timezone(timedelta(hours=-4))
    start_santiago = datetime(2026, 9, 1, 8, 0, 0, tzinfo=tz_santiago)
    clock = DeterministicClock(start_santiago)
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=clock)

    sched = service.create_schedule(
        schedule_id="sched-tz",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=300,
        timezone_str="America/Santiago",
        start_time=start_santiago,
    )

    assert sched.config.timezone_str == "America/Santiago"
    occs = service.tick()
    assert len(occs) == 1


# P. UNKNOWN trigger preservation
def test_p_unknown_trigger_preservation(temp_repo, fixed_clock):
    # Trigger configurado para retornar UNKNOWN
    trigger = MockTrigger(return_status=ExecutionStatus.UNKNOWN, return_error="Uncertain market condition")
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    service.create_schedule(
        schedule_id="sched-unknown",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
    )

    occs = service.tick()
    assert len(occs) == 1
    assert occs[0].status == ExecutionStatus.UNKNOWN
    assert "Uncertain" in occs[0].error


# Q. failed trigger
def test_q_failed_trigger_handling(temp_repo, fixed_clock):
    trigger = MockTrigger(return_status=ExecutionStatus.FAILED, return_error="Network error")
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    service.create_schedule(
        schedule_id="sched-failed",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
    )

    occs = service.tick()
    assert len(occs) == 1
    assert occs[0].status == ExecutionStatus.FAILED
    assert occs[0].error == "Network error"
    # El scheduler no muere y actualiza next_run_at
    updated = service.get_schedule("sched-failed")
    assert updated.next_run_at == fixed_clock.now() + timedelta(seconds=60)


# R. correlation_id & S. provenance
def test_r_s_correlation_and_provenance(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    sched = service.create_schedule(
        schedule_id="sched-provenance",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
        correlation_id="corr-xyz-123",
        provenance="autonomous_pipeline_v1",
    )

    assert sched.correlation_id == "corr-xyz-123"
    assert sched.provenance == "autonomous_pipeline_v1"

    retrieved = service.get_schedule("sched-provenance")
    assert retrieved.correlation_id == "corr-xyz-123"
    assert retrieved.provenance == "autonomous_pipeline_v1"


# T. sensitive data exclusion
def test_t_sensitive_data_exclusion(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    # Crear schedule con parámetros de misión
    service.create_schedule(
        schedule_id="sched-sec",
        mission_type=MissionType.MARKET_DISCOVERY,
        mission_parameters={"category": "test", "target": "ml"},
        interval_seconds=60,
    )

    # Verificar que el JSON en disco no contiene secretos ni claves peligrosas
    file_path = temp_repo._schedules_dir / "sched-sec.json"
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "password" not in content.lower()
    assert "token" not in content.lower()
    assert "secret" not in content.lower()
    assert "api_key" not in content.lower()


# U. deterministic clock verification
def test_u_deterministic_clock(fixed_clock):
    initial = fixed_clock.now()
    fixed_clock.advance(150)
    assert fixed_clock.now() == initial + timedelta(seconds=150)

    new_time = datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    fixed_clock.set_time(new_time)
    assert fixed_clock.now() == new_time


# V. concurrent duplicate protection
def test_v_concurrent_duplicate_protection(temp_repo, fixed_clock):
    trigger = MockTrigger()
    service = SchedulerService(repository=temp_repo, trigger=trigger, clock=fixed_clock)

    sched = service.create_schedule(
        schedule_id="sched-concurrent",
        mission_type=MissionType.MARKET_DISCOVERY,
        interval_seconds=60,
    )

    # Simular dos hilos/workers llamando a tick con la misma hora
    occ_a = service._process_due_schedule(sched, fixed_clock.now())
    occ_b = service._process_due_schedule(sched, fixed_clock.now())

    assert occ_a is not None
    assert occ_b is not None
    # Solo 1 invocación real de la misión
    assert len(trigger.invocations) == 1
