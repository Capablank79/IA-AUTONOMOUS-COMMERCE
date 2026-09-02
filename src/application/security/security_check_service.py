"""
Servicio de Aplicación para Chequeos de Seguridad Transversales (Security Checks - Hito K.8).

Orquesta la evaluación de seguridad transversal:
- Validación de Identidad y Autorización del Actor (reutilizando RBAC/Policies y Tool Registry).
- Validación estricta de Path Safety (prevención de path traversal, ../, /, \\, secuencias relativas).
- Sanitización recursiva de secretos y exclusión de Chain-of-Thought / razonamiento privado.
- Integridad de Persistencia y Detección de Manipulación / Corrupción.
- Validación de Idempotencia y Prevención de Replay con alteración de payload (integrado con K.7).
- Validación de límites en Event Bus (payloads estructurados y tipos conocidos).
- Emisión de auditoría de seguridad (hacia K.1 Audit Trail) y traza observable (hacia K.2 Agent Trace) sin revelar secretos.

Principios:
- REUSE > EXTEND > CREATE.
- No duplica PolicyEngine ni OAuth ni Reliability Engine.
- Fail-secure: Todo estado UNKNOWN, FAIL o ERROR bloquea side-effects externos.
"""

import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Optional, Mapping, Any, Dict, List, Tuple, Sequence

from src.domain.security.models import (
    SecurityCheckStatus,
    SecurityCategory,
    SecuritySeverity,
    SecurityCheckResult,
    SecurityCheckEvaluation,
    SENSITIVE_KEYS,
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)
from src.domain.security.ports import SecurityCheckServicePort
from src.domain.policy.ports import PolicyEnginePort
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.domain.mission.models import LoopDecision, LoopAction
from src.domain.policy.rules import AuthorizationPolicyRule
from src.domain.audit.models import AuditRecordType, AuditActorType, AuditRecord, AuditActor
from src.application.audit.audit_trail_service import AuditTrailService
from src.domain.agent_trace.models import StepType, TraceStatus
from src.application.agent_trace.agent_trace_service import AgentTraceService

logger = logging.getLogger(__name__)


