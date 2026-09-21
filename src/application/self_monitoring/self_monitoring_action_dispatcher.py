"""
Despachador de acciones seguras de remediación para Self-monitoring (Hito R.7 — Advanced Autonomy).

Reutiliza directamente contratos canónicos:
- R.1: ExecutionPlanRepositoryPort + MultiStepPlanningServicePort.replan
- R.5: CoordinationSessionRepositoryPort + AgentCoordinatorPort.delegate_task
- R.6: LeaseManagerPort + LongRunningMissionServicePort.pause_mission
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Optional, Mapping, Any, Dict
from enum import Enum

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier, sanitize_security_data, deep_freeze
from src.domain.self_monitoring.models import (
    HealthAssessment,
    SelfMonitoringDecision,
    SelfMonitoringAction,
    MissionHealthStatus,
)
from src.domain.planning.ports import (
    ExecutionPlanRepositoryPort,
    MultiStepPlanningServicePort,
)
from src.domain.planning.models import StepFailureType
from src.domain.agent_coordination.ports import (
    CoordinationSessionRepositoryPort,
    AgentCoordinatorPort,
)
from src.domain.agent_coordination.delegation_models import (
    DelegationRequest,
    DelegationReason,
)
from src.domain.long_running_mission.ports import (
    LeaseManagerPort,
    LongRunningMissionServicePort,
)

logger = logging.getLogger("SelfMonitoringActionDispatcher")


class DispatchStatus(str, Enum):
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"
    AMBIGUOUS_SAFE_NOOP = "AMBIGUOUS_SAFE_NOOP"


@dataclass(frozen=True)
class DispatchResult:
    action: SelfMonitoringAction
    status: DispatchStatus
    target_resource: Optional[str]
    message: str
    details: Mapping[str, Any]


class SelfMonitoringActionDispatcher:
    """
    Orquestador / despachador de aplicación delgado para materializar de forma segura
    las decisiones tomadas por SelfMonitoringService.

    Invariantes:
    - BLOCK nunca muta a replan/delegation ni elude políticas.
    - Acciones acotadas y revalidación de entidades canónicas.
    - Ante ambigüedad de identificadores o falta de registros inequívocos, devuelve resultado seguro / no-op.
    """

    def __init__(
        self,
        planning_repo: Optional[ExecutionPlanRepositoryPort] = None,
        planning_service: Optional[MultiStepPlanningServicePort] = None,
        coordination_repo: Optional[CoordinationSessionRepositoryPort] = None,
        coordinator_service: Optional[AgentCoordinatorPort] = None,
        lease_manager: Optional[LeaseManagerPort] = None,
        mission_service: Optional[LongRunningMissionServicePort] = None,
    ):
        self._planning_repo = planning_repo
        self._planning_service = planning_service
        self._coordination_repo = coordination_repo
        self._coordinator_service = coordinator_service
        self._lease_manager = lease_manager
        self._mission_service = mission_service

    def dispatch(
        self,
        assessment: HealthAssessment,
        context: TenantContext,
    ) -> DispatchResult:
        CrossTenantGuard.assert_same_tenant(context, assessment.tenant_id, "dispatch")
        action = assessment.decision.action

        if action in (SelfMonitoringAction.NONE, SelfMonitoringAction.CONTINUE_WITH_WARNING):
            return DispatchResult(
                action=action,
                status=DispatchStatus.SKIPPED,
                target_resource=assessment.mission_id,
                message=f"No active remediation required for action {action.value}",
                details={},
            )

        if action == SelfMonitoringAction.BLOCK:
            return DispatchResult(
                action=action,
                status=DispatchStatus.REJECTED,
                target_resource=assessment.mission_id,
                message="Mission execution is blocked due to critical safety/policy condition.",
                details={"health_status": assessment.status.value},
            )

        if action == SelfMonitoringAction.ESCALATE:
            return DispatchResult(
                action=action,
                status=DispatchStatus.ESCALATED,
                target_resource=assessment.mission_id,
                message="Remediation bound reached or critical condition escalated to operators.",
                details={"remediation_counter": assessment.remediation_counter},
            )

        if action == SelfMonitoringAction.PAUSE:
            return self._dispatch_pause(assessment, context)

        if action == SelfMonitoringAction.REQUEST_REPLAN:
            return self._dispatch_replan(assessment, context)

        if action == SelfMonitoringAction.REQUEST_DELEGATION:
            return self._dispatch_delegation(assessment, context)

        return DispatchResult(
            action=action,
            status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
            target_resource=assessment.mission_id,
            message=f"Unhandled action {action.value}; safe fallback.",
            details={},
        )

    def _dispatch_pause(
        self,
        assessment: HealthAssessment,
        context: TenantContext,
    ) -> DispatchResult:
        if not self._mission_service:
            return DispatchResult(
                action=SelfMonitoringAction.PAUSE,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=assessment.mission_id,
                message="LongRunningMissionServicePort not configured; pause skipped safely.",
                details={},
            )

        active_worker_id = assessment.snapshot.active_worker_id
        if not active_worker_id and self._lease_manager:
            lease = self._lease_manager.get_lease(assessment.mission_id, context)
            if lease and lease.worker_id:
                active_worker_id = lease.worker_id

        worker_id = active_worker_id or "self_monitoring_service"
        try:
            checkpoint = self._mission_service.pause_mission(
                mission_id=assessment.mission_id,
                worker_id=worker_id,
                tenant_context=context,
                reason=assessment.decision.rationale or "Self-monitoring pause triggered",
            )
            return DispatchResult(
                action=SelfMonitoringAction.PAUSE,
                status=DispatchStatus.EXECUTED,
                target_resource=assessment.mission_id,
                message=f"Mission paused successfully at checkpoint {checkpoint.checkpoint_id}",
                details={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "version": getattr(checkpoint, "checkpoint_version", getattr(checkpoint, "version", 1)),
                },
            )
        except Exception as exc:
            logger.error(f"Failed to pause mission via R.6: {exc}")
            return DispatchResult(
                action=SelfMonitoringAction.PAUSE,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=assessment.mission_id,
                message=f"Error executing pause: {exc}",
                details={"error": str(exc)},
            )

    def _dispatch_replan(
        self,
        assessment: HealthAssessment,
        context: TenantContext,
    ) -> DispatchResult:
        if not self._planning_repo or not self._planning_service:
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_REPLAN,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=assessment.mission_id,
                message="Planning ports not configured; replan skipped safely.",
                details={},
            )

        plan = self._planning_repo.get_plan_by_mission_id(assessment.mission_id, context)
        if not plan:
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_REPLAN,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=assessment.mission_id,
                message="No active plan found for mission; replan aborted safely.",
                details={},
            )

        failed_step_id = assessment.decision.suggested_payload.get("failed_step_id")
        if not failed_step_id:
            for step in plan.steps:
                if step.status.value in ("FAILED", "BLOCKED"):
                    failed_step_id = step.step_id
                    break
        if not failed_step_id and plan.steps:
            failed_step_id = plan.steps[-1].step_id

        if not failed_step_id:
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_REPLAN,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=plan.plan_id,
                message="Cannot resolve failed step unambiguously; replan aborted safely.",
                details={},
            )

        try:
            result = self._planning_service.replan(
                plan_id=plan.plan_id,
                tenant_context=context,
                failed_step_id=failed_step_id,
                failure_type=StepFailureType.EXECUTION_FAILURE,
                failure_reason=assessment.decision.rationale or "Self-monitoring technical failure replan",
            )
            is_valid = result.success if hasattr(result, "success") else getattr(result, "is_valid", False)
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_REPLAN,
                status=DispatchStatus.EXECUTED if is_valid else DispatchStatus.REJECTED,
                target_resource=plan.plan_id,
                message=f"Replan evaluated with status {result.plan.status.value if result.plan else 'INVALID'}",
                details={"is_valid": is_valid, "new_plan_id": result.plan.plan_id if result.plan else None},
            )
        except Exception as exc:
            logger.error(f"Failed to replan via R.1: {exc}")
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_REPLAN,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=plan.plan_id,
                message=f"Error executing replan: {exc}",
                details={"error": str(exc)},
            )

    def _dispatch_delegation(
        self,
        assessment: HealthAssessment,
        context: TenantContext,
    ) -> DispatchResult:
        if not self._coordination_repo or not self._coordinator_service:
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_DELEGATION,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=assessment.mission_id,
                message="Coordination ports not configured; delegation skipped safely.",
                details={},
            )

        session = self._coordination_repo.get_session_by_mission(assessment.mission_id, context)
        if not session:
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_DELEGATION,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=assessment.mission_id,
                message="No coordination session found for mission; delegation aborted safely.",
                details={},
            )

        target_task = None
        for task in session.tasks.values() if isinstance(session.tasks, Mapping) else session.tasks:
            if task.assigned_agent_id and task.status.value in ("RUNNING", "READY", "BLOCKED", "CLAIMED"):
                target_task = task
                break

        if not target_task:
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_DELEGATION,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=session.session_id,
                message="No eligible task found in session for delegation; aborted safely.",
                details={},
            )

        req = DelegationRequest(
            tenant_id=context.tenant_id,
            mission_id=assessment.mission_id,
            task_id=target_task.task_id,
            from_agent_id=target_task.assigned_agent_id or "unassigned",
            required_capability=target_task.required_capability or "general",
            reason=DelegationReason.AGENT_UNAVAILABLE,
            current_attempt=1,
            correlation_id=context.correlation_id or f"corr-{assessment.mission_id}",
        )

        try:
            updated_session, record = self._coordinator_service.delegate_task(
                session_id=session.session_id,
                request=req,
                tenant_context=context,
            )
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_DELEGATION,
                status=DispatchStatus.EXECUTED,
                target_resource=session.session_id,
                message=f"Task {target_task.task_id} delegated successfully to {record.decision.to_agent_id}",
                details={"session_id": updated_session.session_id, "to_agent_id": record.decision.to_agent_id},
            )
        except Exception as exc:
            logger.error(f"Failed to delegate task via R.5: {exc}")
            return DispatchResult(
                action=SelfMonitoringAction.REQUEST_DELEGATION,
                status=DispatchStatus.AMBIGUOUS_SAFE_NOOP,
                target_resource=session.session_id,
                message=f"Error executing delegation: {exc}",
                details={"error": str(exc)},
            )
