"""
Tests de integración para R.6 — Long-running Missions (Hito R — Advanced Autonomy).

Escenarios cubiertos:
A. Running mission -> Checkpoint -> Pause -> Resume -> Continue.
B. Process/service restart simulation -> State recovered.
C. Completed steps not rerun.
D. Side effect idempotency preserved.
E. Lease expiry -> New owner recovery -> Old worker stale result rejected.
F. Concurrent resume -> Single winner.
G. Policy changed while paused -> Resume denied.
H. Budget/quota continuity.
I. Tenant A/B isolation.
J. R.1–R.5 state survives checkpoint/resume.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import pytest

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard, CrossTenantAccessError
from src.domain.reliability.ports import ClockPort
from src.domain.mission.models import Mission, MissionStatus, MissionPriority, MissionType
from src.domain.planning.models import PlanBudget
from src.domain.agent_coordination.models import (
    CoordinationSession,
    CoordinationTask,
    CoordinationStatus,
    CoordinationTaskStatus,
)
from src.domain.long_running_mission.models import (
    MissionCheckpoint,
    StepCheckpointData,
    CheckpointStatus,
    ResumeDecisionStatus,
    LeaseStatus,
    HeartbeatRecord,
    CheckpointIntegrityError,
    StaleWorkerError,
    StaleCheckpointError,
    ConcurrentResumeConflictError,
)
from src.infrastructure.persistence.data.json.long_running_mission_repository import (
    JsonMissionCheckpointRepository,
    InMemoryLeaseManager,
    InMemoryHeartbeatManager,
)
from src.infrastructure.persistence.data.json.coordination_session_repository import (
    InMemoryCoordinationSessionRepository,
)
from src.application.long_running_mission.long_running_mission_service import (
    LongRunningMissionService,
)


class MockClock(ClockPort):
    def __init__(self, start_time: Optional[datetime] = None):
        self._current = start_time or datetime(2026, 9, 16, 14, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float):
        self._current += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


def test_scenario_a_running_pause_resume_continue(tmp_path: Path):
    """Escenario A: running mission -> checkpoint -> pause -> resume -> continue."""
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    clock = MockClock()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr, clock=clock)

    # 1. Mission en ejecución crea checkpoint
    cp1 = service.create_checkpoint(
        mission_id="m-scen-a",
        worker_id="w-1",
        tenant_context=tenant,
        step_records={"step-1": StepCheckpointData(step_id="step-1", status="COMPLETED")},
    )
    assert cp1.checkpoint_version == 1

    # 2. Pausar misión
    cp2 = service.pause_mission(mission_id="m-scen-a", worker_id="w-1", tenant_context=tenant)
    assert cp2.mission_status == MissionStatus.PAUSED
    assert cp2.checkpoint_version == 2

    # 3. Reanudar con worker-2
    decision = service.resume_mission(mission_id="m-scen-a", worker_id="w-2", tenant_context=tenant)
    assert decision.allowed is True
    assert decision.status == ResumeDecisionStatus.GRANTED
    assert decision.active_worker_id == "w-2"

    # 4. Continuar ejecución
    cp3 = service.create_checkpoint(
        mission_id="m-scen-a",
        worker_id="w-2",
        tenant_context=tenant,
        step_records={
            "step-1": StepCheckpointData(step_id="step-1", status="COMPLETED"),
            "step-2": StepCheckpointData(step_id="step-2", status="COMPLETED"),
        },
    )
    assert cp3.checkpoint_version == 3
    assert len(cp3.completed_steps) == 2


def test_scenario_b_process_restart_recovery(tmp_path: Path):
    """Escenario B: process/service restart simulation -> state recovered."""
    tenant = TenantContext(tenant_id="tenant-beta")
    clock = MockClock()

    # Instancia de servicio 1 (simula proceso vivo antes del crash)
    repo1 = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr1 = InMemoryLeaseManager(clock=clock)
    service1 = LongRunningMissionService(checkpoint_repository=repo1, lease_manager=lease_mgr1, clock=clock)

    service1.create_checkpoint(
        mission_id="m-scen-b",
        worker_id="w-crash",
        tenant_context=tenant,
        step_records={"step-calc": StepCheckpointData(step_id="step-calc", status="COMPLETED", outputs={"val": 100})},
        budget_consumed_tokens=1500,
        budget_consumed_cost=Decimal("3.50"),
    )

    # Simular crash: Se destruye service1, lease_mgr1 y memoria RAM.
    del service1
    del lease_mgr1
    del repo1

    # Instancia de servicio 2 (simula nuevo proceso arrancado de cero en un contenedor nuevo)
    repo2 = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr2 = InMemoryLeaseManager(clock=clock)
    service2 = LongRunningMissionService(checkpoint_repository=repo2, lease_manager=lease_mgr2, clock=clock)

    # Recuperar estado y reanudar
    decision = service2.resume_mission(mission_id="m-scen-b", worker_id="w-new", tenant_context=tenant)
    assert decision.allowed is True
    assert decision.active_worker_id == "w-new"

    recovered_cp = repo2.get_latest_checkpoint("m-scen-b", tenant)
    assert recovered_cp is not None
    assert recovered_cp.budget_consumed_tokens == 1500
    assert recovered_cp.budget_consumed_cost == Decimal("3.50")
    assert recovered_cp.step_records["step-calc"].outputs["val"] == 100


def test_scenario_c_completed_steps_not_rerun(tmp_path: Path):
    """Escenario C: completed steps not rerun."""
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    step_data = {
        "step-discovery": StepCheckpointData(step_id="step-discovery", status="COMPLETED", outputs={"found": 5}),
        "step-purchase": StepCheckpointData(step_id="step-purchase", status="PENDING"),
    }
    service.create_checkpoint(
        mission_id="m-scen-c",
        worker_id="w-1",
        tenant_context=tenant,
        step_records=step_data,
    )

    # Al reanudar, step-discovery se mantiene completado
    cp = repo.get_latest_checkpoint("m-scen-c", tenant)
    assert "step-discovery" in cp.completed_steps
    assert "step-purchase" in cp.pending_steps


def test_scenario_d_side_effect_idempotency_preserved(tmp_path: Path):
    """Escenario D: side effect idempotency preserved."""
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    step_data = {
        "step-publish": StepCheckpointData(
            step_id="step-publish",
            status="COMPLETED",
            is_side_effecting=True,
            side_effect_committed=True,
            outputs={"published_item_id": "item-999"},
        ),
    }

    cp = service.create_checkpoint(
        mission_id="m-scen-d",
        worker_id="w-1",
        tenant_context=tenant,
        step_records=step_data,
    )

    rec = cp.step_records["step-publish"]
    assert rec.is_side_effecting is True
    assert rec.side_effect_committed is True
    assert rec.outputs["published_item_id"] == "item-999"


def test_scenario_e_lease_expiry_and_stale_rejection(tmp_path: Path):
    """Escenario E: lease expiry -> new owner recovery -> old worker stale result rejected."""
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    clock = MockClock()
    lease_mgr = InMemoryLeaseManager(clock=clock)
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr, clock=clock)

    service.create_checkpoint(mission_id="m-scen-e", worker_id="w-old", tenant_context=tenant)
    service.resume_mission(mission_id="m-scen-e", worker_id="w-old", tenant_context=tenant)

    # Simular desconexión de red de w-old y expiración de lease
    clock.advance(65)

    # w-new toma el control
    decision = service.resume_mission(mission_id="m-scen-e", worker_id="w-new", tenant_context=tenant)
    assert decision.allowed is True
    assert decision.active_worker_id == "w-new"

    # w-old despierta tarde e intenta emitir un heartbeat o crear checkpoint
    with pytest.raises(StaleWorkerError):
        service.record_heartbeat(mission_id="m-scen-e", worker_id="w-old", tenant_context=tenant)

    with pytest.raises(StaleWorkerError):
        service.create_checkpoint(mission_id="m-scen-e", worker_id="w-old", tenant_context=tenant)


def test_scenario_f_concurrent_resume_single_winner(tmp_path: Path):
    """Escenario F: concurrent resume -> single winner."""
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.pause_mission(mission_id="m-scen-f", worker_id="w-init", tenant_context=tenant)

    d1 = service.resume_mission(mission_id="m-scen-f", worker_id="w-node-1", tenant_context=tenant)
    d2 = service.resume_mission(mission_id="m-scen-f", worker_id="w-node-2", tenant_context=tenant)

    assert d1.allowed is True
    assert d1.status == ResumeDecisionStatus.GRANTED

    assert d2.allowed is False
    assert d2.status == ResumeDecisionStatus.DENIED_LOCK_CONFLICT


def test_scenario_g_policy_changed_while_paused(tmp_path: Path):
    """Escenario G: policy changed while paused -> resume denied."""
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.pause_mission(mission_id="m-scen-g", worker_id="w-1", tenant_context=tenant)

    # Reanudar con política revocada
    decision = service.resume_mission(
        mission_id="m-scen-g",
        worker_id="w-1",
        tenant_context=tenant,
        policy_override={"policy_denied": True, "reason": "Tenant financial cap reached by admin"},
    )

    assert decision.allowed is False
    assert decision.status == ResumeDecisionStatus.DENIED_POLICY
    assert "Tenant financial cap reached" in decision.reason


def test_scenario_h_budget_quota_continuity(tmp_path: Path):
    """Escenario H: budget/quota continuity across pauses and restarts."""
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    # Turno 1
    cp1 = service.create_checkpoint(
        mission_id="m-scen-h",
        worker_id="w-1",
        tenant_context=tenant,
        budget_consumed_tokens=2000,
        budget_consumed_cost=Decimal("5.00"),
        quota_consumed=4,
    )
    assert cp1.budget_consumed_tokens == 2000

    service.pause_mission(mission_id="m-scen-h", worker_id="w-1", tenant_context=tenant)
    service.resume_mission(mission_id="m-scen-h", worker_id="w-2", tenant_context=tenant)

    # Turno 2 (acumula consumo)
    cp2 = service.create_checkpoint(
        mission_id="m-scen-h",
        worker_id="w-2",
        tenant_context=tenant,
        budget_consumed_tokens=3500,
        budget_consumed_cost=Decimal("8.50"),
        quota_consumed=7,
    )
    assert cp2.budget_consumed_tokens == 3500
    assert cp2.budget_consumed_cost == Decimal("8.50")
    assert cp2.quota_consumed == 7


def test_scenario_i_tenant_isolation(tmp_path: Path):
    """Escenario I: Tenant A/B isolation."""
    tenant_a = TenantContext(tenant_id="tenant-corp-a")
    tenant_b = TenantContext(tenant_id="tenant-corp-b")

    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.create_checkpoint(mission_id="m-scen-i", worker_id="w-a", tenant_context=tenant_a)

    # Tenant B no puede ver ni reanudar la misión de Tenant A
    with pytest.raises(CrossTenantAccessError):
        service.resume_mission(mission_id="m-scen-i", worker_id="w-b", tenant_context=tenant_b)


def test_scenario_j_r1_to_r5_state_survives(tmp_path: Path):
    """Escenario J: R.1–R.5 state survives checkpoint/resume."""
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    coord_repo = InMemoryCoordinationSessionRepository()

    # Configurar sesión R.4 con tareas y delegación R.5
    coord_task = CoordinationTask(
        task_id="task-r4",
        session_id="sess-1",
        tenant_id="tenant-alpha",
        required_capability="MARKET_SEARCH",
        action_type="SEARCH_SUPPLIERS",
        assigned_agent_id="agent-spec-1",
        status=CoordinationTaskStatus.CLAIMED,
        assignment_version=2,
        delegation_history=("agent-spec-0",),
    )
    coord_session = CoordinationSession(
        session_id="sess-1",
        tenant_id="tenant-alpha",
        mission_id="m-scen-j",
        correlation_id="corr-r4",
        plan_id="plan-dag-10",
        plan_version=2,
        tasks={"task-r4": coord_task},
    )
    coord_repo.save_session(coord_session, tenant)

    service = LongRunningMissionService(
        checkpoint_repository=repo,
        lease_manager=lease_mgr,
        coordination_repository=coord_repo,
    )

    cp = service.create_checkpoint(
        mission_id="m-scen-j",
        worker_id="w-coord",
        tenant_context=tenant,
    )

    assert cp.plan_id == "plan-dag-10"
    assert cp.plan_version == 2
    assert cp.coordination_session_id == "sess-1"
    assert cp.assignment_versions["task-r4"] == 2
    assert cp.step_records["task-r4"].assignment_version == 2
    assert cp.step_records["task-r4"].assigned_agent_id == "agent-spec-1"
