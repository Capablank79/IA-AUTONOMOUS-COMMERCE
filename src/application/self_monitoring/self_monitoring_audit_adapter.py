"""
Adaptador de auditoría productivo para Self-monitoring (Hito R.7 — Advanced Autonomy).

Reutiliza directamente:
- K.1 AuditRepositoryPort / AuditRecord / AuditActor
- K.2 AgentTraceService
- P.8 ProductionAlertingService
"""

from datetime import datetime, timezone
import logging
from typing import Optional, Mapping, Any
import uuid

from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import sanitize_security_data, deep_freeze
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import StepType, TraceStatus
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.domain.production_alerting.models import (
    AlertRuleType,
    AlertSeverity,
    ProductionAlertScope,
)
from src.application.production_alerting.production_alerting_service import ProductionAlertingService
from src.domain.self_monitoring.models import (
    HealthAssessment,
    SelfMonitoringDecision,
    MissionHealthStatus,
    SelfMonitoringAction,
)
from src.domain.self_monitoring.ports import SelfMonitoringAuditPort

logger = logging.getLogger("SelfMonitoringAuditAdapter")


class SelfMonitoringAuditAdapter(SelfMonitoringAuditPort):
    """
    Adaptador de auditoría y escalación para Self-monitoring.
    Conecta de forma segura y sanitizada los eventos de R.7 con K.1, K.2 y P.8.
    """

    def __init__(
        self,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_service: Optional[AgentTraceService] = None,
        alerting_service: Optional[ProductionAlertingService] = None,
    ):
        self._audit_repo = audit_repository
        self._trace_service = trace_service
        self._alerting_service = alerting_service

    def _build_actor(self, context: TenantContext) -> AuditActor:
        return AuditActor(
            actor_type=AuditActorType.SYSTEM,
            actor_id=context.identity_id or "self_monitoring_service",
            details={"tenant_id": context.tenant_id},
        )

    def record_assessment_audited(
        self,
        assessment: HealthAssessment,
        context: TenantContext,
    ) -> None:
        CrossTenantGuard.assert_same_tenant(context, assessment.tenant_id, "record_assessment_audited")
        now = datetime.now(timezone.utc)
        safe_meta = dict(deep_freeze(sanitize_security_data(assessment.metadata)))
        safe_meta.update({
            "health_status": assessment.status.value,
            "reason_codes": [r.value for r in assessment.snapshot.reason_codes],
            "decision_action": assessment.decision.action.value,
            "remediation_counter": assessment.remediation_counter,
        })

        if self._audit_repo is not None:
            record_id = f"aud-{uuid.uuid4().hex[:12]}"
            rec_type = AuditRecordType.SELF_MONITORING_ASSESSED
            if assessment.status == MissionHealthStatus.DEGRADED:
                rec_type = AuditRecordType.MISSION_DEGRADED
            elif assessment.status == MissionHealthStatus.AT_RISK:
                rec_type = AuditRecordType.MISSION_AT_RISK

            audit_rec = AuditRecord(
                audit_id=record_id,
                record_type=rec_type,
                occurred_at=assessment.evaluated_at if assessment.evaluated_at.tzinfo else now,
                actor=self._build_actor(context),
                subject_type="MISSION",
                subject_id=assessment.mission_id,
                action_or_operation="HEALTH_ASSESSMENT",
                status=assessment.status.value,
                correlation_id=context.correlation_id or f"corr-{assessment.mission_id}",
                mission_id=assessment.mission_id,
                evidence_reference=assessment.snapshot.checksum,
                provenance="SELF_MONITORING",
                idempotency_key=f"assess:{assessment.assessment_id}",
            )
            try:
                self._audit_repo.append(audit_rec)
            except Exception as exc:
                logger.error(f"Failed to record assessment audit: {exc}")

        if self._trace_service is not None:
            try:
                self._trace_service.record_step(
                    component_name="SelfMonitoringService",
                    execution_id=assessment.assessment_id,
                    step_number=1,
                    step_type=StepType.SERVICE_CALL,
                    operation="assess_mission_health",
                    status=TraceStatus.SUCCESS if assessment.status == MissionHealthStatus.HEALTHY else TraceStatus.FAILED if assessment.status == MissionHealthStatus.BLOCKED else TraceStatus.UNKNOWN,
                    started_at=assessment.evaluated_at,
                    completed_at=assessment.evaluated_at,
                    correlation_id=context.correlation_id or f"corr-{assessment.mission_id}",
                    mission_id=assessment.mission_id,
                    metadata=safe_meta,
                )
            except Exception as exc:
                logger.error(f"Failed to record assessment trace: {exc}")

    def record_action_requested(
        self,
        assessment: HealthAssessment,
        decision: SelfMonitoringDecision,
        context: TenantContext,
    ) -> None:
        CrossTenantGuard.assert_same_tenant(context, assessment.tenant_id, "record_action_requested")
        now = datetime.now(timezone.utc)
        safe_meta = dict(deep_freeze(sanitize_security_data({
            "action": decision.action.value,
            "rationale": decision.rationale,
            "suggested_target": decision.suggested_target,
            "remediation_counter": assessment.remediation_counter,
        })))

        if self._audit_repo is not None:
            record_id = f"aud-{uuid.uuid4().hex[:12]}"
            audit_rec = AuditRecord(
                audit_id=record_id,
                record_type=AuditRecordType.SELF_MONITORING_ACTION_REQUESTED,
                occurred_at=decision.evaluated_at if decision.evaluated_at.tzinfo else now,
                actor=self._build_actor(context),
                subject_type="MISSION",
                subject_id=assessment.mission_id,
                action_or_operation=f"ACTION_REQUESTED_{decision.action.value}",
                status="REQUESTED",
                correlation_id=context.correlation_id or f"corr-{assessment.mission_id}",
                mission_id=assessment.mission_id,
                provenance="SELF_MONITORING",
                idempotency_key=f"act_req:{assessment.assessment_id}:{decision.action.value}",
            )
            try:
                self._audit_repo.append(audit_rec)
            except Exception as exc:
                logger.error(f"Failed to record action audit: {exc}")

    def record_escalation(
        self,
        assessment: HealthAssessment,
        decision: SelfMonitoringDecision,
        context: TenantContext,
    ) -> None:
        CrossTenantGuard.assert_same_tenant(context, assessment.tenant_id, "record_escalation")
        now = datetime.now(timezone.utc)
        safe_evidence = dict(deep_freeze(sanitize_security_data({
            "mission_id": assessment.mission_id,
            "health_status": assessment.status.value,
            "reason_codes": [r.value for r in assessment.snapshot.reason_codes],
            "action": decision.action.value,
            "rationale": decision.rationale,
            "remediation_counter": assessment.remediation_counter,
        })))

        if self._audit_repo is not None:
            record_id = f"aud-{uuid.uuid4().hex[:12]}"
            audit_rec = AuditRecord(
                audit_id=record_id,
                record_type=AuditRecordType.SELF_MONITORING_ESCALATED,
                occurred_at=decision.evaluated_at if decision.evaluated_at.tzinfo else now,
                actor=self._build_actor(context),
                subject_type="MISSION",
                subject_id=assessment.mission_id,
                action_or_operation="ESCALATE_ALERT",
                status=decision.escalation_severity or "HIGH",
                correlation_id=context.correlation_id or f"corr-{assessment.mission_id}",
                mission_id=assessment.mission_id,
                provenance="SELF_MONITORING",
                idempotency_key=f"escalate:{assessment.assessment_id}",
            )
            try:
                self._audit_repo.append(audit_rec)
            except Exception as exc:
                logger.error(f"Failed to record escalation audit: {exc}")

        if self._alerting_service is not None:
            sev = AlertSeverity.CRITICAL if decision.escalation_severity == "CRITICAL" else AlertSeverity.HIGH
            try:
                self._alerting_service.raise_or_escalate_alert(
                    rule_type=AlertRuleType.MISSION_HEALTH_DEGRADED,
                    severity=sev,
                    summary=f"Mission {assessment.mission_id} health degraded: {decision.rationale}",
                    evidence=safe_evidence,
                    target_resource=assessment.mission_id,
                    scope=ProductionAlertScope.TENANT,
                    tenant_id=context.tenant_id,
                )
            except Exception as exc:
                logger.error(f"Failed to raise production alert for escalation: {exc}")

    def record_recovery(
        self,
        assessment: HealthAssessment,
        previous_status: MissionHealthStatus,
        context: TenantContext,
    ) -> None:
        CrossTenantGuard.assert_same_tenant(context, assessment.tenant_id, "record_recovery")
        now = datetime.now(timezone.utc)
        safe_meta = dict(deep_freeze(sanitize_security_data({
            "previous_status": previous_status.value,
            "recovered_status": assessment.status.value,
        })))

        if self._audit_repo is not None:
            record_id = f"aud-{uuid.uuid4().hex[:12]}"
            audit_rec = AuditRecord(
                audit_id=record_id,
                record_type=AuditRecordType.MISSION_HEALTH_RECOVERED,
                occurred_at=assessment.evaluated_at if assessment.evaluated_at.tzinfo else now,
                actor=self._build_actor(context),
                subject_type="MISSION",
                subject_id=assessment.mission_id,
                action_or_operation="HEALTH_RECOVERED",
                status=assessment.status.value,
                correlation_id=context.correlation_id or f"corr-{assessment.mission_id}",
                mission_id=assessment.mission_id,
                provenance="SELF_MONITORING",
                idempotency_key=f"recovery:{assessment.assessment_id}",
            )
            try:
                self._audit_repo.append(audit_rec)
            except Exception as exc:
                logger.error(f"Failed to record recovery audit: {exc}")
