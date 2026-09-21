"""
Implementaciones de repositorios y Lease/Heartbeat Managers para Long-running Missions (Hito R.6).

Implementa:
- InMemoryMissionCheckpointRepository
- JsonMissionCheckpointRepository
- InMemoryLeaseManager
- InMemoryHeartbeatManager

Garantías:
- Aislamiento multi-tenant estricto vía CrossTenantGuard.
- Operaciones atómicas con bloqueo (threading.RLock), optimistic concurrency y fsync.
- ClockPort desacoplado para determinismo temporal sin sleeps en tests.
- Sanitización recursiva N.9 y Zero-CoT en almacenamiento.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import threading
from typing import Optional, List, Dict, Tuple, Any

from src.domain.security.models import validate_safe_identifier
from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.domain.mission.models import MissionStatus
from src.domain.long_running_mission.models import (
    MissionCheckpoint,
    StepCheckpointData,
    CheckpointStatus,
    LeaseState,
    LeaseStatus,
    HeartbeatRecord,
    CheckpointIntegrityError,
    StaleCheckpointError,
    StaleWorkerError,
    ConcurrentResumeConflictError,
)
from src.domain.long_running_mission.ports import (
    MissionCheckpointRepositoryPort,
    LeaseManagerPort,
    HeartbeatPort,
)


class SystemClock(ClockPort):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        pass


class InMemoryMissionCheckpointRepository(MissionCheckpointRepositoryPort):
    """
    Repositorio de Checkpoints en memoria aislado por tenant y seguro ante hilos.
    """

    def __init__(self):
        self._checkpoints: Dict[Tuple[str, str], List[MissionCheckpoint]] = {}  # (tenant_id, mission_id) -> list[MissionCheckpoint]
        self._lock = threading.RLock()

    def save_checkpoint(
        self,
        checkpoint: MissionCheckpoint,
        tenant_context: TenantContext,
    ) -> MissionCheckpoint:
        CrossTenantGuard.assert_same_tenant(tenant_context, checkpoint.tenant_id, "save_checkpoint")
        with self._lock:
            key = (checkpoint.tenant_id, checkpoint.mission_id)
            history = self._checkpoints.get(key, [])
            if history:
                latest = history[-1]
                if checkpoint.checkpoint_version <= latest.checkpoint_version:
                    raise StaleCheckpointError(
                        f"Cannot save checkpoint version {checkpoint.checkpoint_version} <= latest {latest.checkpoint_version}"
                    )
            history.append(checkpoint)
            self._checkpoints[key] = history
            return checkpoint

    def get_latest_checkpoint(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[MissionCheckpoint]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        with self._lock:
            key = (tenant_id_val, mission_id)
            history = self._checkpoints.get(key, [])
            if history:
                return history[-1]
            # Cross-tenant leak check
            for (t_id, m_id), h in self._checkpoints.items():
                if m_id == mission_id and t_id != tenant_id_val:
                    CrossTenantGuard.assert_same_tenant(tenant_context, t_id, "get_latest_checkpoint")
            return None

    def get_checkpoint_by_version(
        self,
        mission_id: str,
        version: int,
        tenant_context: TenantContext,
    ) -> Optional[MissionCheckpoint]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        with self._lock:
            key = (tenant_id_val, mission_id)
            history = self._checkpoints.get(key, [])
            for cp in history:
                if cp.checkpoint_version == version:
                    return cp
            return None

    def list_checkpoints(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> List[MissionCheckpoint]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        with self._lock:
            key = (tenant_id_val, mission_id)
            return list(self._checkpoints.get(key, []))


class JsonMissionCheckpointRepository(MissionCheckpointRepositoryPort):
    """
    Repositorio de Checkpoints en disco JSON con almacenamiento atómico (tmp + rename + fsync).
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_tenant_dir(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        t_dir = self.base_dir / "tenants" / tenant_id / "checkpoints"
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir

    def _get_mission_file(self, tenant_id: str, mission_id: str) -> Path:
        validate_safe_identifier(mission_id, "mission_id")
        return self._get_tenant_dir(tenant_id) / f"{mission_id}.json"

    def _serialize_checkpoint(self, cp: MissionCheckpoint) -> Dict[str, Any]:
        step_records_dict = {}
        for sid, rec in cp.step_records.items():
            step_records_dict[sid] = {
                "step_id": rec.step_id,
                "status": rec.status,
                "assigned_agent_id": rec.assigned_agent_id,
                "assignment_version": rec.assignment_version,
                "outputs": dict(rec.outputs),
                "evidence_refs": list(rec.evidence_refs),
                "is_side_effecting": rec.is_side_effecting,
                "side_effect_committed": rec.side_effect_committed,
                "attempt": rec.attempt,
            }

        return {
            "checkpoint_id": cp.checkpoint_id,
            "tenant_id": cp.tenant_id,
            "mission_id": cp.mission_id,
            "mission_type": cp.mission_type,
            "mission_status": cp.mission_status.value if hasattr(cp.mission_status, "value") else str(cp.mission_status),
            "checkpoint_version": cp.checkpoint_version,
            "created_at": cp.created_at.isoformat(),
            "plan_id": cp.plan_id,
            "plan_version": cp.plan_version,
            "root_mission_id": cp.root_mission_id,
            "parent_mission_id": cp.parent_mission_id,
            "depth": cp.depth,
            "completed_steps": list(cp.completed_steps),
            "active_steps": list(cp.active_steps),
            "pending_steps": list(cp.pending_steps),
            "step_records": step_records_dict,
            "sub_mission_refs": list(cp.sub_mission_refs),
            "coordination_session_id": cp.coordination_session_id,
            "coordination_state_refs": dict(cp.coordination_state_refs),
            "assignment_versions": dict(cp.assignment_versions),
            "current_worker_id": cp.current_worker_id,
            "lease_id": cp.lease_id,
            "budget_consumed_tokens": cp.budget_consumed_tokens,
            "budget_consumed_cost": str(cp.budget_consumed_cost),
            "budget_remaining_tokens": cp.budget_remaining_tokens,
            "budget_remaining_cost": str(cp.budget_remaining_cost) if cp.budget_remaining_cost is not None else None,
            "quota_consumed": cp.quota_consumed,
            "last_event_type": cp.last_event_type,
            "correlation_id": cp.correlation_id,
            "causation_id": cp.causation_id,
            "metadata": dict(cp.metadata),
            "status": cp.status.value if hasattr(cp.status, "value") else str(cp.status),
            "checksum": cp.checksum,
        }

    def _deserialize_checkpoint(self, raw: Dict[str, Any]) -> MissionCheckpoint:
        step_records = {}
        for sid, sraw in raw.get("step_records", {}).items():
            step_records[sid] = StepCheckpointData(
                step_id=sraw["step_id"],
                status=sraw["status"],
                assigned_agent_id=sraw.get("assigned_agent_id"),
                assignment_version=sraw.get("assignment_version", 1),
                outputs=sraw.get("outputs", {}),
                evidence_refs=tuple(sraw.get("evidence_refs", [])),
                is_side_effecting=sraw.get("is_side_effecting", False),
                side_effect_committed=sraw.get("side_effect_committed", False),
                attempt=sraw.get("attempt", 1),
            )

        cp = MissionCheckpoint(
            checkpoint_id=raw["checkpoint_id"],
            tenant_id=raw["tenant_id"],
            mission_id=raw["mission_id"],
            mission_type=raw["mission_type"],
            mission_status=MissionStatus(raw["mission_status"]),
            checkpoint_version=raw["checkpoint_version"],
            created_at=datetime.fromisoformat(raw["created_at"]),
            plan_id=raw.get("plan_id"),
            plan_version=raw.get("plan_version", 1),
            root_mission_id=raw.get("root_mission_id"),
            parent_mission_id=raw.get("parent_mission_id"),
            depth=raw.get("depth", 0),
            completed_steps=tuple(raw.get("completed_steps", [])),
            active_steps=tuple(raw.get("active_steps", [])),
            pending_steps=tuple(raw.get("pending_steps", [])),
            step_records=step_records,
            sub_mission_refs=tuple(raw.get("sub_mission_refs", [])),
            coordination_session_id=raw.get("coordination_session_id"),
            coordination_state_refs=raw.get("coordination_state_refs", {}),
            assignment_versions=raw.get("assignment_versions", {}),
            current_worker_id=raw.get("current_worker_id"),
            lease_id=raw.get("lease_id"),
            budget_consumed_tokens=raw.get("budget_consumed_tokens", 0),
            budget_consumed_cost=Decimal(str(raw.get("budget_consumed_cost", "0.00"))),
            budget_remaining_tokens=raw.get("budget_remaining_tokens"),
            budget_remaining_cost=Decimal(str(raw["budget_remaining_cost"])) if raw.get("budget_remaining_cost") is not None else None,
            quota_consumed=raw.get("quota_consumed", 0),
            last_event_type=raw.get("last_event_type"),
            correlation_id=raw.get("correlation_id", ""),
            causation_id=raw.get("causation_id"),
            metadata=raw.get("metadata", {}),
            status=CheckpointStatus(raw.get("status", CheckpointStatus.VALID.value)),
        )

        # Validar Checksum e integridad
        expected_checksum = raw.get("checksum")
        if expected_checksum and cp.checksum != expected_checksum:
            raise CheckpointIntegrityError(
                f"Checkpoint checksum mismatch: calculated {cp.checksum} != persisted {expected_checksum}"
            )
        return cp

    def _read_history(self, tenant_id: str, mission_id: str) -> List[MissionCheckpoint]:
        file_path = self._get_mission_file(tenant_id, mission_id)
        if not file_path.exists():
            return []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [self._deserialize_checkpoint(item) for item in data]
        except (json.JSONDecodeError, CheckpointIntegrityError, KeyError, ValueError) as e:
            raise CheckpointIntegrityError(f"Corrupt checkpoint file {file_path}: {e}") from e

    def save_checkpoint(
        self,
        checkpoint: MissionCheckpoint,
        tenant_context: TenantContext,
    ) -> MissionCheckpoint:
        CrossTenantGuard.assert_same_tenant(tenant_context, checkpoint.tenant_id, "save_checkpoint")
        with self._lock:
            history = self._read_history(checkpoint.tenant_id, checkpoint.mission_id)
            if history:
                latest = history[-1]
                if checkpoint.checkpoint_version <= latest.checkpoint_version:
                    raise StaleCheckpointError(
                        f"Cannot save checkpoint version {checkpoint.checkpoint_version} <= latest {latest.checkpoint_version}"
                    )
            history.append(checkpoint)
            serialized = [self._serialize_checkpoint(cp) for cp in history]

            file_path = self._get_mission_file(checkpoint.tenant_id, checkpoint.mission_id)
            tmp_path = file_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(serialized, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, file_path)
            return checkpoint

    def get_latest_checkpoint(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[MissionCheckpoint]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        with self._lock:
            history = self._read_history(tenant_id_val, mission_id)
            if history:
                return history[-1]
            # Cross-tenant guard check across directory partitions
            tenants_root = self.base_dir / "tenants"
            if tenants_root.exists():
                for t_dir in tenants_root.iterdir():
                    if t_dir.is_dir() and t_dir.name != tenant_id_val:
                        candidate_file = t_dir / "checkpoints" / f"{mission_id}.json"
                        if candidate_file.exists():
                            CrossTenantGuard.assert_same_tenant(tenant_context, t_dir.name, "get_latest_checkpoint")
            return None

    def get_checkpoint_by_version(
        self,
        mission_id: str,
        version: int,
        tenant_context: TenantContext,
    ) -> Optional[MissionCheckpoint]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        with self._lock:
            history = self._read_history(tenant_id_val, mission_id)
            for cp in history:
                if cp.checkpoint_version == version:
                    return cp
            return None

    def list_checkpoints(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> List[MissionCheckpoint]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        with self._lock:
            return self._read_history(tenant_id_val, mission_id)


class InMemoryLeaseManager(LeaseManagerPort):
    """
    Gestor de Leases de ejecución en memoria con exclusión mutua, soporte de ClockPort y detección de expiración.
    """

    def __init__(self, clock: Optional[ClockPort] = None):
        self._clock = clock or SystemClock()
        self._leases: Dict[Tuple[str, str], LeaseState] = {}  # (tenant_id, mission_id) -> LeaseState
        self._lock = threading.RLock()

    def acquire_lease(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        ttl_seconds: int = 60,
        assignment_version: int = 1,
    ) -> LeaseState:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        now = self._clock.now()

        with self._lock:
            key = (tenant_id_val, mission_id)
            current = self._leases.get(key)
            if current and not current.is_expired(now) and current.status in (LeaseStatus.ACQUIRED, LeaseStatus.ACTIVE):
                if current.worker_id != worker_id:
                    raise ConcurrentResumeConflictError(
                        f"Mission {mission_id} is currently leased to active worker {current.worker_id} until {current.expires_at}"
                    )
                # Mismo worker adquiriendo -> renueva/reconfirma
                new_state = LeaseState(
                    lease_id=current.lease_id,
                    mission_id=mission_id,
                    worker_id=worker_id,
                    tenant_id=tenant_id_val,
                    status=LeaseStatus.ACTIVE,
                    acquired_at=current.acquired_at,
                    expires_at=datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc),
                    assignment_version=assignment_version,
                    heartbeat_count=current.heartbeat_count,
                    last_heartbeat_at=current.last_heartbeat_at,
                )
                self._leases[key] = new_state
                return new_state

            # Nuevo lease
            lease_id = f"lease-{mission_id}-{worker_id}-{int(now.timestamp())}"
            new_state = LeaseState(
                lease_id=lease_id,
                mission_id=mission_id,
                worker_id=worker_id,
                tenant_id=tenant_id_val,
                status=LeaseStatus.ACQUIRED,
                acquired_at=now,
                expires_at=datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc),
                assignment_version=assignment_version,
                heartbeat_count=0,
                last_heartbeat_at=None,
            )
            self._leases[key] = new_state
            return new_state

    def renew_lease(
        self,
        lease_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        ttl_seconds: int = 60,
        assignment_version: int = 1,
    ) -> LeaseState:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        now = self._clock.now()

        with self._lock:
            found_key = None
            current = None
            for key, state in self._leases.items():
                if key[0] == tenant_id_val and state.lease_id == lease_id:
                    found_key = key
                    current = state
                    break

            if not current or not found_key:
                raise StaleWorkerError(f"Lease {lease_id} not found for tenant {tenant_id_val}")

            if current.is_expired(now):
                expired_state = LeaseState(
                    lease_id=current.lease_id,
                    mission_id=current.mission_id,
                    worker_id=current.worker_id,
                    tenant_id=current.tenant_id,
                    status=LeaseStatus.EXPIRED,
                    acquired_at=current.acquired_at,
                    expires_at=current.expires_at,
                    assignment_version=current.assignment_version,
                    heartbeat_count=current.heartbeat_count,
                    last_heartbeat_at=current.last_heartbeat_at,
                )
                self._leases[found_key] = expired_state
                raise StaleWorkerError(f"Lease {lease_id} expired at {current.expires_at}")

            if current.worker_id != worker_id:
                raise StaleWorkerError(f"Worker {worker_id} does not own lease {lease_id} (owned by {current.worker_id})")

            if assignment_version < current.assignment_version:
                raise StaleWorkerError(
                    f"Stale assignment version {assignment_version} < current {current.assignment_version}"
                )

            renewed = LeaseState(
                lease_id=current.lease_id,
                mission_id=current.mission_id,
                worker_id=worker_id,
                tenant_id=current.tenant_id,
                status=LeaseStatus.ACTIVE,
                acquired_at=current.acquired_at,
                expires_at=datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc),
                assignment_version=assignment_version,
                heartbeat_count=current.heartbeat_count + 1,
                last_heartbeat_at=now,
            )
            self._leases[found_key] = renewed
            return renewed

    def release_lease(
        self,
        lease_id: str,
        worker_id: str,
        tenant_context: TenantContext,
    ) -> bool:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        now = self._clock.now()

        with self._lock:
            for key, state in list(self._leases.items()):
                if key[0] == tenant_id_val and state.lease_id == lease_id:
                    if state.worker_id != worker_id:
                        return False
                    released = LeaseState(
                        lease_id=state.lease_id,
                        mission_id=state.mission_id,
                        worker_id=worker_id,
                        tenant_id=state.tenant_id,
                        status=LeaseStatus.RELEASED,
                        acquired_at=state.acquired_at,
                        expires_at=now,
                        assignment_version=state.assignment_version,
                        heartbeat_count=state.heartbeat_count,
                        last_heartbeat_at=state.last_heartbeat_at,
                    )
                    self._leases[key] = released
                    return True
            return False

    def get_lease(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[LeaseState]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        with self._lock:
            key = (tenant_id_val, mission_id)
            return self._leases.get(key)


class InMemoryHeartbeatManager(HeartbeatPort):
    """
    Gestor de latidos en memoria aislado por tenant.
    """

    def __init__(self):
        self._heartbeats: Dict[Tuple[str, str], HeartbeatRecord] = {}  # (tenant_id, mission_id) -> HeartbeatRecord
        self._lock = threading.RLock()

    def record_heartbeat(
        self,
        heartbeat: HeartbeatRecord,
        tenant_context: TenantContext,
    ) -> HeartbeatRecord:
        CrossTenantGuard.assert_same_tenant(tenant_context, heartbeat.tenant_id, "record_heartbeat")
        with self._lock:
            key = (heartbeat.tenant_id, heartbeat.mission_id)
            self._heartbeats[key] = heartbeat
            return heartbeat

    def get_last_heartbeat(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[HeartbeatRecord]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        with self._lock:
            key = (tenant_id_val, mission_id)
            return self._heartbeats.get(key)
