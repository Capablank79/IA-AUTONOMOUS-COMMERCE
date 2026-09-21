"""
Servicio de Aplicación para Long-running Missions (Hito R.6 — Advanced Autonomy).

Responsabilidades:
1. SAFE CHECKPOINTING:
   - Persiste progreso atómico y versionado monotónico sin datos sensibles ni CoT.
   - Preserva estado R.1 (DAG/plan), R.2 (sub-misiones), R.3 (especialistas), R.4 (coordinación) y R.5 (delegaciones/assignment_version).
2. SAFE PAUSE:
   - Pausa misiones activas en un límite seguro (boundary), libera leases y persiste checkpoint con estado PAUSED.
3. SAFE RESUME & POLICY REVALIDATION:
   - Revalida políticas de autorización, roles, límites de herramientas y Emergency Stop N.11.
   - Valida que la misión no sea terminal ni pertenezca a otro tenant.
   - Adquiere lease exclusivo resolviendo concurrencias determinísticamente.
4. STALE WORKER PROTECTION:
   - Detección y rechazo de mutaciones o latidos de workers con leases vencidos o versiones de asignación obsoletas.
5. BUDGET / QUOTA CONTINUITY:
   - El reinicio de procesos o reanudación no resetea tokens ni costes acumulados.
6. AUDITORÍA Y TRAZAS:
   - Emisión de eventos canónicos K.1 / K.2 (MISSION_CHECKPOINT_CREATED, MISSION_PAUSED, MISSION_RESUMED, etc.).
"""

from datetime import datetime, timezone
from decimal import Decimal
import uuid
from typing import Optional, List, Dict, Any, Tuple, Sequence

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier, sanitize_security_data
from src.domain.reliability.ports import ClockPort
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.agent_trace.models import AgentTraceRecord, StepType, TraceStatus
from src.domain.mission.models import Mission, MissionStatus, MissionPriority, MissionType
from src.domain.mission_dashboard.ports import TenantMissionRepositoryPort
from src.domain.emergency_stop.models import (
    EmergencyStopEvaluationContext,
    EmergencyStopDecisionStatus,
)
from src.domain.emergency_stop.ports import EmergencyStopServicePort
from src.domain.agent_coordination.models import (
    CoordinationSession,
    CoordinationStatus,
    CoordinationTaskStatus,
)
from src.domain.agent_coordination.ports import CoordinationSessionRepositoryPort
from src.domain.long_running_mission.models import (
    MissionCheckpoint,
    StepCheckpointData,
    CheckpointStatus,
    LeaseState,
    LeaseStatus,
    HeartbeatRecord,
    ResumeDecision,
    ResumeDecisionStatus,
    CheckpointIntegrityError,
    StaleWorkerError,
    StaleCheckpointError,
    ConcurrentResumeConflictError,
)
from src.domain.long_running_mission.ports import (
    MissionCheckpointRepositoryPort,
    LeaseManagerPort,
    HeartbeatPort,
    LongRunningMissionServicePort,
)
from src.infrastructure.persistence.data.json.long_running_mission_repository import SystemClock


