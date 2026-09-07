"""
Application Service for N.8 — Tool Allowlist / Denylist (Transversal N — Security, Governance & Safety).

Responsabilidades:
- Recibe ToolAccessRequest.
- Aplica normalización determinista anti-bypass sobre tool_id, provider, operation_id.
- Resuelve la política de herramientas (ToolPolicy) correspondiente.
- Aplica semántica de evaluación fail-secure:
  1. Integridad de la política y checksum.
  2. Reglas explícitas DENY (Explicit DENY overrides all).
  3. Reglas explícitas ALLOW (Scoped ALLOW matching tool, provider, op, side_effects, role, scope, account, mission).
  4. Default DENY / UNKNOWN si no hay regla explícita que autorice.
- Emisión de auditoría determinista (K.1 AuditRecordType.TOOL_ACCESS_EVALUATED / TOOL_ACCESS_DENIED).
"""

from datetime import datetime, timezone
import logging
from types import MappingProxyType
from typing import Optional, Dict, Any, Sequence, Union
import uuid

from src.domain.tool_policy.models import (
    ToolAccessStatus,
    ToolAccessReasonCode,
    ToolRuleAction,
    ToolReference,
    ToolPolicyRule,
    ToolPolicy,
    ToolAccessRequest,
    ToolAccessDecision,
    normalize_identifier,
)
from src.domain.tool_policy.ports import (
    ToolPolicyRepositoryPort,
    ToolAccessPolicyServicePort,
)
from src.domain.tool.models import ToolSideEffectLevel
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.security.models import sanitize_security_data

logger = logging.getLogger(__name__)


def _match_pattern(pattern: str, value: Optional[str]) -> bool:
    """
    Evalúa coincidencia de patrones simples (* o texto exacto normalizado).
    """
    if pattern == "*":
        return True
    if value is None:
        return False
    norm_val = normalize_identifier(value)
    norm_pat = normalize_identifier(pattern)
    return norm_val == norm_pat


