"""
Servicio de Aplicación para Autorización Explícita y Determinista (Hito N.3).

Responsabilidades:
1. Validar la precondición de autenticación del principal (reutilizando N.2 PrincipalContext).
2. Construir el contexto de política de autorización (considerando actor, acción, recurso y contexto comercial).
3. Evaluar la política de autorización contra el PolicyEngine existente (Hito E.3).
4. Retornar una AuthorizationDecision estructurada, inmutable y determinista (ALLOW, DENY, UNKNOWN, ERROR).
5. Emitir registros de auditoría (K.1) y trazas de agente (K.2) completamente sanitizados.
6. Aplicar DEFAULT DENY: ninguna acción se autoriza si no existe una política o regla explícita que la permita.
7. Respetar fronteras: NO ejecuta acciones ni implementa RBAC (N.4) o flujos de aprobación humana (Gate M).
"""

from datetime import datetime, timezone
import logging
from types import MappingProxyType
from typing import Optional, Sequence, Mapping, Any, Dict, Union
import uuid

from src.domain.identity.models import (
    IdentityReference,
    IdentityType,
    identity_to_audit_actor,
)
from src.domain.authentication.models import (
    PrincipalContext,
    AuthenticationStatus,
)
from src.domain.authorization.models import (
    AuthorizationRequest,
    AuthorizationDecision,
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
    compute_authorization_checksum,
)
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyEvaluation,
)
from src.domain.mission.models import LoopDecision, LoopAction
from src.domain.policy.ports import PolicyEnginePort, PolicyAuditRepository
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import StepType, TraceStatus
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.domain.reliability.ports import ClockPort
from src.domain.security.models import sanitize_security_data

logger = logging.getLogger(__name__)