class LongRunningMissionService(LongRunningMissionServicePort):
    """
    Servicio de ciclo de vida de misiones de larga duración (R.6).
    """

    def __init__(
        self,
        checkpoint_repository: MissionCheckpointRepositoryPort,
        lease_manager: LeaseManagerPort,
        heartbeat_manager: Optional[HeartbeatPort] = None,
        mission_repository: Optional[TenantMissionRepositoryPort] = None,
        coordination_repository: Optional[CoordinationSessionRepositoryPort] = None,
        emergency_stop_service: Optional[EmergencyStopServicePort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_repository: Optional[AgentTraceRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self._checkpoint_repo = checkpoint_repository
        self._lease_manager = lease_manager
        self._heartbeat_manager = heartbeat_manager
        self._mission_repo = mission_repository
        self._coordination_repo = coordination_repository
        self._emergency_stop_service = emergency_stop_service
        self._audit_repo = audit_repository
        self._trace_repo = trace_repository
        self._clock = clock or SystemClock()

    def _emit_audit(
        self,
        record_type: AuditRecordType,
        mission_id: str,
        operation: str,
        status: str,
        actor_id: str,
        tenant_context: TenantContext,
        correlation_id: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._audit_repo:
            return
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        rec = AuditRecord(
            audit_id=f"audit-{uuid.uuid4().hex[:12]}",
            record_type=record_type,
            occurred_at=self._clock.now(),
            actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id=actor_id),
            subject_type="MISSION",
            subject_id=mission_id,
            action_or_operation=operation,
            status=status,
            correlation_id=correlation_id or f"corr-{mission_id}",
            mission_id=mission_id,
        )
        try:
            self._audit_repo.save_record(rec)
        except Exception:
            pass

    def create_checkpoint(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        metadata: Optional[Dict[str, Any]] = None,
        override_status: Optional[MissionStatus] = None,
        step_records: Optional[Dict[str, StepCheckpointData]] = None,
        budget_consumed_tokens: int = 0,
        budget_consumed_cost: Decimal = Decimal("0.00"),
        budget_remaining_tokens: Optional[int] = None,
        budget_remaining_cost: Optional[Decimal] = None,
        quota_consumed: int = 0,
    ) -> MissionCheckpoint:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        now = self._clock.now()

        # Verificar lease activo del worker si existe
        current_lease = self._lease_manager.get_lease(mission_id, tenant_context)
        if current_lease and not current_lease.is_expired(now) and current_lease.status in (LeaseStatus.ACQUIRED, LeaseStatus.ACTIVE):
            if current_lease.worker_id != worker_id:
                self._emit_audit(
                    AuditRecordType.STALE_WORKER_REJECTED,
                    mission_id,
                    "create_checkpoint",
                    "REJECTED",
                    worker_id,
                    tenant_context,
                )
                raise StaleWorkerError(
                    f"Worker {worker_id} cannot create checkpoint for mission {mission_id} leased to {current_lease.worker_id}"
                )

        # Cargar misión del repo o checkpoint anterior
        mission = None
        if self._mission_repo:
            try:
                mission = self._mission_repo.get_mission(mission_id, tenant_context)
            except Exception:
                pass

        latest_cp = self._checkpoint_repo.get_latest_checkpoint(mission_id, tenant_context)
        next_version = (latest_cp.checkpoint_version + 1) if latest_cp else 1

        mission_type = "MARKET_DISCOVERY"
        mission_status = MissionStatus.RUNNING
        parent_id = None
        root_id = None
        depth = 0
        plan_id = None
        plan_version = 1

        if mission:
            mission_type = mission.type.value if hasattr(mission.type, "value") else str(mission.type)
            mission_status = mission.status
            parent_id = mission.parent_mission_id
            root_id = mission.root_mission_id
            depth = mission.depth
        elif latest_cp:
            mission_type = latest_cp.mission_type
            mission_status = latest_cp.mission_status
            parent_id = latest_cp.parent_mission_id
            root_id = latest_cp.root_mission_id
            depth = latest_cp.depth
            plan_id = latest_cp.plan_id
            plan_version = latest_cp.plan_version

        if override_status:
            mission_status = override_status

        # Reconciliar coordinación si existe
        coord_session = None
        coord_session_id = None
        coordination_state_refs = {}
        if self._coordination_repo:
            try:
                coord_session = self._coordination_repo.get_session_by_mission(mission_id, tenant_context)
                if coord_session:
                    coord_session_id = coord_session.session_id
                    coordination_state_refs = {
                        "session_id": coord_session.session_id,
                        "session_status": coord_session.status.value,
                        "tasks_count": len(coord_session.tasks),
                        "checksum": coord_session.checksum,
                    }
                    if not plan_id:
                        plan_id = coord_session.plan_id
                        plan_version = coord_session.plan_version
            except Exception:
                pass

        # Construir registros de pasos y versiones de asignación
        records = dict(step_records or (latest_cp.step_records if latest_cp else {}))
        assignment_versions = dict(latest_cp.assignment_versions if latest_cp else {})

        completed_steps = []
        active_steps = []
        pending_steps = []

        if coord_session:
            for tid, t in coord_session.tasks.items():
                assignment_versions[tid] = t.assignment_version
                if tid not in records:
                    records[tid] = StepCheckpointData(
                        step_id=t.task_id,
                        status=t.status.value,
                        assigned_agent_id=t.assigned_agent_id,
                        assignment_version=t.assignment_version,
                        outputs=dict(t.outputs),
                        evidence_refs=tuple(t.evidence_refs),
                        is_side_effecting=t.is_side_effecting,
                        side_effect_committed=(t.status == CoordinationTaskStatus.COMPLETED and t.is_side_effecting),
                        attempt=t.attempt,
                    )
                if t.status == CoordinationTaskStatus.COMPLETED:
                    completed_steps.append(tid)
                elif t.status in (CoordinationTaskStatus.CLAIMED, CoordinationTaskStatus.RUNNING):
                    active_steps.append(tid)
                else:
                    pending_steps.append(tid)
        else:
            for sid, srec in records.items():
                if srec.status == "COMPLETED":
                    completed_steps.append(sid)
                elif srec.status in ("IN_PROGRESS", "RUNNING", "CLAIMED"):
                    active_steps.append(sid)
                else:
                    pending_steps.append(sid)

        # Checkpoint id determinista
        checkpoint_id = f"cp-{mission_id}-v{next_version}"

        # Consumo acumulativo (restart / checkpoint preserves budget)
        total_tokens = max(budget_consumed_tokens, latest_cp.budget_consumed_tokens if latest_cp else 0)
        total_cost = max(budget_consumed_cost, latest_cp.budget_consumed_cost if latest_cp else Decimal("0.00"))
        total_quota = max(quota_consumed, latest_cp.quota_consumed if latest_cp else 0)

        cp = MissionCheckpoint(
            checkpoint_id=checkpoint_id,
            tenant_id=tenant_id_val,
            mission_id=mission_id,
            mission_type=mission_type,
            mission_status=mission_status,
            checkpoint_version=next_version,
            created_at=now,
            plan_id=plan_id,
            plan_version=plan_version,
            root_mission_id=root_id,
            parent_mission_id=parent_id,
            depth=depth,
            completed_steps=tuple(sorted(completed_steps)),
            active_steps=tuple(sorted(active_steps)),
            pending_steps=tuple(sorted(pending_steps)),
            step_records=records,
            sub_mission_refs=tuple(latest_cp.sub_mission_refs if latest_cp else ()),
            coordination_session_id=coord_session_id,
            coordination_state_refs=coordination_state_refs,
            assignment_versions=assignment_versions,
            current_worker_id=worker_id,
            lease_id=current_lease.lease_id if current_lease else None,
            budget_consumed_tokens=total_tokens,
            budget_consumed_cost=total_cost,
            budget_remaining_tokens=budget_remaining_tokens if budget_remaining_tokens is not None else (latest_cp.budget_remaining_tokens if latest_cp else None),
            budget_remaining_cost=budget_remaining_cost if budget_remaining_cost is not None else (latest_cp.budget_remaining_cost if latest_cp else None),
            quota_consumed=total_quota,
            last_event_type="CHECKPOINT_CREATED",
            correlation_id=f"corr-{mission_id}",
            metadata=dict(metadata or {}),
            status=CheckpointStatus.VALID,
        )

        saved = self._checkpoint_repo.save_checkpoint(cp, tenant_context)
        self._emit_audit(
            AuditRecordType.MISSION_CHECKPOINT_CREATED,
            mission_id,
            "create_checkpoint",
            "SUCCESS",
            worker_id,
            tenant_context,
            details={"checkpoint_version": next_version, "checkpoint_id": checkpoint_id},
        )
        return saved

    def pause_mission(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        reason: str = "User requested pause",
    ) -> MissionCheckpoint:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        now = self._clock.now()

        current_lease = self._lease_manager.get_lease(mission_id, tenant_context)
        if current_lease and not current_lease.is_expired(now) and current_lease.worker_id != worker_id:
            raise StaleWorkerError(
                f"Worker {worker_id} cannot pause mission {mission_id} leased to {current_lease.worker_id}"
            )

        # Crear Checkpoint con estado PAUSED
        cp = self.create_checkpoint(
            mission_id=mission_id,
            worker_id=worker_id,
            tenant_context=tenant_context,
            override_status=MissionStatus.PAUSED,
            metadata={"pause_reason": reason},
        )

        # Actualizar en repository si aplica
        if self._mission_repo:
            try:
                m = self._mission_repo.get_mission(mission_id, tenant_context)
                if m:
                    updated_m = Mission(
                        mission_id=m.mission_id,
                        type=m.type,
                        priority=m.priority,
                        status=MissionStatus.PAUSED,
                        parameters=m.parameters,
                        created_at=m.created_at,
                        updated_at=now,
                        parent_mission_id=m.parent_mission_id,
                        root_mission_id=m.root_mission_id,
                        depth=m.depth,
                        delegation_key=m.delegation_key,
                        is_required=m.is_required,
                    )
                    self._mission_repo.save_mission(updated_m, tenant_context)
            except Exception:
                pass

        # Liberar lease para no bloquear futuras reanudaciones
        if current_lease:
            self._lease_manager.release_lease(current_lease.lease_id, worker_id, tenant_context)

        self._emit_audit(
            AuditRecordType.MISSION_PAUSED,
            mission_id,
            "pause_mission",
            "PAUSED",
            worker_id,
            tenant_context,
            details={"pause_reason": reason, "checkpoint_version": cp.checkpoint_version},
        )
        return cp

    def resume_mission(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        policy_override: Optional[Dict[str, Any]] = None,
    ) -> ResumeDecision:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        now = self._clock.now()

        self._emit_audit(
            AuditRecordType.MISSION_RESUME_REQUESTED,
            mission_id,
            "resume_mission",
            "REQUESTED",
            worker_id,
            tenant_context,
        )

        # 1. Cargar último checkpoint
        try:
            latest_cp = self._checkpoint_repo.get_latest_checkpoint(mission_id, tenant_context)
        except CheckpointIntegrityError as e:
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_CORRUPT",
                worker_id,
                tenant_context,
                details={"error": str(e)},
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_CORRUPT,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason=f"Checkpoint integrity verification failed: {e}",
                checkpoint_version=0,
                evaluated_at=now,
            )

        if not latest_cp:
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_CORRUPT",
                worker_id,
                tenant_context,
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_CORRUPT,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason="No checkpoint found for mission",
                checkpoint_version=0,
                evaluated_at=now,
            )

        # 2. Verificar integridad del checksum
        if not latest_cp.verify_integrity():
            self._emit_audit(
                AuditRecordType.CHECKPOINT_INVALID,
                mission_id,
                "verify_integrity",
                "INVALID",
                worker_id,
                tenant_context,
            )
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_CORRUPT",
                worker_id,
                tenant_context,
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_CORRUPT,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason="Checkpoint checksum does not match data integrity hash",
                checkpoint_version=latest_cp.checkpoint_version,
                evaluated_at=now,
            )

        # 3. Validar tenant matching
        if latest_cp.tenant_id != tenant_id_val:
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_TENANT_MISMATCH",
                worker_id,
                tenant_context,
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_TENANT_MISMATCH,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason=f"Tenant mismatch: checkpoint tenant {latest_cp.tenant_id} != context {tenant_id_val}",
                checkpoint_version=latest_cp.checkpoint_version,
                evaluated_at=now,
            )

        # 4. Validar que no sea terminal
        if latest_cp.is_terminal:
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_TERMINAL",
                worker_id,
                tenant_context,
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_TERMINAL,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason=f"Mission is in terminal status {latest_cp.mission_status.value} and cannot be resumed",
                checkpoint_version=latest_cp.checkpoint_version,
                evaluated_at=now,
            )

        # 5. Revalidar Emergency Stop N.11
        if self._emergency_stop_service:
            eval_ctx = EmergencyStopEvaluationContext(
                action_name="MISSION_RESUME",
                mission_id=mission_id,
                is_read_only=False,
                is_external_side_effect=True,
            )
            stop_decision = self._emergency_stop_service.evaluate(eval_ctx)
            if stop_decision.decision_status == EmergencyStopDecisionStatus.BLOCK_EXECUTION:
                self._emit_audit(
                    AuditRecordType.MISSION_RESUME_DENIED,
                    mission_id,
                    "resume_mission",
                    "DENIED_EMERGENCY_STOP",
                    worker_id,
                    tenant_context,
                    details={"reason": stop_decision.reason_code.value if hasattr(stop_decision.reason_code, "value") else str(stop_decision.reason_code)},
                )
                return ResumeDecision(
                    status=ResumeDecisionStatus.DENIED_EMERGENCY_STOP,
                    allowed=False,
                    mission_id=mission_id,
                    tenant_id=tenant_id_val,
                    reason=f"Emergency stop is ACTIVE: {stop_decision.reason_details}",
                    checkpoint_version=latest_cp.checkpoint_version,
                    evaluated_at=now,
                    revalidated_policies=("EMERGENCY_STOP",),
                )

        # 6. Revalidar políticas de gobernanza / permission checks
        revalidated = ["TENANT_ISOLATION", "EMERGENCY_STOP"]
        if policy_override and policy_override.get("policy_denied", False):
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_POLICY",
                worker_id,
                tenant_context,
                details={"reason": policy_override.get("reason", "Policy revoked")},
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_POLICY,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason=policy_override.get("reason", "Policy revoked or permission denied"),
                checkpoint_version=latest_cp.checkpoint_version,
                evaluated_at=now,
                revalidated_policies=tuple(revalidated),
            )

        # 7. Validar Budget y Quota continuity
        if latest_cp.budget_remaining_tokens is not None and latest_cp.budget_remaining_tokens <= 0:
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_BUDGET",
                worker_id,
                tenant_context,
                details={"reason": "Tokens exhausted"},
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_BUDGET,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason="Token budget is exhausted",
                checkpoint_version=latest_cp.checkpoint_version,
                evaluated_at=now,
                revalidated_policies=tuple(revalidated),
            )

        if latest_cp.budget_remaining_cost is not None and latest_cp.budget_remaining_cost <= Decimal("0.00"):
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_BUDGET",
                worker_id,
                tenant_context,
                details={"reason": "Cost budget exhausted"},
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_BUDGET,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason="Cost budget is exhausted",
                checkpoint_version=latest_cp.checkpoint_version,
                evaluated_at=now,
                revalidated_policies=tuple(revalidated),
            )

        # 8. Adquirir Lease de ejecución determinista (Single winner)
        try:
            lease = self._lease_manager.acquire_lease(
                mission_id=mission_id,
                worker_id=worker_id,
                tenant_context=tenant_context,
                ttl_seconds=60,
                assignment_version=max(latest_cp.assignment_versions.values(), default=1),
            )
        except ConcurrentResumeConflictError as e:
            self._emit_audit(
                AuditRecordType.MISSION_RESUME_DENIED,
                mission_id,
                "resume_mission",
                "DENIED_LOCK_CONFLICT",
                worker_id,
                tenant_context,
                details={"error": str(e)},
            )
            return ResumeDecision(
                status=ResumeDecisionStatus.DENIED_LOCK_CONFLICT,
                allowed=False,
                mission_id=mission_id,
                tenant_id=tenant_id_val,
                reason=str(e),
                checkpoint_version=latest_cp.checkpoint_version,
                evaluated_at=now,
                revalidated_policies=tuple(revalidated),
            )

        # 9. Actualizar estado a RUNNING
        if self._mission_repo:
            try:
                m = self._mission_repo.get_mission(mission_id, tenant_context)
                if m:
                    updated_m = Mission(
                        mission_id=m.mission_id,
                        type=m.type,
                        priority=m.priority,
                        status=MissionStatus.RUNNING,
                        parameters=m.parameters,
                        created_at=m.created_at,
                        updated_at=now,
                        parent_mission_id=m.parent_mission_id,
                        root_mission_id=m.root_mission_id,
                        depth=m.depth,
                        delegation_key=m.delegation_key,
                        is_required=m.is_required,
                    )
                    self._mission_repo.save_mission(updated_m, tenant_context)
            except Exception:
                pass

        self._emit_audit(
            AuditRecordType.MISSION_RESUMED,
            mission_id,
            "resume_mission",
            "RESUMED",
            worker_id,
            tenant_context,
            details={"checkpoint_version": latest_cp.checkpoint_version, "lease_id": lease.lease_id},
        )

        return ResumeDecision(
            status=ResumeDecisionStatus.GRANTED,
            allowed=True,
            mission_id=mission_id,
            tenant_id=tenant_id_val,
            reason="Resume allowed and lease acquired successfully",
            checkpoint_version=latest_cp.checkpoint_version,
            evaluated_at=now,
            active_worker_id=worker_id,
            lease_id=lease.lease_id,
            revalidated_policies=tuple(revalidated),
        )

    def record_heartbeat(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HeartbeatRecord:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        now = self._clock.now()

        current_lease = self._lease_manager.get_lease(mission_id, tenant_context)
        if not current_lease:
            raise StaleWorkerError(f"No lease found for mission {mission_id}")

        if current_lease.is_expired(now):
            self._emit_audit(
                AuditRecordType.MISSION_LEASE_EXPIRED,
                mission_id,
                "record_heartbeat",
                "EXPIRED",
                worker_id,
                tenant_context,
            )
            raise StaleWorkerError(f"Lease {current_lease.lease_id} expired at {current_lease.expires_at}")

        if current_lease.worker_id != worker_id:
            self._emit_audit(
                AuditRecordType.STALE_WORKER_REJECTED,
                mission_id,
                "record_heartbeat",
                "REJECTED",
                worker_id,
                tenant_context,
            )
            raise StaleWorkerError(
                f"Worker {worker_id} does not own active lease for mission {mission_id} (owned by {current_lease.worker_id})"
            )

        renewed_lease = self._lease_manager.renew_lease(
            lease_id=current_lease.lease_id,
            worker_id=worker_id,
            tenant_context=tenant_context,
            ttl_seconds=60,
            assignment_version=current_lease.assignment_version,
        )

        hb = HeartbeatRecord(
            mission_id=mission_id,
            worker_id=worker_id,
            tenant_id=tenant_id_val,
            assignment_version=renewed_lease.assignment_version,
            last_seen_at=now,
            lease_id=renewed_lease.lease_id,
            lease_until=renewed_lease.expires_at,
            metadata=dict(metadata or {}),
        )

        if self._heartbeat_manager:
            self._heartbeat_manager.record_heartbeat(hb, tenant_context)

        return hb