class ToolAccessPolicyService(ToolAccessPolicyServicePort):
    """
    Servicio de aplicación para la evaluación y gobernanza de políticas de herramientas (N.8).
    """

    def __init__(
        self,
        policy_repository: Optional[ToolPolicyRepositoryPort] = None,
        default_policy_name: str = "default_tool_access_policy",
        audit_repository: Optional[AuditRepositoryPort] = None,
        clock: Optional[Any] = None,
    ):
        self.policy_repository = policy_repository
        self.default_policy_name = default_policy_name
        self.audit_repository = audit_repository
        self.clock = clock

    def _get_current_time(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now"):
            now_val = self.clock.now()
            if now_val.tzinfo is None:
                return now_val.replace(tzinfo=timezone.utc)
            return now_val
        return datetime.now(timezone.utc)

    def evaluate(self, request: ToolAccessRequest) -> ToolAccessDecision:
        """
        Evalúa si una herramienta/operación concreta está permitida en el contexto dado.
        """
        eval_time = self._get_current_time()
        policy_name = request.policy_name or self.default_policy_name

        # 1. Validar integridad básica del request y tool_reference
        if request.tool_reference is None or not isinstance(request.tool_reference, ToolReference):
            return self._build_decision(
                request=request,
                status=ToolAccessStatus.ERROR,
                reason_code=ToolAccessReasonCode.INVALID_TOOL_REFERENCE,
                reason_details="ToolReference is missing or invalid.",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        tool_ref = request.tool_reference

        # 2. Obtener la política
        if self.policy_repository is None:
            return self._build_decision(
                request=request,
                status=ToolAccessStatus.UNKNOWN,
                reason_code=ToolAccessReasonCode.POLICY_NOT_FOUND,
                reason_details="No ToolPolicyRepository configured.",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        try:
            policy = self.policy_repository.get_policy(policy_name)
        except Exception as e:
            logger.error(f"Error loading ToolPolicy '{policy_name}': {e}")
            return self._build_decision(
                request=request,
                status=ToolAccessStatus.DENY,
                reason_code=ToolAccessReasonCode.CORRUPTED_POLICY_OR_CHECKSUM_INVALID,
                reason_details=f"ToolPolicy '{policy_name}' is corrupted or failed to load: {e}",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )
        if policy is None:
            return self._build_decision(
                request=request,
                status=ToolAccessStatus.UNKNOWN,
                reason_code=ToolAccessReasonCode.POLICY_NOT_FOUND,
                reason_details=f"ToolPolicy '{policy_name}' was not found in repository.",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        # 3. Validar integridad criptográfica de la política
        if not policy.is_valid_checksum():
            return self._build_decision(
                request=request,
                status=ToolAccessStatus.DENY,
                reason_code=ToolAccessReasonCode.CORRUPTED_POLICY_OR_CHECKSUM_INVALID,
                reason_details=f"ToolPolicy '{policy_name}' has an invalid or tampered checksum.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=eval_time,
            )

        # 4. Evaluación de reglas:
        # Precedencia:
        # a) EXPLICIT DENY matching rule -> Inmediatamente DENY
        # b) EXPLICIT ALLOW matching rule -> Si hace match completo -> ALLOW
        # c) Ninguna regla hace match -> Default Action de la política (o DEFAULT_DENY)

        # Primero: Chequear si alguna regla DENY hace match
        for rule in policy.rules:
            if rule.action == ToolRuleAction.DENY and self._rule_matches(rule, request):
                return self._build_decision(
                    request=request,
                    status=ToolAccessStatus.DENY,
                    reason_code=ToolAccessReasonCode.EXPLICIT_DENY_RULE,
                    reason_details=f"Explicit DENY rule matched: {rule.rule_id} ({rule.description})",
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                    evaluated_at=eval_time,
                    matched_rule_id=rule.rule_id,
                )

        # Segundo: Chequear si alguna regla ALLOW hace match
        matched_allow_rule: Optional[ToolPolicyRule] = None
        for rule in policy.rules:
            if rule.action == ToolRuleAction.ALLOW and self._rule_matches(rule, request):
                matched_allow_rule = rule
                break

        if matched_allow_rule is not None:
            return self._build_decision(
                request=request,
                status=ToolAccessStatus.ALLOW,
                reason_code=ToolAccessReasonCode.ALLOWED_BY_POLICY,
                reason_details=f"Explicit ALLOW rule matched: {matched_allow_rule.rule_id}",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=eval_time,
                matched_rule_id=matched_allow_rule.rule_id,
            )

        # Tercero: Si ninguna regla hizo match, aplicar Default Action de la política
        if policy.default_action == ToolRuleAction.ALLOW:
            return self._build_decision(
                request=request,
                status=ToolAccessStatus.ALLOW,
                reason_code=ToolAccessReasonCode.ALLOWED_BY_POLICY,
                reason_details="Allowed by default policy action.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=eval_time,
            )

        return self._build_decision(
            request=request,
            status=ToolAccessStatus.DENY,
            reason_code=ToolAccessReasonCode.DEFAULT_DENY_NO_RULE,
            reason_details="No matching ALLOW rule found; rejected by default DENY.",
            policy_name=policy.policy_name,
            policy_version=policy.version,
            evaluated_at=eval_time,
        )

    def _rule_matches(self, rule: ToolPolicyRule, request: ToolAccessRequest) -> bool:
        """
        Determina si una regla de política aplica a la solicitud dada.
        """
        tool_ref = request.tool_reference

        # Match tool_id pattern
        if not _match_pattern(rule.tool_id_pattern, tool_ref.tool_id):
            return False

        # Match provider pattern
        if not _match_pattern(rule.provider_pattern, tool_ref.provider):
            return False

        # Match operation pattern
        if not _match_pattern(rule.operation_pattern, tool_ref.operation_id):
            return False

        # Match side effect levels
        if rule.prohibited_side_effect_levels and tool_ref.side_effect_level in rule.prohibited_side_effect_levels:
            return False

        if rule.allowed_side_effect_levels and tool_ref.side_effect_level not in rule.allowed_side_effect_levels:
            return False

        # Para reglas DENY: si tiene denied_roles, hace match si el request tiene uno de esos roles
        if rule.action == ToolRuleAction.DENY and rule.denied_roles:
            if not request.role or request.role.upper() not in rule.denied_roles:
                return False

        # Para reglas ALLOW: si tiene denied_roles y el request tiene ese rol, la regla ALLOW no hace match
        if rule.action == ToolRuleAction.ALLOW and rule.denied_roles:
            if request.role and request.role.upper() in rule.denied_roles:
                return False

        # Match allowed roles (si la regla restringe roles, el request debe tener uno de ellos)
        if rule.allowed_roles:
            if not request.role or request.role.upper() not in rule.allowed_roles:
                return False

        # Match allowed scopes
        if rule.allowed_scopes:
            if not request.scope or request.scope.lower() not in rule.allowed_scopes:
                return False

        # Match target_account_id
        if rule.target_account_id:
            if not request.account_id or request.account_id != rule.target_account_id:
                return False

        # Match target_mission_id
        if rule.target_mission_id:
            if not request.mission_id or request.mission_id != rule.target_mission_id:
                return False

        return True

    def _build_decision(
        self,
        request: ToolAccessRequest,
        status: ToolAccessStatus,
        reason_code: ToolAccessReasonCode,
        reason_details: str,
        policy_name: str,
        policy_version: str,
        evaluated_at: datetime,
        matched_rule_id: Optional[str] = None,
    ) -> ToolAccessDecision:
        decision = ToolAccessDecision(
            request_id=request.request_id,
            status=status,
            reason_code=reason_code,
            tool_reference=request.tool_reference,
            policy_name=policy_name,
            policy_version=policy_version,
            evaluated_at=evaluated_at,
            matched_rule_id=matched_rule_id,
            reason_details=reason_details,
            metadata=request.metadata,
        )

        self._emit_audit(request, decision)
        return decision

    def _emit_audit(self, request: ToolAccessRequest, decision: ToolAccessDecision) -> None:
        if self.audit_repository is None:
            return

        try:
            actor = AuditActor(
                actor_id=request.identity_id or "system",
                actor_type=AuditActorType.AGENT if request.identity_id else AuditActorType.SYSTEM,
                details={"role": request.role} if request.role else {},
            )

            record_type = (
                AuditRecordType.TOOL_ACCESS_EVALUATED
                if decision.status == ToolAccessStatus.ALLOW
                else AuditRecordType.TOOL_ACCESS_DENIED
            )

            payload = {
                "request_id": request.request_id,
                "tool_id": request.tool_reference.tool_id,
                "provider": request.tool_reference.provider,
                "operation_id": request.tool_reference.operation_id,
                "side_effect_level": request.tool_reference.side_effect_level.value,
                "status": decision.status.value,
                "reason_code": decision.reason_code.value,
                "policy_name": decision.policy_name,
                "policy_version": decision.policy_version,
                "matched_rule_id": decision.matched_rule_id,
                "decision_checksum": decision.checksum,
            }

            audit_record = AuditRecord(
                audit_id=f"audit_{uuid.uuid4().hex[:16]}",
                record_type=record_type,
                occurred_at=decision.evaluated_at,
                actor=actor,
                subject_type="TOOL",
                subject_id=decision.tool_reference.canonical_id,
                action_or_operation=f"TOOL_ACCESS_{decision.tool_reference.canonical_id}",
                status="SUCCESS" if decision.status == ToolAccessStatus.ALLOW else "DENIED",
                correlation_id=request.request_id,
                mission_id=request.mission_id,
                metadata=payload,
            )
            if hasattr(self.audit_repository, "append"):
                self.audit_repository.append(audit_record)
            elif hasattr(self.audit_repository, "save"):
                self.audit_repository.save(audit_record)
        except Exception as e:
            logger.warning(f"Failed to emit audit record for ToolAccessDecision: {e}")
