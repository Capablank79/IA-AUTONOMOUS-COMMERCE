"""
Approval Policy Service (Hito N.6 — Approval Policies).

Servicio de aplicación encargado de:
- Evaluar si una acción requiere aprobación humana u operativa previa según la ApprovalPolicy activa.
- Validar evidencia de aprobación (ApprovalEvidence) contra contexto, acción, recurso, identidad y expiración.
- Garantizar separación de funciones (Self-approval blocking).
- Emitir registros de auditoría K.1 y trazas de observabilidad K.2 seguras.
- Integrar con ClockPort K.7 para verificaciones temporales deterministas.
- Cero límites financieros numéricos (N.7).
- Cero sobreescritura de DENY de N.3.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Sequence, Mapping, Union, Tuple
from types import MappingProxyType
import uuid

from src.domain.approval.models import (
    ApprovalStatus,
    ApprovalReasonCode,
    ApprovalPolicy,
    ApprovalEvidence,
    ApprovalRequest,
    ApprovalDecision,
    compute_approval_checksum,
)
from src.domain.approval.ports import (
    ApprovalPolicyRepositoryPort,
    ApprovalEvidenceRepositoryPort,
)
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.domain.agent_trace.models import StepType, TraceStatus
from src.domain.reliability.ports import ClockPort
from src.infrastructure.reliability.reliability_infrastructure import SystemClock
from src.domain.security.models import (
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)


class InMemoryApprovalPolicyRepository(ApprovalPolicyRepositoryPort):
    """Repositorio en memoria para políticas de aprobación."""
    def __init__(self, initial_policies: Optional[Sequence[ApprovalPolicy]] = None):
        self._policies: Dict[str, ApprovalPolicy] = {}
        if initial_policies:
            for p in initial_policies:
                self.save_policy(p)

    def save_policy(self, policy: ApprovalPolicy) -> None:
        self._policies[policy.policy_name] = policy

    def get_policy(self, policy_name: str) -> Optional[ApprovalPolicy]:
        return self._policies.get(policy_name)

    def list_policies(self) -> Sequence[ApprovalPolicy]:
        return tuple(self._policies.values())


class ApprovalPolicyService:
    """
    Servicio de aplicación para N.6 Approval Policies.
    """

    def __init__(
        self,
        evidence_repository: ApprovalEvidenceRepositoryPort,
        policy_repository: Optional[ApprovalPolicyRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        agent_trace_service: Optional[AgentTraceService] = None,
        default_policy: Optional[ApprovalPolicy] = None,
    ):
        if evidence_repository is None:
            raise ValueError("evidence_repository cannot be None")
        self.evidence_repository = evidence_repository
        self.policy_repository = policy_repository or InMemoryApprovalPolicyRepository()
        self.clock = clock or SystemClock()
        self.audit_repository = audit_repository
        self.agent_trace_service = agent_trace_service

        if default_policy:
            self.policy_repository.save_policy(default_policy)
            self._default_policy_name = default_policy.policy_name
        else:
            # Registrar política comercial estándar por defecto
            std_policy = ApprovalPolicy(
                policy_name="default_commercial_approval_policy",
                version="1.0.0",
                actions_not_requiring_approval=(
                    "market_discovery.analyze",
                    "opportunity.evaluate",
                    "catalog.query",
                    "pricing.simulate",
                    "health_check",
                ),
                actions_requiring_approval=(
                    "listing.publish",
                    "listing.delete",
                    "price.update",
                    "inventory.write_off",
                    "order.cancel",
                    "return.accept",
                    "external_action.execute",
                ),
                prohibited_actions=(
                    "system.shutdown",
                    "credential.dump",
                ),
                require_separation_of_duties=True,
                default_ttl_seconds=3600,
                description="Default governance approval policy for commercial agent operations.",
            )
            self.policy_repository.save_policy(std_policy)
            self._default_policy_name = std_policy.policy_name

    def evaluate_request(
        self,
        request: ApprovalRequest,
        policy_override: Optional[ApprovalPolicy] = None,
    ) -> ApprovalDecision:
        """
        Evalúa una petición de aprobación.
        Retorna ApprovalDecision estructurada e inmutable.
        """
        now = self.clock.now()
        decision_id = f"APDEC-{uuid.uuid4().hex[:12]}"
        corr_id = request.correlation_id or f"corr-eval-{uuid.uuid4().hex[:8]}"

        # 1. Resolver política aplicable
        policy = policy_override or self.policy_repository.get_policy(request.policy_name)
        if policy is None:
            # Fallback seguro: Si no se encuentra la política, denegar con UNKNOWN
            decision = ApprovalDecision(
                decision_id=decision_id,
                status=ApprovalStatus.UNKNOWN,
                reason_code=ApprovalReasonCode.UNKNOWN_APPROVAL_POLICY,
                reason=f"Approval policy '{request.policy_name}' not found.",
                policy_name=request.policy_name,
                policy_version="0.0.0",
                evaluated_at=now,
                requesting_identity_id=request.requesting_identity_id,
                target_action=request.action,
                target_resource=request.resource,
                correlation_id=request.correlation_id,
            )
            self._record_audit_and_trace(request, decision)
            return decision

        # 2. Verificar si la acción está prohibida por la política
        if request.action in policy.prohibited_actions:
            decision = ApprovalDecision(
                decision_id=decision_id,
                status=ApprovalStatus.REJECTED,
                reason_code=ApprovalReasonCode.APPROVAL_REJECTED_BY_OPERATOR,
                reason=f"Action '{request.action}' is explicitly prohibited by policy '{policy.policy_name}'.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=now,
                requesting_identity_id=request.requesting_identity_id,
                target_action=request.action,
                target_resource=request.resource,
                correlation_id=request.correlation_id,
            )
            self._record_audit_and_trace(request, decision)
            return decision

        # 3. Determinar si la acción requiere aprobación o si se adjuntó evidencia explícita a evaluar
        requires_approval = (
            request.action in policy.actions_requiring_approval
            or request.is_irreversible
            or (request.is_external_impact and request.action not in policy.actions_not_requiring_approval)
            or bool(request.attached_evidence_id)
        )

        if not requires_approval and request.action in policy.actions_not_requiring_approval:
            # Acción permitida directamente sin aprobación
            decision = ApprovalDecision(
                decision_id=decision_id,
                status=ApprovalStatus.NOT_REQUIRED,
                reason_code=ApprovalReasonCode.APPROVAL_NOT_REQUIRED,
                reason=f"Action '{request.action}' does not require approval under policy '{policy.policy_name}'.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=now,
                requesting_identity_id=request.requesting_identity_id,
                target_action=request.action,
                target_resource=request.resource,
                correlation_id=request.correlation_id,
            )
            self._record_audit_and_trace(request, decision)
            return decision

        # 4. Requiere aprobación: Verificar si se adjuntó evidencia de aprobación válida
        if not request.attached_evidence_id:
            # Buscar si existe evidencia persistida previamente para este target
            candidates = self.evidence_repository.list_by_target(request.action, request.resource)
            valid_evidence: Optional[ApprovalEvidence] = None
            last_reason_code = ApprovalReasonCode.APPROVAL_REQUIRED_BY_POLICY

            for cand in candidates:
                is_valid, rcode = cand.is_valid_for(
                    action=request.action,
                    resource=request.resource,
                    requesting_identity_id=request.requesting_identity_id,
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                    current_time=now,
                    require_separation_of_duties=policy.require_separation_of_duties,
                )
                if is_valid:
                    valid_evidence = cand
                    break
                else:
                    last_reason_code = rcode

            if valid_evidence is None:
                decision = ApprovalDecision(
                    decision_id=decision_id,
                    status=ApprovalStatus.APPROVAL_REQUIRED,
                    reason_code=last_reason_code,
                    reason=f"Action '{request.action}' on resource '{request.resource}' requires explicit approval.",
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                    evaluated_at=now,
                    requesting_identity_id=request.requesting_identity_id,
                    target_action=request.action,
                    target_resource=request.resource,
                    correlation_id=request.correlation_id,
                )
                self._record_audit_and_trace(request, decision)
                return decision

            # Evidencia encontrada y válida
            decision = ApprovalDecision(
                decision_id=decision_id,
                status=ApprovalStatus.APPROVED,
                reason_code=ApprovalReasonCode.VALID_APPROVAL_ATTACHED,
                reason=f"Valid approval evidence '{valid_evidence.approval_id}' attached for action '{request.action}'.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=now,
                requesting_identity_id=request.requesting_identity_id,
                target_action=request.action,
                target_resource=request.resource,
                correlation_id=request.correlation_id,
                evidence_id=valid_evidence.approval_id,
                approver_identity_id=valid_evidence.approver_identity_id,
                expires_at=valid_evidence.expires_at,
            )
            self._record_audit_and_trace(request, decision)
            return decision

        # 5. Se adjuntó un ID de evidencia específico en el request
        try:
            evidence = self.evidence_repository.get_by_id(request.attached_evidence_id)
        except Exception as ex:
            # Capturar fallos de integridad / corrupción del registro en persistencia
            decision = ApprovalDecision(
                decision_id=decision_id,
                status=ApprovalStatus.ERROR,
                reason_code=ApprovalReasonCode.APPROVAL_EVIDENCE_CORRUPTED,
                reason=f"Attached approval evidence '{request.attached_evidence_id}' could not be loaded: {str(ex)}",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=now,
                requesting_identity_id=request.requesting_identity_id,
                target_action=request.action,
                target_resource=request.resource,
                correlation_id=request.correlation_id,
            )
            self._record_audit_and_trace(request, decision)
            return decision

        if evidence is None:
            decision = ApprovalDecision(
                decision_id=decision_id,
                status=ApprovalStatus.APPROVAL_REQUIRED,
                reason_code=ApprovalReasonCode.APPROVAL_EVIDENCE_NOT_FOUND,
                reason=f"Attached approval evidence '{request.attached_evidence_id}' was not found.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=now,
                requesting_identity_id=request.requesting_identity_id,
                target_action=request.action,
                target_resource=request.resource,
                correlation_id=request.correlation_id,
            )
            self._record_audit_and_trace(request, decision)
            return decision

        is_valid, rcode = evidence.is_valid_for(
            action=request.action,
            resource=request.resource,
            requesting_identity_id=request.requesting_identity_id,
            policy_name=policy.policy_name,
            policy_version=policy.version,
            current_time=now,
            require_separation_of_duties=policy.require_separation_of_duties,
        )

        if not is_valid:
            status_map = {
                ApprovalReasonCode.APPROVAL_REJECTED_BY_OPERATOR: ApprovalStatus.REJECTED,
                ApprovalReasonCode.APPROVAL_EVIDENCE_EXPIRED: ApprovalStatus.EXPIRED,
                ApprovalReasonCode.SELF_APPROVAL_FORBIDDEN: ApprovalStatus.REJECTED,
            }
            res_status = status_map.get(rcode, ApprovalStatus.APPROVAL_REQUIRED)
            decision = ApprovalDecision(
                decision_id=decision_id,
                status=res_status,
                reason_code=rcode,
                reason=f"Attached approval evidence '{evidence.approval_id}' is invalid: {rcode.value}.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=now,
                requesting_identity_id=request.requesting_identity_id,
                target_action=request.action,
                target_resource=request.resource,
                correlation_id=request.correlation_id,
                evidence_id=evidence.approval_id,
                approver_identity_id=evidence.approver_identity_id,
            )
            self._record_audit_and_trace(request, decision)
            return decision

        decision = ApprovalDecision(
            decision_id=decision_id,
            status=ApprovalStatus.APPROVED,
            reason_code=ApprovalReasonCode.VALID_APPROVAL_ATTACHED,
            reason=f"Action '{request.action}' on resource '{request.resource}' successfully approved by '{evidence.approver_identity_id}'.",
            policy_name=policy.policy_name,
            policy_version=policy.version,
            evaluated_at=now,
            requesting_identity_id=request.requesting_identity_id,
            target_action=request.action,
            target_resource=request.resource,
            correlation_id=request.correlation_id,
            evidence_id=evidence.approval_id,
            approver_identity_id=evidence.approver_identity_id,
            expires_at=evidence.expires_at,
        )
        self._record_audit_and_trace(request, decision)
        return decision

    def grant_approval(
        self,
        approval_id: str,
        target_action: str,
        target_resource: str,
        requesting_identity_id: str,
        approver_identity_id: str,
        policy_name: str,
        policy_version: str = "1.0.0",
        ttl_seconds: Optional[int] = None,
        correlation_id: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ApprovalEvidence:
        """
        Registra una evidencia formal de aprobación (APPROVED).
        """
        now = self.clock.now()
        policy = self.policy_repository.get_policy(policy_name)
        effective_ttl = ttl_seconds if ttl_seconds is not None else (policy.default_ttl_seconds if policy else 3600)
        expires_at = now + timedelta(seconds=effective_ttl) if effective_ttl > 0 else None

        effective_correlation_id = correlation_id or f"corr-app-{uuid.uuid4().hex[:8]}"

        evidence = ApprovalEvidence(
            approval_id=approval_id,
            target_action=target_action,
            target_resource=target_resource,
            requesting_identity_id=requesting_identity_id,
            approver_identity_id=approver_identity_id,
            policy_name=policy_name,
            policy_version=policy_version,
            status=ApprovalStatus.APPROVED,
            approved_at=now,
            expires_at=expires_at,
            correlation_id=effective_correlation_id,
            metadata=metadata or {},
        )
        self.evidence_repository.save_evidence(evidence)

        if self.audit_repository:
            meta = {
                "requesting_identity_id": requesting_identity_id,
                "policy_name": policy_name,
                "policy_version": policy_version,
                "expires_at": expires_at.isoformat() if expires_at else None,
            }
            if metadata:
                meta.update(dict(metadata))

            rec = AuditRecord(
                audit_id=f"AUD-{uuid.uuid4().hex[:12]}",
                record_type=AuditRecordType.APPROVAL_GRANTED,
                occurred_at=now,
                actor=AuditActor(actor_type=AuditActorType.USER, actor_id=approver_identity_id),
                subject_type="ApprovalEvidence",
                subject_id=approval_id,
                action_or_operation=target_action,
                status="GRANTED",
                correlation_id=effective_correlation_id,
                entity_reference=f"resource:{target_resource}",
                metadata=meta,
            )
            self.audit_repository.append(rec)

        return evidence

    def reject_approval(
        self,
        approval_id: str,
        target_action: str,
        target_resource: str,
        requesting_identity_id: str,
        approver_identity_id: str,
        rejection_reason: str,
        policy_name: str,
        policy_version: str = "1.0.0",
        correlation_id: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ApprovalEvidence:
        """
        Registra un rechazo explícito de aprobación (REJECTED).
        """
        now = self.clock.now()
        effective_correlation_id = correlation_id or f"corr-app-{uuid.uuid4().hex[:8]}"

        evidence = ApprovalEvidence(
            approval_id=approval_id,
            target_action=target_action,
            target_resource=target_resource,
            requesting_identity_id=requesting_identity_id,
            approver_identity_id=approver_identity_id,
            policy_name=policy_name,
            policy_version=policy_version,
            status=ApprovalStatus.REJECTED,
            approved_at=now,
            expires_at=None,
            rejection_reason=rejection_reason,
            correlation_id=effective_correlation_id,
            metadata=metadata or {},
        )
        self.evidence_repository.save_evidence(evidence)

        if self.audit_repository:
            rec = AuditRecord(
                audit_id=f"AUD-{uuid.uuid4().hex[:12]}",
                record_type=AuditRecordType.APPROVAL_REJECTED,
                occurred_at=now,
                actor=AuditActor(actor_type=AuditActorType.USER, actor_id=approver_identity_id),
                subject_type="ApprovalEvidence",
                subject_id=approval_id,
                action_or_operation=target_action,
                status="REJECTED",
                correlation_id=effective_correlation_id,
                entity_reference=f"resource:{target_resource}",
                metadata={
                    "requesting_identity_id": requesting_identity_id,
                    "policy_name": policy_name,
                    "policy_version": policy_version,
                    "rejection_reason": rejection_reason,
                }
            )
            self.audit_repository.append(rec)

        return evidence

    def _record_audit_and_trace(self, request: ApprovalRequest, decision: ApprovalDecision) -> None:
        """Registra la evaluación de aprobación en Audit Trail K.1 y Agent Trace K.2."""
        now = decision.evaluated_at
        effective_correlation_id = request.correlation_id or decision.correlation_id or f"corr-eval-{uuid.uuid4().hex[:8]}"

        if self.audit_repository:
            rec = AuditRecord(
                audit_id=f"AUD-{uuid.uuid4().hex[:12]}",
                record_type=AuditRecordType.APPROVAL_EVALUATED,
                occurred_at=now,
                actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="approval_policy_service"),
                subject_type="ApprovalDecision",
                subject_id=decision.decision_id,
                action_or_operation=request.action,
                status=decision.status.value,
                correlation_id=effective_correlation_id,
                mission_id=request.mission_id,
                entity_reference=f"resource:{request.resource}",
                metadata={
                    "reason_code": decision.reason_code.value,
                    "reason": decision.reason,
                    "policy_name": decision.policy_name,
                    "policy_version": decision.policy_version,
                    "requesting_identity_id": decision.requesting_identity_id,
                    "evidence_id": decision.evidence_id,
                    "approver_identity_id": decision.approver_identity_id,
                    "is_executable": decision.is_executable,
                }
            )
            self.audit_repository.append(rec)

        if self.agent_trace_service:
            self.agent_trace_service.record_step(
                component_name="ApprovalPolicyService",
                execution_id=request.correlation_id or "approval_evaluation",
                step_number=1,
                step_type=StepType.SERVICE_CALL,
                operation="ApprovalPolicyService.evaluate",
                status=TraceStatus.SUCCESS if decision.is_executable else TraceStatus.FAILED,
                correlation_id=request.correlation_id,
                mission_id=request.mission_id,
                metadata={
                    "action": request.action,
                    "resource": request.resource,
                    "status": decision.status.value,
                    "reason_code": decision.reason_code.value,
                    "policy_name": decision.policy_name,
                    "is_executable": decision.is_executable,
                }
            )
