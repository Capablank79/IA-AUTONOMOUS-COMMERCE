"""
Ejecutor de acciones decorador y guardián de autorización (Hitos N.3, N.4, N.6, N.7, N.8).

Garantiza la frontera de ejecución de seguridad:
PrincipalContext
  ↓
AuthorizationRequest
  ↓
AuthorizationService.authorize(...)  [N.3 / N.4]
  ↓
[ALLOW] ──────────────────────────────────────────┐
  ↓                                              │
ToolAccessPolicyService.evaluate(...) [N.8]       │
  ↓                                              │
[ALLOW] ──────────────────────────────────────────┤
  ↓                                              │
FinancialLimitService.evaluate(...) [N.7]         │
  ↓                                              │
[WITHIN_LIMIT] ──┐                               │
  ↓              │                               │
ApprovalPolicyService.evaluate(...) [N.6]        │
  ↓                                              │
[NOT_REQUIRED / APPROVED] ─────────┐             │
  ↓                                │             │
ActionExecutor Delegate            │             │
                                   ▼             ▼
                           [DENY / BLOCKED] (0 physical calls)

Principios:
- Ninguna acción externa o financiera de alto impacto puede ejecutarse sin decisión ALLOW explícita.
- El ActionExecutor delegado nunca es invocado si la decisión no es ALLOW y (WITHIN_LIMIT o APPROVAL válida).
- Inmutable, seguro y sin fuga de credenciales o secretos.
"""

from decimal import Decimal
from typing import Dict, Any, Optional, Sequence, Mapping
from types import MappingProxyType

from src.domain.mission.models import LoopDecision, LoopState
from src.domain.mission.ports import ActionExecutor
from src.domain.authentication.models import PrincipalContext
from src.domain.authorization.models import (
    AuthorizationRequest,
    AuthorizationDecision,
    AuthorizationStatus,
    ResourceReference,
)
from src.domain.profit.models import Money
from src.domain.financial_limit.models import (
    FinancialLimitRequest,
    FinancialLimitDecision,
    FinancialLimitStatus,
    FinancialLimitType,
)
from src.domain.approval.models import ApprovalRequest, ApprovalStatus, ApprovalDecision
from src.domain.tool_policy.models import (
    ToolReference,
    ToolAccessRequest,
    ToolAccessDecision,
    ToolAccessStatus,
)
from src.domain.tool.models import ToolSideEffectLevel
from src.domain.emergency_stop.models import (
    EmergencyStopEvaluationContext,
    EmergencyStopDecision,
    EmergencyStopDecisionStatus,
    EmergencyStopScope,
)
from src.domain.emergency_stop.ports import EmergencyStopServicePort
from src.application.authorization.authorization_service import AuthorizationService
from src.application.rbac.rbac_service import RBACService
from src.application.financial_limit.financial_limit_service import FinancialLimitService
from src.application.approval.approval_policy_service import ApprovalPolicyService
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.domain.security.sensitive_data_models import (
    DataHandlingRequest,
    DataHandlingPurpose,
    DataHandlingDecision,
)
from src.domain.security.sensitive_data_ports import SensitiveDataHandlingServicePort


