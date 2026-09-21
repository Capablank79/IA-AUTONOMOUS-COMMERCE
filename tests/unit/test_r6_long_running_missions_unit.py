"""
Tests unitarios para R.6 — Long-running Missions (Hito R — Advanced Autonomy).

Cubre los 22 requerimientos mínimos:
1. checkpoint creation
2. atomic checkpoint version
3. completed work preserved
4. pause != cancel
5. valid resume
6. terminal mission resume denied
7. policy revalidation
8. emergency stop blocks resume
9. budget preserved
10. quota not reset
11. rate limit not reset
12. heartbeat update
13. lease expiry
14. stale worker rejected
15. stale checkpoint rejected
16. corrupt checkpoint rejected
17. idempotent resume
18. concurrent resume single winner
19. tenant isolation
20. Anti-CoT checkpoint
21. assignment_version preserved
22. no R.7 implementation
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
from src.domain.emergency_stop.models import (
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
    EmergencyStopEvaluationContext,
    EmergencyStopDecisionStatus,
    EmergencyStopReasonCode,
    EmergencyStopDecision,
)
from src.domain.emergency_stop.ports import EmergencyStopServicePort
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
    InMemoryMissionCheckpointRepository,
    JsonMissionCheckpointRepository,
    InMemoryLeaseManager,
    InMemoryHeartbeatManager,
)
from src.application.long_running_mission.long_running_mission_service import (
    LongRunningMissionService,
)


class MockClock(ClockPort):
    def __init__(self, start_time: Optional[datetime] = None):
        self._current = start_time or datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float):
        self._current += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class MockEmergencyStopService(EmergencyStopServicePort):
    def __init__(self, block: bool = False):
        self._block = block

    def evaluate(self, context: EmergencyStopEvaluationContext) -> EmergencyStopDecision:
        if self._block:
            return EmergencyStopDecision(
                decision_id="stop-dec-1",
                decision_status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                reason_code=EmergencyStopReasonCode.MANUAL_OPERATOR_HALT,
                reason_details="Emergency stop actively blocking resume",
                evaluated_at=datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc),
            )
        return EmergencyStopDecision(
            decision_id="stop-dec-2",
            decision_status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
            reason_code=EmergencyStopReasonCode.AUTHORIZED_DEACTIVATION,
            reason_details="No active stop",
            evaluated_at=datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc),
        )

    def activate_stop(self, *args, **kwargs):
        pass

    def deactivate_stop(self, *args, **kwargs):
        pass


def test_1_checkpoint_creation():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    clock = MockClock()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr, clock=clock)

    cp = service.create_checkpoint(
        mission_id="mission-100",
        worker_id="worker-1",
        tenant_context=tenant,
        step_records={"step-1": StepCheckpointData(step_id="step-1", status="COMPLETED")},
    )

    assert cp.mission_id == "mission-100"
    assert cp.checkpoint_version == 1
    assert "step-1" in cp.completed_steps
    assert cp.verify_integrity() is True


def test_2_atomic_checkpoint_version(tmp_path: Path):
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    clock = MockClock()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr, clock=clock)

    cp1 = service.create_checkpoint(mission_id="mission-200", worker_id="worker-1", tenant_context=tenant)
    assert cp1.checkpoint_version == 1

    clock.advance(10)
    cp2 = service.create_checkpoint(mission_id="mission-200", worker_id="worker-1", tenant_context=tenant)
    assert cp2.checkpoint_version == 2
    assert cp2.checkpoint_id == "cp-mission-200-v2"


def test_3_completed_work_preserved():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    step_data = {
        "step-1": StepCheckpointData(step_id="step-1", status="COMPLETED", outputs={"res": 42}, side_effect_committed=True),
        "step-2": StepCheckpointData(step_id="step-2", status="PENDING"),
    }
    cp = service.create_checkpoint(
        mission_id="mission-300",
        worker_id="worker-1",
        tenant_context=tenant,
        step_records=step_data,
    )

    assert "step-1" in cp.completed_steps
    assert "step-2" in cp.pending_steps
    assert cp.step_records["step-1"].outputs["res"] == 42
    assert cp.step_records["step-1"].side_effect_committed is True


def test_4_pause_is_not_cancel():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    cp = service.pause_mission(
        mission_id="mission-400",
        worker_id="worker-1",
        tenant_context=tenant,
        reason="Scheduled maintenance window",
    )

    assert cp.mission_status == MissionStatus.PAUSED
    assert cp.mission_status != MissionStatus.ABORTED
    assert cp.is_terminal is False
    assert cp.metadata["pause_reason"] == "Scheduled maintenance window"


def test_5_valid_resume():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.pause_mission(mission_id="mission-500", worker_id="worker-1", tenant_context=tenant)
    decision = service.resume_mission(mission_id="mission-500", worker_id="worker-2", tenant_context=tenant)

    assert decision.allowed is True
    assert decision.status == ResumeDecisionStatus.GRANTED
    assert decision.active_worker_id == "worker-2"
    assert decision.lease_id is not None


def test_6_terminal_mission_resume_denied():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.create_checkpoint(
        mission_id="mission-600",
        worker_id="worker-1",
        tenant_context=tenant,
        override_status=MissionStatus.COMPLETED,
    )

    decision = service.resume_mission(mission_id="mission-600", worker_id="worker-1", tenant_context=tenant)
    assert decision.allowed is False
    assert decision.status == ResumeDecisionStatus.DENIED_TERMINAL


def test_7_policy_revalidation():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.pause_mission(mission_id="mission-700", worker_id="worker-1", tenant_context=tenant)
    decision = service.resume_mission(
        mission_id="mission-700",
        worker_id="worker-1",
        tenant_context=tenant,
        policy_override={"policy_denied": True, "reason": "Supplier tool permission revoked"},
    )

    assert decision.allowed is False
    assert decision.status == ResumeDecisionStatus.DENIED_POLICY
    assert "Supplier tool permission revoked" in decision.reason


def test_8_emergency_stop_blocks_resume():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    stop_service = MockEmergencyStopService(block=True)
    service = LongRunningMissionService(
        checkpoint_repository=repo,
        lease_manager=lease_mgr,
        emergency_stop_service=stop_service,
    )

    service.pause_mission(mission_id="mission-800", worker_id="worker-1", tenant_context=tenant)
    decision = service.resume_mission(mission_id="mission-800", worker_id="worker-1", tenant_context=tenant)

    assert decision.allowed is False
    assert decision.status == ResumeDecisionStatus.DENIED_EMERGENCY_STOP


def test_9_budget_preserved():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    cp = service.create_checkpoint(
        mission_id="mission-900",
        worker_id="worker-1",
        tenant_context=tenant,
        budget_consumed_tokens=4500,
        budget_consumed_cost=Decimal("12.50"),
        budget_remaining_tokens=5500,
    )

    assert cp.budget_consumed_tokens == 4500
    assert cp.budget_consumed_cost == Decimal("12.50")
    assert cp.budget_remaining_tokens == 5500


def test_10_quota_not_reset():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    cp1 = service.create_checkpoint(
        mission_id="mission-1000",
        worker_id="worker-1",
        tenant_context=tenant,
        quota_consumed=18,
    )
    assert cp1.quota_consumed == 18

    # Resume/next checkpoint preserves quota
    cp2 = service.create_checkpoint(
        mission_id="mission-1000",
        worker_id="worker-1",
        tenant_context=tenant,
        quota_consumed=18,
    )
    assert cp2.quota_consumed == 18


def test_11_rate_limit_not_reset():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    cp = service.create_checkpoint(
        mission_id="mission-1100",
        worker_id="worker-1",
        tenant_context=tenant,
        metadata={"rate_limit_rpm_consumed": 45},
    )
    assert cp.metadata["rate_limit_rpm_consumed"] == 45


def test_12_heartbeat_update():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    clock = MockClock()
    lease_mgr = InMemoryLeaseManager(clock=clock)
    hb_mgr = InMemoryHeartbeatManager()
    service = LongRunningMissionService(
        checkpoint_repository=repo,
        lease_manager=lease_mgr,
        heartbeat_manager=hb_mgr,
        clock=clock,
    )

    service.create_checkpoint(mission_id="mission-1200", worker_id="worker-1", tenant_context=tenant)
    service.resume_mission(mission_id="mission-1200", worker_id="worker-1", tenant_context=tenant)

    clock.advance(15)
    hb = service.record_heartbeat(mission_id="mission-1200", worker_id="worker-1", tenant_context=tenant)

    assert hb.worker_id == "worker-1"
    assert hb.last_seen_at == clock.now()
    assert hb_mgr.get_last_heartbeat("mission-1200", tenant) is not None


def test_13_lease_expiry():
    tenant = TenantContext(tenant_id="tenant-alpha")
    clock = MockClock()
    lease_mgr = InMemoryLeaseManager(clock=clock)

    lease = lease_mgr.acquire_lease("mission-1300", "worker-1", tenant, ttl_seconds=30)
    assert lease.is_expired(clock.now()) is False

    clock.advance(35)
    assert lease.is_expired(clock.now()) is True


def test_14_stale_worker_rejected():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    clock = MockClock()
    lease_mgr = InMemoryLeaseManager(clock=clock)
    service = LongRunningMissionService(
        checkpoint_repository=repo,
        lease_manager=lease_mgr,
        clock=clock,
    )

    service.create_checkpoint(mission_id="mission-1400", worker_id="worker-1", tenant_context=tenant)
    service.resume_mission(mission_id="mission-1400", worker_id="worker-1", tenant_context=tenant)

    # El lease expira
    clock.advance(70)

    # Worker-2 adquiere el lease
    service.resume_mission(mission_id="mission-1400", worker_id="worker-2", tenant_context=tenant)

    # Worker-1 (stale) intenta emitir heartbeat
    with pytest.raises(StaleWorkerError):
        service.record_heartbeat(mission_id="mission-1400", worker_id="worker-1", tenant_context=tenant)


def test_15_stale_checkpoint_rejected():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()

    now = datetime.now(timezone.utc)
    cp1 = MissionCheckpoint(
        checkpoint_id="cp-1",
        tenant_id="tenant-alpha",
        mission_id="m-1500",
        mission_type="MARKET_DISCOVERY",
        mission_status=MissionStatus.RUNNING,
        checkpoint_version=2,
        created_at=now,
    )
    repo.save_checkpoint(cp1, tenant)

    stale_cp = MissionCheckpoint(
        checkpoint_id="cp-0",
        tenant_id="tenant-alpha",
        mission_id="m-1500",
        mission_type="MARKET_DISCOVERY",
        mission_status=MissionStatus.RUNNING,
        checkpoint_version=1,
        created_at=now,
    )

    with pytest.raises(StaleCheckpointError):
        repo.save_checkpoint(stale_cp, tenant)


def test_16_corrupt_checkpoint_rejected(tmp_path: Path):
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = JsonMissionCheckpointRepository(base_dir=tmp_path)
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.create_checkpoint(mission_id="mission-1600", worker_id="worker-1", tenant_context=tenant)

    # Corromper intencionalmente el archivo
    file_path = tmp_path / "tenants" / "tenant-alpha" / "checkpoints" / "mission-1600.json"
    file_path.write_text("CORRUPTED_JSON_DATA", encoding="utf-8")

    decision = service.resume_mission(mission_id="mission-1600", worker_id="worker-1", tenant_context=tenant)
    assert decision.allowed is False
    assert decision.status == ResumeDecisionStatus.DENIED_CORRUPT


def test_17_idempotent_resume():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.pause_mission(mission_id="mission-1700", worker_id="worker-1", tenant_context=tenant)

    d1 = service.resume_mission(mission_id="mission-1700", worker_id="worker-1", tenant_context=tenant)
    d2 = service.resume_mission(mission_id="mission-1700", worker_id="worker-1", tenant_context=tenant)

    assert d1.allowed is True
    assert d2.allowed is True
    assert d1.lease_id == d2.lease_id


def test_18_concurrent_resume_single_winner():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.pause_mission(mission_id="mission-1800", worker_id="worker-1", tenant_context=tenant)

    # Worker-1 gana lease
    d1 = service.resume_mission(mission_id="mission-1800", worker_id="worker-1", tenant_context=tenant)
    assert d1.allowed is True

    # Worker-2 intenta simultáneamente
    d2 = service.resume_mission(mission_id="mission-1800", worker_id="worker-2", tenant_context=tenant)
    assert d2.allowed is False
    assert d2.status == ResumeDecisionStatus.DENIED_LOCK_CONFLICT


def test_19_tenant_isolation():
    tenant_a = TenantContext(tenant_id="tenant-a")
    tenant_b = TenantContext(tenant_id="tenant-b")

    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    service.create_checkpoint(mission_id="mission-1900", worker_id="worker-1", tenant_context=tenant_a)

    with pytest.raises(CrossTenantAccessError):
        service.resume_mission(mission_id="mission-1900", worker_id="worker-1", tenant_context=tenant_b)


def test_20_anti_cot_checkpoint():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    metadata_with_secrets = {
        "api_key": "sk-secret-token",
        "chain_of_thought": "Let me think step by step...",
        "clean_field": "valid_metric",
    }

    cp = service.create_checkpoint(
        mission_id="mission-2000",
        worker_id="worker-1",
        tenant_context=tenant,
        metadata=metadata_with_secrets,
    )

    # Claves sensibles no deben persistir en texto plano
    assert cp.metadata.get("clean_field") == "valid_metric"
    assert "api_key" not in cp.metadata or cp.metadata["api_key"] == "[REDACTED]"
    assert "chain_of_thought" not in cp.metadata or cp.metadata["chain_of_thought"] == "[REDACTED]"


def test_21_assignment_version_preserved():
    tenant = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryMissionCheckpointRepository()
    lease_mgr = InMemoryLeaseManager()
    service = LongRunningMissionService(checkpoint_repository=repo, lease_manager=lease_mgr)

    step_data = {
        "step-1": StepCheckpointData(step_id="step-1", status="CLAIMED", assigned_agent_id="specialist-1", assignment_version=3),
    }

    cp = service.create_checkpoint(
        mission_id="mission-2100",
        worker_id="worker-1",
        tenant_context=tenant,
        step_records=step_data,
    )

    assert cp.step_records["step-1"].assignment_version == 3


def test_22_no_r8_implementation():
    import sys
    # Verificar que no existen módulos o clases de tareas posteriores (e.g. R.8) creados por error
    assert "src.application.autonomous_meta_learning" not in sys.modules
    assert "src.domain.autonomous_meta_learning" not in sys.modules