class AuthorizationService:
    """
    Servicio de Aplicación para la toma de decisiones de autorización explícita (N.3).
    """

    def __init__(
        self,
        policy_engine: Optional[PolicyEnginePort] = None,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[Union[AuditRepositoryPort, PolicyAuditRepository]] = None,
        agent_trace_service: Optional[AgentTraceService] = None,
        default_policy_version: str = "1.0.0",
        policies_by_resource: Optional[Mapping[str, Sequence[str]]] = None,
    ):
        self.policy_engine = policy_engine or PolicyEngine(rules=[AuthorizationPolicyRule()])
        self.clock = clock
        self.audit_repository = audit_repository
        self.agent_trace_service = agent_trace_service
        self.default_policy_version = default_policy_version
        # Mapeo opcional de reglas de recursos: {resource_prefix / canonical: (allowed_actions)}
        self._policies_by_resource = dict(policies_by_resource or {})

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def authorize(
        self,
        request: AuthorizationRequest,
        allowed_actions_override: Optional[Sequence[str]] = None,
        prohibited_actions_override: Optional[Sequence[str]] = None,
    ) -> AuthorizationDecision:
        """
        Evalúa si la identidad autenticada tiene autorización para ejecutar la acción sobre el recurso.
        Garantiza que toda evaluación resulte en una AuthorizationDecision estructurada.
        """
        now = self._now()
        decision_id = f"authz_dec_{uuid.uuid4().hex[:12]}"
        corr_id = request.correlation_id or str(uuid.uuid4())
        resource_str = request.target_resource_str

        # ---------------------------------------------------------------------
        # 1. PRECONDICIÓN DE AUTENTICACIÓN (Reusar N.2)
        # ---------------------------------------------------------------------
        ctx = request.principal_context

        if ctx is None:
            decision = AuthorizationDecision(
                decision_id=decision_id,
                status=AuthorizationStatus.DENY,
                identity_id=request.identity_id or "unauthenticated",
                action=request.action,
                resource=resource_str,
                matched_policy="AuthenticationPreconditionPolicy",
                policy_version=request.policy_version,
                reason_codes=(AuthorizationReasonCode.MISSING_PRINCIPAL_CONTEXT.value,),
                reasons=("Authentication context is missing; unauthenticated requests are denied.",),
                evaluated_at=now,
                correlation_id=corr_id,
                metadata={"error": "missing_principal_context"},
            )
            self._record_audit_and_trace(decision, principal=None)
            return decision

        auth_result = ctx.auth_result
        if not ctx.is_authenticated:
            status = AuthorizationStatus.DENY
            reason_code = AuthorizationReasonCode.UNAUTHENTICATED_PRINCIPAL.value

            if auth_result.status == AuthenticationStatus.EXPIRED:
                reason_code = AuthorizationReasonCode.EXPIRED_AUTHENTICATION.value
                reason_msg = "Principal authentication token/credential is expired."
            elif auth_result.status == AuthenticationStatus.INVALID:
                reason_code = AuthorizationReasonCode.INVALID_AUTHENTICATION.value
                reason_msg = "Principal authentication credentials are invalid."
            elif auth_result.status == AuthenticationStatus.UNKNOWN:
                status = AuthorizationStatus.UNKNOWN
                reason_code = AuthorizationReasonCode.UNKNOWN_AUTHENTICATION.value
                reason_msg = "Principal authentication status is unknown."
            else:
                reason_msg = f"Principal is not authenticated (status: {auth_result.status.value})."

            decision = AuthorizationDecision(
                decision_id=decision_id,
                status=status,
                identity_id=ctx.identity_id,
                action=request.action,
                resource=resource_str,
                matched_policy="AuthenticationPreconditionPolicy",
                policy_version=request.policy_version,
                reason_codes=(reason_code,),
                reasons=(reason_msg,),
                evaluated_at=now,
                correlation_id=corr_id,
                metadata={"auth_status": auth_result.status.value},
            )
            self._record_audit_and_trace(decision, principal=ctx.principal)
            return decision

        # ---------------------------------------------------------------------
        # 2. VALIDACIÓN DE CONTEXTO DE RECURSO (Resource mismatch check)
        # ---------------------------------------------------------------------
        principal = ctx.principal
        provider = ctx.provider
        if isinstance(request.resource, ResourceReference):
            # Si el recurso especifica una cuenta objetivo y provider
            if request.resource.account_id and provider:
                # Extraer subject del canonical_identifier (formato: type:provider:subject)
                parts = principal.canonical_identifier.split(":")
                principal_subject = parts[-1] if len(parts) >= 3 else principal.identity_id

                if (request.resource.provider is None or provider.lower() == request.resource.provider.lower()):
                    if principal_subject.lower() != request.resource.account_id.lower():
                        decision = AuthorizationDecision(
                            decision_id=decision_id,
                            status=AuthorizationStatus.DENY,
                            identity_id=ctx.identity_id,
                            action=request.action,
                            resource=resource_str,
                            matched_policy="ResourceBoundaryPolicy",
                            policy_version=request.policy_version,
                            reason_codes=(AuthorizationReasonCode.RESOURCE_MISMATCH.value,),
                            reasons=(f"Principal account '{principal_subject}' does not match target resource account '{request.resource.account_id}'.",),
                            evaluated_at=now,
                            correlation_id=corr_id,
                            metadata={
                                "principal_account": principal_subject,
                                "target_account": request.resource.account_id,
                            },
                        )
                        self._record_audit_and_trace(decision, principal=principal)
                        return decision

        # ---------------------------------------------------------------------
        # 3. CONSTRUCCIÓN DE LISTAS ALLOWED / PROHIBITED (Policy context)
        # ---------------------------------------------------------------------
        allowed_actions = list(allowed_actions_override) if allowed_actions_override is not None else None
        prohibited_actions = list(prohibited_actions_override or ())

        # Extraer directivas desde commercial_context si existen
        comm_ctx = dict(request.commercial_context)
        mission_id = comm_ctx.get("mission_id", corr_id)
        if allowed_actions is None and "allowed_actions" in comm_ctx and isinstance(comm_ctx["allowed_actions"], (list, tuple)):
            allowed_actions = list(comm_ctx["allowed_actions"])
        if "prohibited_actions" in comm_ctx and isinstance(comm_ctx["prohibited_actions"], (list, tuple)):
            prohibited_actions.extend(comm_ctx["prohibited_actions"])

        # Extraer directivas basadas en recurso si hay configuración registrada
        if resource_str and resource_str in self._policies_by_resource:
            res_allowed = self._policies_by_resource[resource_str]
            if allowed_actions is not None:
                allowed_actions.extend(res_allowed)

        # ---------------------------------------------------------------------
        # 4. DEFAULT DENY PRE-CHECK: Si allowed_actions está definido (e.g. vía RBAC) y la acción no está
        # ---------------------------------------------------------------------
        if allowed_actions is not None and request.action not in allowed_actions:
            decision = AuthorizationDecision(
                decision_id=decision_id,
                status=AuthorizationStatus.DENY,
                identity_id=ctx.identity_id,
                action=request.action,
                resource=resource_str,
                matched_policy="ExplicitAllowedActionsPolicy",
                policy_version=request.policy_version,
                reason_codes=(AuthorizationReasonCode.ACTION_NOT_ALLOWED.value,),
                reasons=(f"Action '{request.action}' is not in allowed actions list for identity '{ctx.identity_id}'.",),
                evaluated_at=now,
                correlation_id=corr_id,
                metadata={"allowed_actions": list(allowed_actions)},
            )
            self._record_audit_and_trace(decision, principal=principal, mission_id=mission_id)
            return decision

        effective_allowed = tuple(allowed_actions) if allowed_actions is not None else ()

        # ---------------------------------------------------------------------
        # 5. EVALUACIÓN CON POLICY ENGINE EXISTENTE (Hito E.3)
        # ---------------------------------------------------------------------
        # Sintetizar LoopDecision si no viene en el contexto
        loop_decision = comm_ctx.get("loop_decision")
        if not isinstance(loop_decision, LoopDecision):
            loop_decision = LoopDecision(
                action=LoopAction.CONTINUE,
                reason=f"Authorization request for action {request.action}",
                target=resource_str,
                parameters=MappingProxyType(comm_ctx),
            )

        mission_id = comm_ctx.get("mission_id", corr_id)

        policy_context = PolicyEvaluationContext(
            action_type=request.action,
            actor_id=ctx.identity_id,
            mission_id=mission_id,
            correlation_id=corr_id,
            loop_decision=loop_decision,
            target_resource=resource_str,
            allowed_actions=effective_allowed,
            prohibited_actions=tuple(prohibited_actions),
            custom_context=MappingProxyType(comm_ctx),
            timestamp=now,
        )

        try:
            policy_eval: PolicyEvaluation = self.policy_engine.evaluate(policy_context)
        except Exception as ex:
            logger.exception("Unexpected error during policy evaluation: %s", ex)
            decision = AuthorizationDecision(
                decision_id=decision_id,
                status=AuthorizationStatus.ERROR,
                identity_id=ctx.identity_id,
                action=request.action,
                resource=resource_str,
                matched_policy="PolicyEngineExecution",
                policy_version=request.policy_version,
                reason_codes=(AuthorizationReasonCode.POLICY_EVALUATION_ERROR.value,),
                reasons=(f"Policy evaluation raised error: {str(ex)}",),
                evaluated_at=now,
                correlation_id=corr_id,
                metadata={"exception": str(ex)},
            )
            self._record_audit_and_trace(decision, principal=principal, mission_id=mission_id)
            return decision

        # ---------------------------------------------------------------------
        # 6. MAPEADO DETERMINISTA DE PolicyDecisionType A AuthorizationStatus
        # ---------------------------------------------------------------------
        reason_codes_list = []
        for r in policy_eval.reasons:
            if "prohibited" in r.lower():
                reason_codes_list.append(AuthorizationReasonCode.ACTION_PROHIBITED.value)
            elif "not authorized" in r.lower() or "not in allowed" in r.lower():
                reason_codes_list.append(AuthorizationReasonCode.ACTION_NOT_ALLOWED.value)
            elif "authorized" in r.lower():
                reason_codes_list.append(AuthorizationReasonCode.AUTHORIZED_BY_POLICY.value)

        for v in policy_eval.violations:
            if v.code:
                reason_codes_list.append(v.code)

        if not reason_codes_list:
            if policy_eval.decision == PolicyDecisionType.ALLOW:
                reason_codes_list.append(AuthorizationReasonCode.AUTHORIZED_BY_POLICY.value)
            elif policy_eval.decision == PolicyDecisionType.DENY:
                reason_codes_list.append(AuthorizationReasonCode.POLICY_DENIED.value)
            elif policy_eval.decision == PolicyDecisionType.UNKNOWN:
                reason_codes_list.append(AuthorizationReasonCode.INSUFFICIENT_EVIDENCE.value)
            else:
                reason_codes_list.append(AuthorizationReasonCode.DEFAULT_DENY.value)

        if policy_eval.decision == PolicyDecisionType.ALLOW:
            final_status = AuthorizationStatus.ALLOW
        elif policy_eval.decision == PolicyDecisionType.DENY:
            final_status = AuthorizationStatus.DENY
        elif policy_eval.decision == PolicyDecisionType.UNKNOWN:
            final_status = AuthorizationStatus.UNKNOWN
        else:
            # REQUIRE_APPROVAL / DEFER u otros no son ALLOW directo
            final_status = AuthorizationStatus.DENY

        matched_rule = policy_eval.violations[0].rule_name if policy_eval.violations else "AuthorizationPolicyRule"

        decision = AuthorizationDecision(
            decision_id=decision_id,
            status=final_status,
            identity_id=ctx.identity_id,
            action=request.action,
            resource=resource_str,
            matched_policy=matched_rule,
            policy_version=request.policy_version,
            reason_codes=tuple(reason_codes_list),
            reasons=policy_eval.reasons or (f"Policy decision: {final_status.value}",),
            evaluated_at=now,
            correlation_id=corr_id,
            metadata={
                "policy_evaluation_id": policy_eval.evaluation_id,
                "engine_decision": policy_eval.decision.value,
            },
        )

        self._record_audit_and_trace(decision, principal=principal, mission_id=mission_id)
        return decision

    def _record_audit_and_trace(
        self,
        decision: AuthorizationDecision,
        principal: Optional[IdentityReference],
        mission_id: Optional[str] = None,
    ) -> None:
        """
        Registra la auditoría (K.1) y la traza (K.2) de la decisión de autorización de forma segura y sanitizada.
        """
        now = decision.evaluated_at

        # 1. Audit Trail (K.1)
        if self.audit_repository is not None:
            try:
                if principal is not None:
                    actor = identity_to_audit_actor(principal)
                else:
                    actor = AuditActor(
                        actor_type=AuditActorType.SYSTEM,
                        actor_id="unauthenticated_actor",
                    )

                audit_meta = {
                    "action": decision.action,
                    "resource": decision.resource,
                    "status": decision.status.value,
                    "matched_policy": decision.matched_policy,
                    "policy_version": decision.policy_version,
                    "reason_codes": list(decision.reason_codes),
                    "checksum": decision.checksum,
                }

                record = AuditRecord(
                    audit_id=f"audit_authz_{uuid.uuid4().hex[:12]}",
                    record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
                    occurred_at=now,
                    actor=actor,
                    subject_type="AUTHORIZATION_DECISION",
                    subject_id=decision.decision_id,
                    action_or_operation=decision.action,
                    status=decision.status.value,
                    correlation_id=decision.correlation_id or "authz_global",
                    mission_id=mission_id or decision.correlation_id or "",
                    provenance="AUTHORIZATION_SERVICE",
                    metadata=audit_meta,
                )

                if hasattr(self.audit_repository, "append"):
                    self.audit_repository.append(record)
                elif hasattr(self.audit_repository, "save_record"):
                    self.audit_repository.save_record(record)
                elif hasattr(self.audit_repository, "save"):
                    self.audit_repository.save(record)

                # Si el principal está presente y autenticado, registrar también evento de autenticación/identidad en auditoría
                if principal is not None:
                    auth_record = AuditRecord(
                        audit_id=f"audit_authn_{uuid.uuid4().hex[:12]}",
                        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
                        occurred_at=now,
                        actor=actor,
                        subject_type="PRINCIPAL",
                        subject_id=principal.identity_id,
                        action_or_operation="AUTHENTICATE",
                        status="AUTHENTICATED",
                        correlation_id=decision.correlation_id or "authz_global",
                        mission_id=mission_id or decision.correlation_id or "",
                        provenance="AUTHENTICATION_SERVICE",
                        metadata={"identity_id": principal.identity_id, "canonical_identifier": principal.canonical_identifier},
                    )
                    if hasattr(self.audit_repository, "append"):
                        self.audit_repository.append(auth_record)
                    elif hasattr(self.audit_repository, "save_record"):
                        self.audit_repository.save_record(auth_record)
                    elif hasattr(self.audit_repository, "save"):
                        self.audit_repository.save(auth_record)
            except Exception as e:
                logger.warning("Could not record authorization audit event: %s", e)

        # 2. Agent Trace (K.2)
        if self.agent_trace_service is not None:
            try:
                trace_status = TraceStatus.SUCCESS if decision.is_allowed else TraceStatus.FAILED
                self.agent_trace_service.record_step(
                    component_name="AuthorizationService",
                    execution_id=decision.decision_id,
                    step_number=1,
                    step_type=StepType.POLICY_EVALUATION,
                    operation=f"AUTHORIZE:{decision.action}",
                    status=trace_status,
                    started_at=now,
                    completed_at=now,
                    correlation_id=decision.correlation_id or "",
                    tool_or_service="PolicyEngine",
                    input_reference=f"action:{decision.action}|resource:{decision.resource or 'none'}",
                    output_reference=f"status:{decision.status.value}|reasons:{','.join(decision.reason_codes)}",
                    metadata={"decision_checksum": decision.checksum},
                )
            except Exception as e:
                logger.warning("Could not record authorization trace step: %s", e)