class AuthorizationGuardedActionExecutor(ActionExecutor):
    is_guarded_executor = True

    """
    Guardián de ejecución que impone autorización previa (Hito N.3), permisos RBAC (Hito N.4),
    políticas de herramientas (Hito N.8), manejo/redacción de datos sensibles (Hito N.9),
    límites financieros (Hito N.7), políticas de aprobación (Hito N.6) y control superior
    de parada de emergencia (Hito N.11 Emergency Stop).
    """

    def __init__(
        self,
        delegate_executor: ActionExecutor,
        authorization_service: AuthorizationService,
        principal_context: Optional[PrincipalContext] = None,
        default_allowed_actions: Sequence[str] = (),
        default_prohibited_actions: Sequence[str] = (),
        rbac_service: Optional[RBACService] = None,
        financial_limit_service: Optional[FinancialLimitService] = None,
        default_financial_policy_name: Optional[str] = None,
        approval_service: Optional[ApprovalPolicyService] = None,
        default_approval_policy_name: str = "default_commercial_approval_policy",
        tool_policy_service: Optional[ToolAccessPolicyService] = None,
        default_tool_policy_name: Optional[str] = None,
        sensitive_data_service: Optional[SensitiveDataHandlingServicePort] = None,
        default_sensitive_data_policy_name: Optional[str] = None,
        emergency_stop_service: Optional[EmergencyStopServicePort] = None,
    ):
        if delegate_executor is None:
            raise ValueError("delegate_executor cannot be None")
        if authorization_service is None:
            raise ValueError("authorization_service cannot be None")

        self.delegate_executor = delegate_executor
        self.authorization_service = authorization_service
        self.principal_context = principal_context
        self.default_allowed_actions = tuple(default_allowed_actions)
        self.default_prohibited_actions = tuple(default_prohibited_actions)
        self.rbac_service = rbac_service
        self.financial_limit_service = financial_limit_service
        self.default_financial_policy_name = default_financial_policy_name
        self.approval_service = approval_service
        self.default_approval_policy_name = default_approval_policy_name
        self.tool_policy_service = tool_policy_service
        self.default_tool_policy_name = default_tool_policy_name
        self.sensitive_data_service = sensitive_data_service
        self.default_sensitive_data_policy_name = default_sensitive_data_policy_name
        self.emergency_stop_service = emergency_stop_service
        self._latest_decision: Optional[AuthorizationDecision] = None
        self._latest_financial_decision: Optional[FinancialLimitDecision] = None
        self._latest_approval_decision: Optional[ApprovalDecision] = None
        self._latest_tool_decision: Optional[ToolAccessDecision] = None
        self._latest_sensitive_data_decision: Optional[DataHandlingDecision] = None
        self._latest_emergency_stop_decision: Optional[EmergencyStopDecision] = None

    @property
    def latest_decision(self) -> Optional[AuthorizationDecision]:
        return self._latest_decision

    @property
    def latest_financial_decision(self) -> Optional[FinancialLimitDecision]:
        return self._latest_financial_decision

    @property
    def latest_approval_decision(self) -> Optional[ApprovalDecision]:
        return self._latest_approval_decision

    @property
    def latest_tool_decision(self) -> Optional[ToolAccessDecision]:
        return self._latest_tool_decision

    @property
    def latest_sensitive_data_decision(self) -> Optional[DataHandlingDecision]:
        return self._latest_sensitive_data_decision

    @property
    def latest_emergency_stop_decision(self) -> Optional[EmergencyStopDecision]:
        return self._latest_emergency_stop_decision

    @property
    def external_calls_count(self) -> int:
        if hasattr(self.delegate_executor, "external_calls_count"):
            return getattr(self.delegate_executor, "external_calls_count")
        return 0

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        """
        Evalúa la autorización, políticas de herramientas, límites financieros y aprobación antes de delegar su ejecución.
        """
        params = dict(decision.parameters) if decision.parameters else {}
        action_type = params.get("action_type") or (
            decision.action.value if hasattr(decision.action, "value") else str(decision.action)
        )
        target_resource = params.get("target_resource") or decision.target

        # 1. Construir petición de autorización (N.3)
        authz_req = AuthorizationRequest(
            action=action_type,
            principal_context=self.principal_context,
            resource=target_resource,
            commercial_context=params,
            correlation_id=state.mission_id or "",
        )

        # 1b. Resolver permisos efectivos RBAC (N.4) si se inyectó RBACService
        allowed_actions = self.default_allowed_actions
        if self.rbac_service is not None:
            if self.principal_context is not None:
                rbac_result = self.rbac_service.resolve_effective_permissions(
                    principal_or_identity=self.principal_context,
                    scope=params.get("scope"),
                    correlation_id=state.mission_id or "",
                )
                allowed_actions = tuple(set(self.default_allowed_actions) | set(rbac_result.actions))
            else:
                allowed_actions = self.default_allowed_actions

        # 2. Evaluar autorización N.3
        authz_decision = self.authorization_service.authorize(
            request=authz_req,
            allowed_actions_override=allowed_actions,
            prohibited_actions_override=self.default_prohibited_actions,
        )
        self._latest_decision = authz_decision

        # 3. Aplicar frontera estricta N.3: sólo ALLOW puede continuar
        if not authz_decision.is_allowed:
            return {
                "status": f"AUTHORIZATION_{authz_decision.status.value}",
                "action_executed": action_type,
                "is_allowed": False,
                "authorization_status": authz_decision.status.value,
                "reason_codes": list(authz_decision.reason_codes),
                "reasons": list(authz_decision.reasons),
                "decision_id": authz_decision.decision_id,
                "correlation_id": authz_decision.correlation_id,
                "identity_id": authz_decision.identity_id,
            }

        # 3a. Evaluar N.8 Tool Allowlist / Denylist si se inyectó ToolAccessPolicyService y hay contexto de herramienta
        if self.tool_policy_service is not None:
            tool_id = params.get("tool_id") or params.get("tool")
            if tool_id:
                provider = params.get("provider")
                operation_id = params.get("operation_id") or params.get("operation")
                side_effect_raw = params.get("side_effect_level", ToolSideEffectLevel.READ_ONLY)
                if isinstance(side_effect_raw, str):
                    try:
                        side_effect_lvl = ToolSideEffectLevel(side_effect_raw)
                    except ValueError:
                        side_effect_lvl = ToolSideEffectLevel.READ_ONLY
                else:
                    side_effect_lvl = side_effect_raw

                tool_ref = ToolReference(
                    tool_id=str(tool_id),
                    provider=str(provider) if provider else None,
                    operation_id=str(operation_id) if operation_id else None,
                    side_effect_level=side_effect_lvl,
                )

                tool_policy_name = params.get("tool_policy_name") or self.default_tool_policy_name
                requester_id = (
                    self.principal_context.identity_id
                    if self.principal_context
                    else "anonymous"
                )
                role_val = (
                    params.get("role")
                    or (getattr(self.principal_context, "roles", (None,))[0] if self.principal_context and hasattr(self.principal_context, "roles") and self.principal_context.roles else None)
                    or (self.principal_context.principal.identity_type.value if self.principal_context and hasattr(self.principal_context, "principal") and hasattr(self.principal_context.principal, "identity_type") else None)
                )

                tool_req = ToolAccessRequest(
                    tool_reference=tool_ref,
                    request_id=f"treq_{state.mission_id or 'anon'}_{action_type}",
                    identity_id=requester_id,
                    role=role_val,
                    scope=params.get("scope"),
                    account_id=params.get("account_id"),
                    mission_id=state.mission_id,
                    policy_name=tool_policy_name,
                    metadata=params,
                )

                tool_decision = self.tool_policy_service.evaluate(tool_req)
                self._latest_tool_decision = tool_decision

                if not tool_decision.is_allowed:
                    return {
                        "status": f"TOOL_{tool_decision.status.value}",
                        "action_executed": action_type,
                        "is_allowed": True,
                        "is_executable": False,
                        "tool_access_status": tool_decision.status.value,
                        "tool_reason_code": tool_decision.reason_code.value,
                        "reason": tool_decision.reason_details,
                        "tool_canonical_id": tool_decision.tool_reference.canonical_id,
                        "policy_name": tool_decision.policy_name,
                        "policy_version": tool_decision.policy_version,
                        "authorization_decision_id": authz_decision.decision_id,
                        "tool_decision_checksum": tool_decision.checksum,
                    }

        # 3b. Evaluar N.7 Financial Limits si se inyectó FinancialLimitService
        financial_requires_approval = False
        if self.financial_limit_service is not None:
            money_obj = params.get("money")
            raw_amount = params.get("amount") or params.get("price") or params.get("refund_amount")
            currency = params.get("currency") or (money_obj.currency if isinstance(money_obj, Money) else "USD")

            if isinstance(money_obj, Money):
                money_to_eval = money_obj
            elif raw_amount is not None:
                amount_decimal = Decimal(str(raw_amount)) if not isinstance(raw_amount, Decimal) else raw_amount
                money_to_eval = Money(amount=amount_decimal, currency=currency)
            else:
                # Si la acción tiene contexto financiero explícito o la política está configurada
                money_to_eval = None

            if money_to_eval is not None:
                requester_id = (
                    self.principal_context.identity_id
                    if self.principal_context
                    else "anonymous"
                )
                fin_policy_name = params.get("financial_policy_name") or self.default_financial_policy_name
                fin_req = FinancialLimitRequest(
                    action=action_type,
                    money=money_to_eval,
                    resource=str(target_resource or "global_scope"),
                    resource_type=params.get("resource_type", ""),
                    identity_id=requester_id,
                    account_id=params.get("account_id", ""),
                    limit_type=params.get("limit_type"),
                    policy_name=fin_policy_name,
                    correlation_id=state.mission_id or "",
                    allow_zero=params.get("allow_zero", False),
                    context=params,
                )
                fin_decision = self.financial_limit_service.evaluate(fin_req)
                self._latest_financial_decision = fin_decision

                # Si está bloqueado estrictamente o hubo error / desconocido
                if fin_decision.is_blocked:
                    return {
                        "status": f"FINANCIAL_{fin_decision.status.value}",
                        "action_executed": action_type,
                        "is_allowed": True,
                        "is_executable": False,
                        "financial_status": fin_decision.status.value,
                        "financial_reason_code": fin_decision.reason_code.value,
                        "reason": fin_decision.reason,
                        "amount": str(fin_decision.amount),
                        "currency": fin_decision.currency,
                        "limit_value": str(fin_decision.limit_value) if fin_decision.limit_value is not None else None,
                        "policy_name": fin_decision.policy_name,
                        "policy_version": fin_decision.policy_version,
                        "authorization_decision_id": authz_decision.decision_id,
                        "financial_decision_id": fin_decision.decision_id,
                        "correlation_id": fin_decision.correlation_id,
                    }

                # Si excedió el límite pero la regla permite override con aprobación
                if fin_decision.requires_approval:
                    financial_requires_approval = True

        # 3c. Evaluar N.6 Approval Policies si se inyectó ApprovalPolicyService
        if self.approval_service is not None:
            requester_id = (
                self.principal_context.identity_id
                if self.principal_context
                else "anonymous"
            )
            policy_name = params.get("approval_policy_name") or self.default_approval_policy_name
            evidence_id = params.get("attached_evidence_id") or params.get("approval_id")

            approval_req = ApprovalRequest(
                action=action_type,
                resource=str(target_resource or "global_scope"),
                requesting_identity_id=requester_id,
                policy_name=policy_name,
                correlation_id=state.mission_id or "",
                is_external_impact=params.get("is_external_impact", False),
                is_irreversible=params.get("is_irreversible", False),
                attached_evidence_id=evidence_id,
                context=params,
            )
            approval_decision = self.approval_service.evaluate_request(approval_req)
            self._latest_approval_decision = approval_decision

            # Si N.7 requirió aprobación pero N.6 no tiene evidencia aprobada
            if financial_requires_approval and approval_decision.status != ApprovalStatus.APPROVED:
                return {
                    "status": "APPROVAL_REQUIRED",
                    "action_executed": action_type,
                    "is_allowed": True,
                    "is_executable": False,
                    "requires_approval": True,
                    "financial_limit_exceeded": True,
                    "approval_status": approval_decision.status.value,
                    "reason": "Financial limit exceeded and required approval was not provided.",
                    "authorization_decision_id": authz_decision.decision_id,
                    "financial_decision_id": self._latest_financial_decision.decision_id if self._latest_financial_decision else None,
                    "approval_decision_id": approval_decision.decision_id,
                    "correlation_id": approval_decision.correlation_id,
                }

            if not approval_decision.is_executable:
                return {
                    "status": f"APPROVAL_{approval_decision.status.value}",
                    "action_executed": action_type,
                    "is_allowed": True,
                    "is_executable": False,
                    "requires_approval": approval_decision.status == ApprovalStatus.APPROVAL_REQUIRED,
                    "approval_status": approval_decision.status.value,
                    "approval_reason_code": approval_decision.reason_code.value,
                    "reason": approval_decision.reason,
                    "policy_name": approval_decision.policy_name,
                    "policy_version": approval_decision.policy_version,
                    "authorization_decision_id": authz_decision.decision_id,
                    "approval_decision_id": approval_decision.decision_id,
                    "correlation_id": approval_decision.correlation_id,
                }

        elif financial_requires_approval:
            # Si N.7 requiere aprobación pero no hay servicio de aprobación configurado
            return {
                "status": "FINANCIAL_APPROVAL_REQUIRED",
                "action_executed": action_type,
                "is_allowed": True,
                "is_executable": False,
                "requires_approval": True,
                "reason": "Financial limit exceeded requiring approval, but no ApprovalService is present to verify evidence.",
                "authorization_decision_id": authz_decision.decision_id,
                "financial_decision_id": self._latest_financial_decision.decision_id if self._latest_financial_decision else None,
            }

        # 3d. Evaluar N.9 Sensitive Data Handling y sanitizar/minimizar payload si se inyectó SensitiveDataHandlingService
        effective_decision = decision
        if self.sensitive_data_service is not None:
            sens_req = DataHandlingRequest(
                payload=decision.parameters,
                purpose=DataHandlingPurpose.MARKETPLACE_OPERATION if "mercadolibre" in str(target_resource or "").lower() else DataHandlingPurpose.GENERAL,
                destination=str(target_resource or "delegate_executor"),
                policy_name=params.get("sensitive_data_policy_name") or self.default_sensitive_data_policy_name,
                correlation_id=state.mission_id or "",
                metadata=params,
            )
            sens_decision = self.sensitive_data_service.evaluate(sens_req)
            self._latest_sensitive_data_decision = sens_decision

            if not sens_decision.is_allowed_for_purpose:
                return {
                    "status": "SENSITIVE_DATA_BLOCKED",
                    "action_executed": action_type,
                    "is_allowed": True,
                    "is_executable": False,
                    "sensitive_data_reason_code": sens_decision.reason_code.value,
                    "reason": sens_decision.reason_details,
                    "authorization_decision_id": authz_decision.decision_id,
                    "sensitive_data_decision_id": sens_decision.decision_id,
                }

            # Si se redactaron o minimizaron parámetros, actualizar el LoopDecision para el delegate
            if sens_decision.redaction_result and isinstance(sens_decision.redaction_result.sanitized_payload, dict):
                effective_decision = LoopDecision(
                    action=decision.action,
                    parameters=sens_decision.redaction_result.sanitized_payload,
                    reason=decision.reason,
                    target=decision.target,
                )

        # 3e. Evaluar N.11 Emergency Stop (Control superior inmediato antes del execution boundary físico)
        if self.emergency_stop_service is not None:
            marketplace = params.get("marketplace") or ("mercadolibre" if "mercadolibre" in str(target_resource or "").lower() else None)
            account_id = params.get("account_id")
            tool_name = params.get("tool_name") or params.get("tool")
            is_read_only = bool(params.get("is_read_only", False))
            is_external_side_effect = bool(params.get("is_external_side_effect", True))

            estop_ctx = EmergencyStopEvaluationContext(
                action_name=action_type,
                target_resource=str(target_resource or ""),
                marketplace=marketplace,
                account_id=account_id,
                mission_id=state.mission_id,
                tool_name=tool_name,
                action_type=action_type,
                is_read_only=is_read_only,
                is_external_side_effect=is_external_side_effect,
                correlation_id=state.mission_id or "",
                metadata=params,
            )
            estop_decision = self.emergency_stop_service.evaluate(estop_ctx)
            self._latest_emergency_stop_decision = estop_decision

            if not estop_decision.is_executable:
                return {
                    "status": "EMERGENCY_STOP_BLOCKED",
                    "action_executed": action_type,
                    "is_allowed": True,
                    "is_executable": False,
                    "emergency_stop_status": estop_decision.decision_status.value,
                    "emergency_stop_reason_code": estop_decision.reason_code.value,
                    "reason": estop_decision.reason_details,
                    "applied_scope": estop_decision.applied_scope.value if estop_decision.applied_scope else None,
                    "applied_target_id": estop_decision.applied_target_id,
                    "emergency_stop_decision_id": estop_decision.decision_id,
                    "active_record_ids": list(estop_decision.active_record_ids),
                    "authorization_decision_id": authz_decision.decision_id,
                }

        # 4. Caso ALLOW, WITHIN_LIMIT, APPROVAL OK, DATA_HANDLING OK y EMERGENCY_STOP OK: Ejecutar acción a través del delegate
        result = self.delegate_executor.execute(effective_decision, state)
        if isinstance(result, dict):
            enriched = dict(result)
            enriched["is_allowed"] = True
            enriched["is_executable"] = True
            enriched["authorization_decision_id"] = authz_decision.decision_id
            enriched["authorization_status"] = authz_decision.status.value
            if self._latest_tool_decision:
                enriched["tool_decision_id"] = self._latest_tool_decision.request_id
                enriched["tool_access_status"] = self._latest_tool_decision.status.value
            if self._latest_financial_decision:
                enriched["financial_decision_id"] = self._latest_financial_decision.decision_id
                enriched["financial_status"] = self._latest_financial_decision.status.value
            if self._latest_approval_decision:
                enriched["approval_decision_id"] = self._latest_approval_decision.decision_id
                enriched["approval_status"] = self._latest_approval_decision.status.value
            if self._latest_sensitive_data_decision:
                enriched["sensitive_data_decision_id"] = self._latest_sensitive_data_decision.decision_id
            if self._latest_emergency_stop_decision:
                enriched["emergency_stop_decision_id"] = self._latest_emergency_stop_decision.decision_id
                enriched["emergency_stop_status"] = self._latest_emergency_stop_decision.decision_status.value
            return enriched

        return {
            "status": "SUCCESS",
            "is_allowed": True,
            "is_executable": True,
            "action_executed": action_type,
            "authorization_decision_id": authz_decision.decision_id,
            "authorization_status": authz_decision.status.value,
            "tool_decision_id": self._latest_tool_decision.request_id if self._latest_tool_decision else None,
            "financial_decision_id": self._latest_financial_decision.decision_id if self._latest_financial_decision else None,
            "approval_decision_id": self._latest_approval_decision.decision_id if self._latest_approval_decision else None,
            "sensitive_data_decision_id": self._latest_sensitive_data_decision.decision_id if self._latest_sensitive_data_decision else None,
            "emergency_stop_decision_id": self._latest_emergency_stop_decision.decision_id if self._latest_emergency_stop_decision else None,
            "raw_result": result,
        }
