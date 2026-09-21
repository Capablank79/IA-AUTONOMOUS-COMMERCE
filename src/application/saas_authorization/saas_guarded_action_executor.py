"""
Ejecutor de acciones decorador y guardián de autorización SaaS (Hitos O.4, O.3, O.1, O.2, N.3, N.4, N.8, N.7, N.6).

Garantiza la frontera de ejecución de seguridad SaaS:
SaaSAuthorizationRequest (O.4)
  ↓
SaaSAuthorizationService.authorize(...) [O.4: Session O.3 -> Tenant O.1 -> Org O.2 -> RBAC N.4 -> Authz N.3]
  ↓
[ALLOW] ──────────────────────────────────────────┐
  ↓                                              │
ToolAccessPolicyService.evaluate(...) [N.8]       │
  ↓                                              │
FinancialLimitService.evaluate(...) [N.7]         │
  ↓                                              │
ApprovalPolicyService.evaluate(...) [N.6]        │
  ↓                                              │
ActionExecutor Delegate                          │
                                                 ▼
                         [DENY / BLOCKED] (0 physical calls)

Principios:
- Ninguna acción externa sensible se ejecuta sin decisión ALLOW de O.4 SaaSAuthorizationService.
- En caso de fallo (DENY, UNKNOWN, ERROR), 0 llamadas físicas al delegado.
"""

from typing import Dict, Any, Optional, Sequence, Mapping
from types import MappingProxyType

from src.domain.mission.models import LoopDecision, LoopState
from src.domain.mission.ports import ActionExecutor
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
    SaaSAuthorizationDeniedError,
)
from src.domain.saas_authorization.ports import SaaSAuthorizationServicePort
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
from src.application.financial_limit.financial_limit_service import FinancialLimitService
from src.application.approval.approval_policy_service import ApprovalPolicyService
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.domain.security.sensitive_data_models import (
    DataHandlingRequest,
    DataHandlingPurpose,
    DataHandlingDecision,
)
from src.domain.security.sensitive_data_ports import SensitiveDataHandlingServicePort


class SaaSGuardedActionExecutor(ActionExecutor):
    is_guarded_executor = True

    """
    Decorador guardián para ActionExecutor que interpone autorización SaaS multi-tenant O.4
    antes de la ejecución física de acciones.
    """

    def __init__(
        self,
        delegate: ActionExecutor,
        saas_authorization_service: SaaSAuthorizationServicePort,
        tool_access_policy_service: Optional[ToolAccessPolicyService] = None,
        financial_limit_service: Optional[FinancialLimitService] = None,
        approval_policy_service: Optional[ApprovalPolicyService] = None,
        emergency_stop_service: Optional[EmergencyStopServicePort] = None,
        sensitive_data_service: Optional[SensitiveDataHandlingServicePort] = None,
    ):
        self.delegate = delegate
        self.saas_authorization_service = saas_authorization_service
        self.tool_access_policy_service = tool_access_policy_service
        self.financial_limit_service = financial_limit_service
        self.approval_policy_service = approval_policy_service
        self.emergency_stop_service = emergency_stop_service
        self.sensitive_data_service = sensitive_data_service

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        """
        Interpone la guardia de autorización SaaS O.4.
        Si la autorización es denegada o no es ALLOW, aborta la ejecución con 0 llamadas al delegado.
        """
        # Determinar acción efectiva (si viene 'action' en parameters, preferir esa acción)
        action_name = decision.parameters.get("action") or (
            decision.action.value if hasattr(decision.action, "value") else str(decision.action)
        )
        authz_req_data = decision.parameters.get("saas_authorization_request")
        corr_id = decision.parameters.get("correlation_id") or "exec_corr_id"

        # Construir SaaSAuthorizationRequest desde parameters o contexto
        if isinstance(authz_req_data, SaaSAuthorizationRequest):
            req = authz_req_data
        elif isinstance(authz_req_data, dict):
            req = SaaSAuthorizationRequest(
                action=action_name,
                session_id=authz_req_data.get("session_id"),
                session_context=authz_req_data.get("session_context"),
                session=authz_req_data.get("session"),
                tenant_id=authz_req_data.get("tenant_id"),
                organization_id=authz_req_data.get("organization_id"),
                identity_id=authz_req_data.get("identity_id"),
                resource=authz_req_data.get("resource"),
                resource_tenant_id=authz_req_data.get("resource_tenant_id"),
                marketplace_account_id=authz_req_data.get("marketplace_account_id"),
                commercial_context=decision.parameters,
                correlation_id=corr_id,
            )
        else:
            # Reconstruir request mínimo con parameters de decision
            req = SaaSAuthorizationRequest(
                action=action_name,
                session_id=decision.parameters.get("session_id"),
                tenant_id=decision.parameters.get("tenant_id"),
                organization_id=decision.parameters.get("organization_id"),
                identity_id=decision.parameters.get("identity_id"),
                resource=decision.parameters.get("resource"),
                resource_tenant_id=decision.parameters.get("resource_tenant_id"),
                marketplace_account_id=decision.parameters.get("marketplace_account_id"),
                commercial_context=decision.parameters,
                correlation_id=corr_id,
            )

        # 1. Evaluar Autorización SaaS O.4
        authz_decision: SaaSAuthorizationDecision = self.saas_authorization_service.authorize(req)

        if authz_decision.status != SaaSAuthorizationStatus.ALLOW:
            raise SaaSAuthorizationDeniedError(
                f"SaaS Authorization Denied: {authz_decision.reason_code.value} - {authz_decision.message}"
            )

        # 2. Delegar ejecución física si todas las guardias downstream se cumplen
        return self.delegate.execute(decision, state)