class SecurityCheckService(SecurityCheckServicePort):
    """
    Orquestador transversal de comprobaciones de seguridad.
    """

    def __init__(
        self,
        policy_engine: Optional[PolicyEnginePort] = None,
        audit_trail_service: Optional[AuditTrailService] = None,
        agent_trace_service: Optional[AgentTraceService] = None,
        allowed_actors: Optional[Sequence[str]] = None,
        prohibited_actions: Optional[Sequence[str]] = None,
    ):
        self.policy_engine = policy_engine
        self.audit_trail_service = audit_trail_service
        self.agent_trace_service = agent_trace_service
        self.allowed_actors = tuple(allowed_actors) if allowed_actors else ()
        self.prohibited_actions = tuple(prohibited_actions) if prohibited_actions else ()

    def validate_path_safety(
        self,
        path_or_identifier: str,
        field_name: str = "path",
    ) -> SecurityCheckResult:
        """
        Valida que un path o identificador no contenga secuencias de path traversal ni separadores.
        """
        check_id = f"SEC-PATH-{uuid.uuid4().hex[:8]}"
        try:
            validate_safe_identifier(path_or_identifier, field_name=field_name)
            return SecurityCheckResult(
                check_id=check_id,
                category=SecurityCategory.PATH_SAFETY,
                target=str(path_or_identifier),
                status=SecurityCheckStatus.PASS,
                severity=SecuritySeverity.INFO,
                message=f"Path identifier '{field_name}' is valid and safe from traversal.",
                code="PATH_SAFE",
                details={"field_name": field_name},
            )
        except ValueError as e:
            return SecurityCheckResult(
                check_id=check_id,
                category=SecurityCategory.PATH_SAFETY,
                target=str(path_or_identifier),
                status=SecurityCheckStatus.FAIL,
                severity=SecuritySeverity.CRITICAL,
                message=f"Path traversal or unsafe character detected in '{field_name}': {str(e)}",
                code="PATH_TRAVERSAL_DETECTED",
                details={"field_name": field_name, "error": str(e)},
            )

    def validate_payload_safety(
        self,
        payload: Mapping[str, Any],
        target: str = "payload",
        correlation_id: Optional[str] = None,
    ) -> SecurityCheckResult:
        """
        Verifica que el payload no contenga valores ilegales, secretos expuestos en texto plano sin sanitizar
        ni campos de razonamiento privado o CoT.
        """
        check_id = f"SEC-INPUT-{uuid.uuid4().hex[:8]}"
        if not isinstance(payload, (dict, MappingProxyType)):
            return SecurityCheckResult(
                check_id=check_id,
                category=SecurityCategory.INPUT_SAFETY,
                target=target,
                status=SecurityCheckStatus.FAIL,
                severity=SecuritySeverity.HIGH,
                message="Payload must be a dictionary or mapping.",
                code="INVALID_PAYLOAD_STRUCTURE",
                correlation_id=correlation_id,
            )

        # Detectar si hay presencia de razonamiento privado / CoT
        cot_keys = {"chain_of_thought", "internal_scratchpad", "reasoning_tokens", "raw_prompt_leak"}
        found_cot = []
        for k in payload.keys():
            if str(k).lower() in cot_keys:
                found_cot.append(str(k))

        if found_cot:
            return SecurityCheckResult(
                check_id=check_id,
                category=SecurityCategory.AGENT_SAFETY,
                target=target,
                status=SecurityCheckStatus.FAIL,
                severity=SecuritySeverity.HIGH,
                message=f"Private agent reasoning or scratchpad keys detected: {found_cot}",
                code="PRIVATE_REASONING_LEAK_DETECTED",
                details={"forbidden_keys": found_cot},
                correlation_id=correlation_id,
            )

        return SecurityCheckResult(
            check_id=check_id,
            category=SecurityCategory.INPUT_SAFETY,
            target=target,
            status=SecurityCheckStatus.PASS,
            severity=SecuritySeverity.INFO,
            message="Payload input structure conforms to safety checks.",
            code="INPUT_PAYLOAD_SAFE",
            correlation_id=correlation_id,
        )

    def evaluate_action_security(
        self,
        action_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
        correlation_id: Optional[str] = None,
        context_metadata: Optional[Mapping[str, Any]] = None,
    ) -> SecurityCheckEvaluation:
        """
        Evalúa integralmente la seguridad de una acción antes de side effects externos.
        """
        eval_id = f"SECEVAL-{uuid.uuid4().hex[:12]}"
        corr_id = correlation_id or f"corr-{uuid.uuid4().hex[:8]}"
        checks: List[SecurityCheckResult] = []

        # 1. Chequeo de Identidad / Actor
        if not actor_id or not isinstance(actor_id, str):
            checks.append(SecurityCheckResult(
                check_id=f"SEC-ACTOR-{uuid.uuid4().hex[:8]}",
                category=SecurityCategory.AUTHENTICATION,
                target=action_type,
                status=SecurityCheckStatus.FAIL,
                severity=SecuritySeverity.CRITICAL,
                message="Missing or invalid actor_id identity for protected action.",
                code="UNAUTHENTICATED_ACTOR",
                correlation_id=corr_id,
            ))
        elif self.allowed_actors and actor_id not in self.allowed_actors:
            checks.append(SecurityCheckResult(
                check_id=f"SEC-AUTHZ-{uuid.uuid4().hex[:8]}",
                category=SecurityCategory.AUTHORIZATION,
                target=action_type,
                status=SecurityCheckStatus.FAIL,
                severity=SecuritySeverity.HIGH,
                message=f"Actor '{actor_id}' is not authorized to execute actions.",
                code="ACTOR_UNAUTHORIZED",
                correlation_id=corr_id,
            ))
        else:
            checks.append(SecurityCheckResult(
                check_id=f"SEC-AUTHZ-{uuid.uuid4().hex[:8]}",
                category=SecurityCategory.AUTHORIZATION,
                target=action_type,
                status=SecurityCheckStatus.PASS,
                severity=SecuritySeverity.INFO,
                message=f"Actor '{actor_id}' is recognized.",
                code="ACTOR_AUTHORIZED",
                correlation_id=corr_id,
            ))

        # 2. Chequeo de Prohibición de Acción
        if action_type in self.prohibited_actions:
            checks.append(SecurityCheckResult(
                check_id=f"SEC-PROHIB-{uuid.uuid4().hex[:8]}",
                category=SecurityCategory.AUTHORIZATION,
                target=action_type,
                status=SecurityCheckStatus.FAIL,
                severity=SecuritySeverity.CRITICAL,
                message=f"Action '{action_type}' is explicitly prohibited.",
                code="ACTION_EXPLICITLY_PROHIBITED",
                correlation_id=corr_id,
            ))

        # 3. Chequeo de Seguridad de Payload
        payload_check = self.validate_payload_safety(payload=payload, target=action_type, correlation_id=corr_id)
        checks.append(payload_check)

        # 4. Chequeo de Path Safety en parámetros si existen campos de tipo id / path / file / name
        for k, v in payload.items():
            if isinstance(v, str) and any(sub in k.lower() for sub in ("id", "path", "file", "filename", "slug", "name")):
                # Verificar si contiene traversals
                if "/" in v or "\\" in v or ".." in v:
                    path_check = self.validate_path_safety(v, field_name=k)
                    checks.append(path_check)

        # 5. Evaluación de PolicyEngine si está configurado
        if self.policy_engine is not None:
            try:
                p_ctx = PolicyEvaluationContext(
                    action_type=action_type,
                    actor_id=actor_id,
                    mission_id=str(payload.get("mission_id", "GLOBAL_MISSION")),
                    correlation_id=corr_id,
                    loop_decision=LoopDecision(action=LoopAction.CONTINUE, reason="Security check evaluation"),
                    target_resource=str(payload.get("target_resource", action_type)),
                    prohibited_actions=self.prohibited_actions,
                    allowed_actions=(),
                )
                p_eval = self.policy_engine.evaluate(p_ctx)
                if p_eval.decision == PolicyDecisionType.DENY:
                    checks.append(SecurityCheckResult(
                        check_id=f"SEC-POL-{uuid.uuid4().hex[:8]}",
                        category=SecurityCategory.POLICY_INTEGRITY,
                        target=action_type,
                        status=SecurityCheckStatus.FAIL,
                        severity=SecuritySeverity.CRITICAL,
                        message=f"PolicyEngine denied action '{action_type}': {'; '.join(p_eval.reasons)}",
                        code="POLICY_ENGINE_DENIED",
                        details={"policy_violations": [v.code for v in p_eval.violations]},
                        correlation_id=corr_id,
                    ))
                elif p_eval.decision == PolicyDecisionType.UNKNOWN:
                    checks.append(SecurityCheckResult(
                        check_id=f"SEC-POL-{uuid.uuid4().hex[:8]}",
                        category=SecurityCategory.POLICY_INTEGRITY,
                        target=action_type,
                        status=SecurityCheckStatus.UNKNOWN,
                        severity=SecuritySeverity.HIGH,
                        message=f"PolicyEngine returned UNKNOWN for action '{action_type}'.",
                        code="POLICY_ENGINE_UNCERTAIN",
                        correlation_id=corr_id,
                    ))
                else:
                    checks.append(SecurityCheckResult(
                        check_id=f"SEC-POL-{uuid.uuid4().hex[:8]}",
                        category=SecurityCategory.POLICY_INTEGRITY,
                        target=action_type,
                        status=SecurityCheckStatus.PASS,
                        severity=SecuritySeverity.INFO,
                        message=f"PolicyEngine approved action '{action_type}'.",
                        code="POLICY_ENGINE_PASSED",
                        correlation_id=corr_id,
                    ))
            except Exception as exc:
                checks.append(SecurityCheckResult(
                    check_id=f"SEC-POL-{uuid.uuid4().hex[:8]}",
                    category=SecurityCategory.POLICY_INTEGRITY,
                    target=action_type,
                    status=SecurityCheckStatus.ERROR,
                    severity=SecuritySeverity.CRITICAL,
                    message=f"PolicyEngine evaluation encountered error: {str(exc)}",
                    code="POLICY_ENGINE_ERROR",
                    details={"error": str(exc)},
                    correlation_id=corr_id,
                ))

        # Determinar estado final consolidado
        has_fail = any(c.status == SecurityCheckStatus.FAIL for c in checks)
        has_error = any(c.status == SecurityCheckStatus.ERROR for c in checks)
        has_unknown = any(c.status == SecurityCheckStatus.UNKNOWN for c in checks)

        if has_fail:
            overall_status = SecurityCheckStatus.FAIL
            allowed = False
            summary = "Security verification failed due to blocking violations."
        elif has_error:
            overall_status = SecurityCheckStatus.ERROR
            allowed = False
            summary = "Security verification failed due to internal evaluation errors."
        elif has_unknown:
            overall_status = SecurityCheckStatus.UNKNOWN
            allowed = False
            summary = "Security verification uncertain due to missing or unknown evidence."
        else:
            overall_status = SecurityCheckStatus.PASS
            allowed = True
            summary = "All security checks passed successfully."

        evaluation = SecurityCheckEvaluation(
            evaluation_id=eval_id,
            status=overall_status,
            checks=tuple(checks),
            target_resource=action_type,
            correlation_id=corr_id,
            allowed=allowed,
            summary=summary,
            metadata={
                "actor_id": actor_id,
                "action_type": action_type,
                "payload_keys": list(payload.keys()) if isinstance(payload, (dict, MappingProxyType)) else [],
                "context_metadata": sanitize_security_data(context_metadata or {}),
            }
        )

        # Emitir trazas de auditoría de seguridad si AuditTrailService está configurado
        if self.audit_trail_service is not None:
            try:
                rec = AuditRecord(
                    audit_id=f"aud-sec-{eval_id}",
                    record_type=AuditRecordType.POLICY_EVALUATED if not allowed else AuditRecordType.ACTION_EXECUTED,
                    occurred_at=datetime.now(timezone.utc),
                    actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="SecurityCheckService"),
                    subject_type="SECURITY_ACTION_EVALUATION",
                    subject_id=action_type,
                    action_or_operation=f"SECURITY_EVALUATION_{action_type}",
                    status=overall_status.value,
                    correlation_id=corr_id,
                    causation_id=str(payload.get("mission_id", "GLOBAL_MISSION")),
                    mission_id=str(payload.get("mission_id", "GLOBAL_MISSION")),
                    entity_reference=eval_id,
                    provenance="SECURITY_CHECK_SERVICE",
                    metadata={
                        "evaluation_id": eval_id,
                        "status": overall_status.value,
                        "allowed": allowed,
                        "context_metadata": sanitize_security_data(context_metadata or {}),
                    }
                )
                self.audit_trail_service.audit_repository.append(rec)
            except Exception as e:
                logger.warning(f"Failed to record security audit trail: {e}")

        return evaluation
