"""
Servicio de Aplicación para Admin Console & Multi-Tenant Management (Hito O.10 — SaaS / Platformization).

Orquesta capacidades existentes de administración para:
- O.1 Tenant Isolation
- O.2 Organizations / Users
- O.3 SaaS Session
- O.4 SaaS Authorization
- O.6 Usage Metering
- O.7 Quota Management
- O.8 Plans & Pricing Tiers
- O.9 Billing & Subscription Management
- N.4 RBAC
- N.9 Sensitive Data Handling
- K.1 Audit Trail
- K.2 Agent Trace

Principios:
1. Responde a: "¿Puede un operador autorizado administrar tenants, organizations, users, plans, quotas y billing desde una consola segura sin romper el aislamiento multi-tenant?".
2. Cero duplicación de lógica de dominio (delega a domain services existentes).
3. No manipulación directa de JSON/repositorios para lógica de negocio de planes, cuotas, billing y memberships.
4. Toda operación requiere validación O.3, O.4 y permisos explícitos (READ != MANAGE).
5. Cross-tenant mismatch -> DENY inmediato.
6. Emisión de auditoría K.1 para toda mutación administrativa.
7. Vistas seguras (View Models DTO) libres de secretos, PAN/CVV, prompts y CoT.
"""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
import uuid
from typing import Optional, Sequence, Mapping, Any, Dict, Union, Tuple, List

from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.session.ports import SaaSSessionRepositoryPort, SaaSSessionServicePort
from src.domain.tenant.models import (
    TenantId,
    TenantContext,
    TenantScope,
    CrossTenantAccessError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.organization.models import (
    Organization,
    UserMembership,
    MembershipStatus,
    MembershipRole,
    OrganizationStatus,
)
from src.domain.organization.ports import (
    OrganizationRepositoryPort,
    MembershipRepositoryPort,
)
from src.application.organization.organization_service import (
    OrganizationService,
    OrganizationMembershipService,
)
from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
    normalize_action_token,
)
from src.domain.rbac.ports import RoleRepositoryPort, RoleAssignmentRepositoryPort
from src.application.rbac.rbac_service import RBACService
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
)
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.domain.usage_metering.models import (
    UsageQuery,
    UsagePeriod,
    UsagePeriodType,
    UsageAggregate,
)
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.domain.quota_management.models import (
    QuotaPolicy,
    QuotaRule,
    QuotaStatus,
)
from src.domain.quota_management.ports import (
    QuotaPolicyRepositoryPort,
    QuotaReservationRepositoryPort,
)
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.domain.plans.models import (
    Plan,
    PlanAssignment,
    PlanTier,
    PlanStatus,
)
from src.domain.plans.ports import (
    PlanCatalogRepositoryPort,
    PlanAssignmentRepositoryPort,
)
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.domain.billing.models import (
    Subscription,
    SubscriptionStatus,
    BillingCycle,
    Invoice,
    InvoiceStatus,
)
from src.domain.billing.ports import (
    SubscriptionRepositoryPort,
    InvoiceRepositoryPort,
    PaymentAttemptRepositoryPort,
)
from src.application.billing.subscription_service import SubscriptionService
from src.application.billing.billing_service import BillingService
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
)
from src.application.security.sensitive_data_handling_service import (
    SensitiveDataHandlingService,
)
from src.domain.audit.models import (
    AuditActor,
    AuditActorType,
    AuditRecord,
    AuditRecordType,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.application.audit.audit_trail_service import AuditTrailService
from src.domain.agent_trace.models import StepType, TraceStatus
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.domain.reliability.ports import ClockPort
from src.application.tenant_configuration.tenant_configuration_service import TenantConfigurationService
from src.domain.tenant_configuration.models import ConfigurationScope

from src.domain.saas_observability.ports import TenantObservabilityServicePort
from src.domain.saas_observability.models import (
    TenantOperationalSnapshot,
    OperationalAlert,
    AlertStatus,
)

from src.domain.admin_console.models import (
    AdminAction,
    AdminPermission,
    TenantAdminSummary,
    OrganizationAdminView,
    MembershipAdminView,
    UsageAdminSummary,
    QuotaAdminView,
    PlanAdminView,
    BillingInvoiceSummary,
    BillingAdminView,
    AuditAdminView,
    TraceAdminView,
    ObservabilityAdminView,
    ObservabilityAlertAdminView,
    AdminConsoleError,
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminResourceNotFoundError,
    AdminInvalidRequestError,
    mask_pii,
)

logger = logging.getLogger(__name__)


def _serialize_usage_value(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize_usage_value(asdict(value))
    if isinstance(value, Mapping):
        return {key: _serialize_usage_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_usage_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _serialize_usage_breakdown(breakdown: Mapping[str, Any]) -> Dict[str, Any]:
    """Convierte los resúmenes O.6 en una proyección JSON segura para O.10."""
    return {key: _serialize_usage_value(value) for key, value in breakdown.items()}


class AdminConsoleService:
    """
    Servicio de Aplicación para Admin Console & Multi-Tenant Management (O.10).
    """

    def __init__(
        self,
        session_repository: SaaSSessionRepositoryPort,
        authorization_service: SaaSAuthorizationService,
        organization_service: Optional[OrganizationService] = None,
        membership_service: Optional[OrganizationMembershipService] = None,
        usage_metering_service: Optional[UsageMeteringService] = None,
        quota_management_service: Optional[QuotaManagementService] = None,
        quota_policy_repository: Optional[QuotaPolicyRepositoryPort] = None,
        quota_reservation_repository: Optional[QuotaReservationRepositoryPort] = None,
        plan_entitlement_service: Optional[PlanEntitlementService] = None,
        plan_repository: Optional[PlanCatalogRepositoryPort] = None,
        plan_assignment_repository: Optional[PlanAssignmentRepositoryPort] = None,
        subscription_service: Optional[SubscriptionService] = None,
        billing_service: Optional[BillingService] = None,
        subscription_repository: Optional[SubscriptionRepositoryPort] = None,
        invoice_repository: Optional[InvoiceRepositoryPort] = None,
        rbac_service: Optional[RBACService] = None,
        sensitive_data_service: Optional[SensitiveDataHandlingService] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_repository: Optional[AgentTraceRepositoryPort] = None,
        clock: Optional[ClockPort] = None,
        tenant_configuration_service: Optional[TenantConfigurationService] = None,
        tenant_observability_service: Optional[TenantObservabilityServicePort] = None,
    ):
        self.session_repository = session_repository
        self.authorization_service = authorization_service
        self.organization_service = organization_service
        self.membership_service = membership_service
        self.usage_metering_service = usage_metering_service
        self.quota_management_service = quota_management_service
        self.quota_policy_repository = quota_policy_repository
        self.quota_reservation_repository = quota_reservation_repository
        self.plan_entitlement_service = plan_entitlement_service
        self.plan_repository = plan_repository
        self.plan_assignment_repository = plan_assignment_repository
        self.subscription_service = subscription_service
        self.billing_service = billing_service
        self.subscription_repository = subscription_repository
        self.invoice_repository = invoice_repository
        self.rbac_service = rbac_service
        self.sensitive_data_service = sensitive_data_service
        self.audit_repository = audit_repository
        self.trace_repository = trace_repository
        self.clock = clock
        self.tenant_configuration_service = tenant_configuration_service
        self.tenant_observability_service = tenant_observability_service

    def _now(self) -> datetime:
        if self.clock:
            now_dt = self.clock.now()
            if now_dt.tzinfo is None:
                return now_dt.replace(tzinfo=timezone.utc)
            return now_dt
        return datetime.now(timezone.utc)

    # -------------------------------------------------------------------------
    # Authentication & Authorization Guard Pipeline
    # -------------------------------------------------------------------------

    def _verify_session_and_authorize(
        self,
        session_id: str,
        target_tenant_id: str,
        action: Union[AdminAction, str],
        organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Tuple[SaaSSession, TenantContext]:
        """
        Valida rigurosamente la sesión del operador y autoriza la acción administrativa solicitada.

        Reglas de seguridad:
        1. Sesión debe existir, estar ACTIVE y no haber expirado.
        2. Si session.tenant_id == target_tenant_id -> Tenant Admin normal evaluado con O.4 en scope tenant.
        3. Si session.tenant_id != target_tenant_id -> Cross-tenant request:
           Solo se permite si el llamador posee rol de plataforma global evaluado explícitamente en O.4/RBAC.
           De lo contrario -> AdminAuthorizationError.
        4. Acción administrativa explícita debe ser evaluada por O.4 con SaaSAuthorizationRequest.
        5. Decisión DENY -> AdminAuthorizationError.
        """
        if not session_id or not isinstance(session_id, str):
            raise AdminAuthenticationError("Missing or invalid session identifier.")

        if not target_tenant_id or not isinstance(target_tenant_id, str):
            raise AdminInvalidRequestError("Missing or invalid target tenant identifier.")

        corr_id = correlation_id or str(uuid.uuid4())
        session = self.session_repository.get_by_id(session_id)
        if not session:
            raise AdminAuthenticationError(f"Session '{session_id}' not found.")

        now_dt = self._now()
        if session.status != SessionStatus.ACTIVE:
            raise AdminAuthenticationError(f"Session '{session_id}' is not ACTIVE (status: {session.status}).")

        if session.is_expired(now_dt):
            raise AdminAuthenticationError(f"Session '{session_id}' has expired.")

        norm_action = normalize_action_token(action.value if isinstance(action, AdminAction) else str(action))

        # Validación Cross-Tenant
        is_cross_tenant = (session.tenant_id != target_tenant_id)
        if is_cross_tenant:
            # Comprobar si la identidad posee permiso global de plataforma
            has_platform_privilege = False
            if self.rbac_service:
                eval_res = self.rbac_service.resolve_effective_permissions(
                    principal_or_identity=session.identity_id,
                    scope="PLATFORM",
                    correlation_id=corr_id,
                )
                effective_actions = eval_res.effective_permissions.actions
                if "PLATFORM_ADMIN" in effective_actions or norm_action in effective_actions or "ALL" in effective_actions:
                    has_platform_privilege = True

            if not has_platform_privilege:
                raise AdminAuthorizationError(
                    f"Cross-tenant access forbidden: session tenant '{session.tenant_id}' cannot manage target tenant '{target_tenant_id}'."
                )

        # Autorización contextual formal mediante O.4
        eval_tenant_id = target_tenant_id if not is_cross_tenant else session.tenant_id
        auth_req = SaaSAuthorizationRequest(
            session_id=session_id,
            action=norm_action,
            tenant_id=eval_tenant_id,
            identity_id=session.identity_id,
            organization_id=organization_id,
            correlation_id=corr_id,
        )

        decision = self.authorization_service.authorize(auth_req)
        if decision.status != SaaSAuthorizationStatus.ALLOW:
            reason = decision.message or decision.reason_code.value
            raise AdminAuthorizationError(
                f"Unauthorized admin action '{norm_action}' on tenant '{target_tenant_id}': {reason}"
            )

        tenant_context = TenantContext(
            tenant_id=target_tenant_id,
            identity_id=session.identity_id,
            correlation_id=corr_id,
        )

        return session, tenant_context

    def _record_audit_mutation(
        self,
        actor_id: str,
        target_tenant_id: str,
        operation: str,
        details: Mapping[str, Any],
        correlation_id: str,
    ) -> None:
        """Registra un evento de mutación administrativa en K.1 sin secretos."""
        if not self.audit_repository:
            return

        sanitized_details = sanitize_security_data(dict(details))
        actor = AuditActor(
            actor_id=actor_id,
            actor_type=AuditActorType.USER,
        )
        record = AuditRecord(
            audit_id=f"audit_admin_{uuid.uuid4().hex[:16]}",
            occurred_at=self._now(),
            record_type=AuditRecordType.ACTION_EXECUTED,
            actor=actor,
            subject_type="TENANT",
            subject_id=target_tenant_id,
            action_or_operation=operation,
            status="SUCCESS",
            mission_id=f"admin_mgmt_{target_tenant_id}",
            correlation_id=correlation_id,
            provenance="ADMIN_CONSOLE_SERVICE",
            metadata={
                "description": f"Admin mutation: {operation} on tenant {target_tenant_id}",
                "target_tenant_id": target_tenant_id,
                **sanitized_details,
            },
        )
        try:
            self.audit_repository.append(record)
        except Exception as e:
            logger.warning("Failed to record admin audit record: %s", e)

    # -------------------------------------------------------------------------
    # O.11 Tenant Configuration (delegación mínima desde O.10)
    # -------------------------------------------------------------------------

    def get_tenant_configuration(
        self, session_id: str, target_tenant_id: str,
        organization_id: Optional[str] = None,
    ):
        if not self.tenant_configuration_service:
            raise AdminInvalidRequestError("Tenant configuration service is not configured.")
        session, context = self._verify_session_and_authorize(
            session_id, target_tenant_id, AdminAction.TENANT_CONFIG_READ,
            organization_id=organization_id,
        )
        return self.tenant_configuration_service.resolve_effective(
            target_tenant_id, organization_id=organization_id,
            actor_id=session.identity_id, correlation_id=context.correlation_id,
        )

    def update_tenant_configuration(
        self, session_id: str, target_tenant_id: str, key: str, value: Any,
        expected_version: Optional[int] = None,
        scope: ConfigurationScope = ConfigurationScope.TENANT,
        organization_id: Optional[str] = None,
    ):
        if not self.tenant_configuration_service:
            raise AdminInvalidRequestError("Tenant configuration service is not configured.")
        session, context = self._verify_session_and_authorize(
            session_id, target_tenant_id, AdminAction.TENANT_CONFIG_MANAGE,
            organization_id=organization_id,
        )
        return self.tenant_configuration_service.set_configuration(
            tenant_id=target_tenant_id, key=key, value=value,
            updated_by=session.identity_id, expected_version=expected_version,
            scope=scope, organization_id=organization_id,
            correlation_id=context.correlation_id,
        )

    # -------------------------------------------------------------------------
    # O.10 Tenant Management
    # -------------------------------------------------------------------------

    def get_tenant_summary(
        self,
        session_id: str,
        target_tenant_id: str,
        correlation_id: Optional[str] = None,
    ) -> TenantAdminSummary:
        """
        Obtiene un resumen administrativo integral y seguro del Tenant.
        Requiere TENANT_READ.
        """
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.TENANT_READ,
            correlation_id=correlation_id,
        )

        # 1. Organizaciones
        orgs_count = 0
        if self.organization_service:
            try:
                orgs = self.organization_service.list_organizations(context)
                orgs_count = len(orgs)
            except Exception:
                orgs_count = 0

        # 2. Plan Activo
        active_plan_view: Optional[PlanAdminView] = None
        if self.plan_entitlement_service:
            try:
                plan = self.plan_entitlement_service.get_active_plan(
                    tenant_id=target_tenant_id,
                    current_time=self._now(),
                    context=context,
                )
                if plan:
                    active_plan_view = PlanAdminView(
                        plan_id=plan.plan_id,
                        name=plan.name,
                        tier=plan.tier.value if hasattr(plan.tier, "value") else str(plan.tier),
                        version=plan.version,
                        status=plan.status.value if hasattr(plan.status, "value") else str(plan.status),
                        description=plan.metadata.get("description"),
                        limits=plan.limits.to_dict() if hasattr(plan.limits, "to_dict") else {},
                        features=[f.value if hasattr(f, "value") else str(f) for f in plan.features],
                    )
            except Exception:
                active_plan_view = None

        # 3. Cuotas
        quota_view: Optional[QuotaAdminView] = None
        if self.quota_policy_repository:
            try:
                policy = self.quota_policy_repository.get_policy(target_tenant_id)
                active_res = []
                if self.quota_reservation_repository:
                    active_res = self.quota_reservation_repository.list_active_reservations(
                        tenant_id=target_tenant_id,
                        current_time=self._now(),
                    )
                rules_summary = []
                if policy and policy.rules:
                    for r in policy.rules:
                        rules_summary.append({
                            "quota_type": r.quota_type.value if hasattr(r.quota_type, "value") else str(r.quota_type),
                            "scope": r.scope.value if hasattr(r.scope, "value") else str(r.scope),
                            "limit_value": str(r.limit_value),
                            "window_type": r.window_type.value if hasattr(r.window_type, "value") else str(r.window_type),
                        })
                quota_view = QuotaAdminView(
                    tenant_id=target_tenant_id,
                    policy_id=policy.policy_id if policy else None,
                    is_active=policy is not None,
                    rules_count=len(policy.rules) if policy and policy.rules else 0,
                    active_reservations_count=len(active_res),
                    rules_summary=rules_summary,
                )
            except Exception:
                quota_view = None

        # 4. Uso
        usage_view: Optional[UsageAdminSummary] = None
        if self.usage_metering_service:
            try:
                end_time = self._now()
                start_time = end_time - timedelta(days=30)
                q = UsageQuery(
                    tenant_id=target_tenant_id,
                    period=UsagePeriod(start_time=start_time, end_time=end_time, period_type=UsagePeriodType.CUSTOM),
                )
                agg = self.usage_metering_service.aggregate_usage(context, q)
                usage_view = UsageAdminSummary(
                    tenant_id=target_tenant_id,
                    period_start=start_time.isoformat(),
                    period_end=end_time.isoformat(),
                    total_requests=agg.total_requests,
                    successful_requests=agg.successful_requests,
                    failed_requests=agg.failed_requests,
                    cached_requests=agg.cached_requests,
                    input_tokens=agg.total_input_tokens,
                    output_tokens=agg.total_output_tokens,
                    total_tokens=agg.total_tokens,
                    estimated_cost=str(agg.total_estimated_cost),
                    actual_cost=str(agg.total_actual_cost),
                    model_breakdown=dict(agg.breakdown_by_model),
                    provider_breakdown=dict(agg.breakdown_by_provider),
                )
            except Exception:
                usage_view = None

        # 5. Billing
        billing_view: Optional[BillingAdminView] = None
        if self.subscription_service:
            try:
                sub = self.subscription_service.get_active_subscription(
                    tenant_id=target_tenant_id,
                    current_time=self._now(),
                    context=context,
                )
                invs_list = []
                if self.invoice_repository:
                    invs = self.invoice_repository.list_invoices_for_tenant(target_tenant_id)
                    for inv in invs[:5]:
                        invs_list.append(BillingInvoiceSummary(
                            invoice_id=inv.invoice_id,
                            status=inv.status.value if hasattr(inv.status, "value") else str(inv.status),
                            total_amount=str(inv.total_amount.amount),
                            currency=inv.total_amount.currency,
                            due_date=inv.due_date.isoformat() if inv.due_date else None,
                            paid_at=inv.paid_at.isoformat() if inv.paid_at else None,
                        ))
                billing_view = BillingAdminView(
                    tenant_id=target_tenant_id,
                    subscription_id=sub.subscription_id if sub else None,
                    subscription_status=sub.status.value if sub and hasattr(sub.status, "value") else (str(sub.status) if sub else "NONE"),
                    plan_id=sub.plan_id if sub else None,
                    plan_version=sub.plan_version if sub else None,
                    billing_cycle=sub.billing_cycle.value if sub and hasattr(sub.billing_cycle, "value") else None,
                    current_period_start=(
                        sub.current_period_start.isoformat()
                        if sub and hasattr(sub, "current_period_start") and sub.current_period_start
                        else (sub.current_period.start_time.isoformat() if sub and hasattr(sub, "current_period") and sub.current_period else None)
                    ),
                    current_period_end=(
                        sub.current_period_end.isoformat()
                        if sub and hasattr(sub, "current_period_end") and sub.current_period_end
                        else (sub.current_period.end_time.isoformat() if sub and hasattr(sub, "current_period") and sub.current_period else None)
                    ),
                    invoices_count=len(invs_list),
                    recent_invoices=invs_list,
                )
            except Exception:
                billing_view = None

        return TenantAdminSummary(
            tenant_id=target_tenant_id,
            tenant_name=None,
            organizations_count=orgs_count,
            active_plan=active_plan_view,
            quota_summary=quota_view,
            usage_summary=usage_view,
            billing_summary=billing_view,
        )

    # -------------------------------------------------------------------------
    # O.2 Organizations / Users Management
    # -------------------------------------------------------------------------

    def list_organizations(
        self,
        session_id: str,
        target_tenant_id: str,
        correlation_id: Optional[str] = None,
    ) -> List[OrganizationAdminView]:
        """Lista organizaciones del tenant. Requiere ORGANIZATION_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.ORGANIZATION_READ,
            correlation_id=correlation_id,
        )
        if not self.organization_service:
            return []

        orgs = self.organization_service.list_organizations(context)
        res = []
        for org in orgs:
            members_cnt = 0
            if self.membership_service:
                members_cnt = len(self.membership_service.list_organization_members(context, org.organization_id))
            res.append(OrganizationAdminView(
                organization_id=org.organization_id,
                name=org.name,
                status=org.status.value if hasattr(org.status, "value") else str(org.status),
                tenant_id=org.tenant_id,
                created_at=org.created_at.isoformat() if org.created_at else None,
                members_count=members_cnt,
                metadata=dict(org.metadata),
            ))
        return res

    def list_memberships(
        self,
        session_id: str,
        target_tenant_id: str,
        organization_id: str,
        correlation_id: Optional[str] = None,
    ) -> List[MembershipAdminView]:
        """Lista miembros de una organización con PII enmascarada. Requiere ORGANIZATION_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.ORGANIZATION_READ,
            organization_id=organization_id,
            correlation_id=correlation_id,
        )
        if not self.membership_service:
            return []

        members = self.membership_service.list_organization_members(context, organization_id)
        res = []
        for m in members:
            res.append(MembershipAdminView(
                membership_id=mask_pii(m.membership_id) or "***",
                organization_id=m.organization_id,
                tenant_id=m.tenant_id,
                identity_id_masked=mask_pii(m.identity_id) or "",
                role=m.role.value if hasattr(m.role, "value") else str(m.role),
                status=m.status.value if hasattr(m.status, "value") else str(m.status),
                created_at=m.joined_at.isoformat() if m.joined_at else None,
            ))
        return res

    def add_membership(
        self,
        session_id: str,
        target_tenant_id: str,
        organization_id: str,
        identity_id: str,
        role: MembershipRole = MembershipRole.MEMBER,
        correlation_id: Optional[str] = None,
    ) -> MembershipAdminView:
        """Añade membresía delegando a O.2. Requiere USER_MEMBERSHIP_MANAGE."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.USER_MEMBERSHIP_MANAGE,
            organization_id=organization_id,
            correlation_id=correlation_id,
        )
        if not self.membership_service:
            raise AdminConsoleError("OrganizationMembershipService is not configured.")

        membership = self.membership_service.add_membership(
            context=context,
            organization_id=organization_id,
            identity_id=identity_id,
            role=role,
            source="ADMIN_CONSOLE",
        )

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="ADD_MEMBERSHIP",
            details={
                "organization_id": organization_id,
                "identity_id_masked": mask_pii(identity_id),
                "role": role.value if hasattr(role, "value") else str(role),
            },
            correlation_id=context.correlation_id,
        )

        return MembershipAdminView(
            membership_id=membership.membership_id,
            organization_id=membership.organization_id,
            tenant_id=membership.tenant_id,
            identity_id_masked=mask_pii(membership.identity_id) or "",
            role=membership.role.value if hasattr(membership.role, "value") else str(membership.role),
            status=membership.status.value if hasattr(membership.status, "value") else str(membership.status),
            created_at=membership.joined_at.isoformat() if membership.joined_at else None,
        )

    def remove_membership(
        self,
        session_id: str,
        target_tenant_id: str,
        organization_id: str,
        identity_id: str,
        correlation_id: Optional[str] = None,
    ) -> MembershipAdminView:
        """Remueve membresía delegando a O.2. Requiere USER_MEMBERSHIP_MANAGE."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.USER_MEMBERSHIP_MANAGE,
            organization_id=organization_id,
            correlation_id=correlation_id,
        )
        if not self.membership_service:
            raise AdminConsoleError("OrganizationMembershipService is not configured.")

        membership = self.membership_service.remove_membership(
            context=context,
            organization_id=organization_id,
            identity_id=identity_id,
        )

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="REMOVE_MEMBERSHIP",
            details={
                "organization_id": organization_id,
                "identity_id_masked": mask_pii(identity_id),
            },
            correlation_id=context.correlation_id,
        )

        return MembershipAdminView(
            membership_id=membership.membership_id,
            organization_id=membership.organization_id,
            tenant_id=membership.tenant_id,
            identity_id_masked=mask_pii(membership.identity_id) or "",
            role=membership.role.value if hasattr(membership.role, "value") else str(membership.role),
            status=membership.status.value if hasattr(membership.status, "value") else str(membership.status),
            created_at=membership.joined_at.isoformat() if membership.joined_at else None,
        )

    # -------------------------------------------------------------------------
    # O.6 Usage View
    # -------------------------------------------------------------------------

    def get_usage_summary(
        self,
        session_id: str,
        target_tenant_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
    ) -> UsageAdminSummary:
        """Obtiene consumo de inferencia sin prompts ni CoT. Requiere TENANT_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.TENANT_READ,
            correlation_id=correlation_id,
        )
        if not self.usage_metering_service:
            raise AdminConsoleError("UsageMeteringService is not configured.")

        end_dt = end_time or self._now()
        start_dt = start_time or (end_dt - timedelta(days=30))

        q = UsageQuery(
            tenant_id=target_tenant_id,
            period=UsagePeriod(start_time=start_dt, end_time=end_dt, period_type=UsagePeriodType.CUSTOM),
        )
        agg = self.usage_metering_service.aggregate_usage(context, q)

        return UsageAdminSummary(
            tenant_id=target_tenant_id,
            period_start=start_dt.isoformat(),
            period_end=end_dt.isoformat(),
            total_requests=agg.total_requests,
            successful_requests=agg.successful_requests,
            failed_requests=agg.failed_requests,
            cached_requests=agg.cached_requests,
            input_tokens=agg.total_input_tokens,
            output_tokens=agg.total_output_tokens,
            total_tokens=agg.total_tokens,
            estimated_cost=str(agg.total_estimated_cost),
            actual_cost=str(agg.total_actual_cost),
            model_breakdown=_serialize_usage_breakdown(agg.breakdown_by_model),
            provider_breakdown=_serialize_usage_breakdown(agg.breakdown_by_provider),
        )

    # -------------------------------------------------------------------------
    # O.7 Quota Management
    # -------------------------------------------------------------------------

    def get_quota_view(
        self,
        session_id: str,
        target_tenant_id: str,
        correlation_id: Optional[str] = None,
    ) -> QuotaAdminView:
        """Inspecciona política de cuota y reservas. Requiere QUOTA_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.QUOTA_READ,
            correlation_id=correlation_id,
        )
        if not self.quota_policy_repository:
            raise AdminConsoleError("QuotaPolicyRepository is not configured.")

        policy = self.quota_policy_repository.get_policy(target_tenant_id)
        active_res = []
        if self.quota_reservation_repository:
            active_res = self.quota_reservation_repository.list_active_reservations(
                tenant_id=target_tenant_id,
                current_time=self._now(),
            )
        rules_summary = []
        if policy and policy.rules:
            for r in policy.rules:
                rules_summary.append({
                    "quota_type": r.quota_type.value if hasattr(r.quota_type, "value") else str(r.quota_type),
                    "scope": r.scope.value if hasattr(r.scope, "value") else str(r.scope),
                    "limit_value": str(r.limit_value),
                    "window_type": r.window_type.value if hasattr(r.window_type, "value") else str(r.window_type),
                })
        return QuotaAdminView(
            tenant_id=target_tenant_id,
            policy_id=policy.policy_id if policy else None,
            is_active=policy is not None,
            rules_count=len(policy.rules) if policy and policy.rules else 0,
            active_reservations_count=len(active_res),
            rules_summary=rules_summary,
        )

    def update_quota_policy(
        self,
        session_id: str,
        target_tenant_id: str,
        policy: QuotaPolicy,
        correlation_id: Optional[str] = None,
    ) -> QuotaAdminView:
        """Actualiza política de cuota del tenant. Requiere QUOTA_MANAGE."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.QUOTA_MANAGE,
            correlation_id=correlation_id,
        )
        if not self.quota_policy_repository:
            raise AdminConsoleError("QuotaPolicyRepository is not configured.")

        if policy.tenant_id != target_tenant_id:
            raise AdminInvalidRequestError(
                f"Policy tenant '{policy.tenant_id}' does not match target tenant '{target_tenant_id}'."
            )

        self.quota_policy_repository.save_policy(policy)

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="UPDATE_QUOTA_POLICY",
            details={
                "policy_id": policy.policy_id,
                "rules_count": len(policy.rules),
            },
            correlation_id=context.correlation_id,
        )

        return self.get_quota_view(session_id, target_tenant_id, correlation_id=correlation_id)

    # -------------------------------------------------------------------------
    # O.8 Plan Management
    # -------------------------------------------------------------------------

    def get_plan_view(
        self,
        session_id: str,
        target_tenant_id: str,
        correlation_id: Optional[str] = None,
    ) -> Optional[PlanAdminView]:
        """Obtiene el plan activo del tenant. Requiere PLAN_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.PLAN_READ,
            correlation_id=correlation_id,
        )
        if not self.plan_entitlement_service:
            raise AdminConsoleError("PlanEntitlementService is not configured.")

        plan = self.plan_entitlement_service.get_active_plan(
            tenant_id=target_tenant_id,
            current_time=self._now(),
            context=context,
        )
        if not plan:
            return None

        return PlanAdminView(
            plan_id=plan.plan_id,
            name=plan.name,
            tier=plan.tier.value if hasattr(plan.tier, "value") else str(plan.tier),
            version=plan.version,
            status=plan.status.value if hasattr(plan.status, "value") else str(plan.status),
            description=plan.metadata.get("description"),
            limits=plan.limits.to_dict() if hasattr(plan.limits, "to_dict") else {},
            features=[f.value if hasattr(f, "value") else str(f) for f in plan.features],
        )

    def list_catalog_plans(
        self,
        session_id: str,
        active_only: bool = True,
        correlation_id: Optional[str] = None,
    ) -> List[PlanAdminView]:
        """Lista el catálogo de planes. Requiere PLAN_READ."""
        if not session_id or not isinstance(session_id, str):
            raise AdminAuthenticationError("Missing session identifier.")
        session = self.session_repository.get_by_id(session_id)
        if not session or session.status != SessionStatus.ACTIVE or session.is_expired(self._now()):
            raise AdminAuthenticationError("Valid active session required.")

        # Verificar PLAN_READ en el tenant de sesión
        self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=session.tenant_id,
            action=AdminAction.PLAN_READ,
            correlation_id=correlation_id,
        )

        if not self.plan_repository:
            return []

        plans = self.plan_repository.list_plans(active_only=active_only)
        res = []
        for p in plans:
            res.append(PlanAdminView(
                plan_id=p.plan_id,
                name=p.name,
                tier=p.tier.value if hasattr(p.tier, "value") else str(p.tier),
                version=p.version,
                status=p.status.value if hasattr(p.status, "value") else str(p.status),
                description=p.metadata.get("description"),
                limits=p.limits.to_dict() if hasattr(p.limits, "to_dict") else {},
                features=[f.value if hasattr(f, "value") else str(f) for f in p.features],
            ))
        return res

    def assign_plan(
        self,
        session_id: str,
        target_tenant_id: str,
        plan_id: str,
        plan_version: Optional[str] = None,
        reason: str = "ADMIN_PLAN_ASSIGNMENT",
        correlation_id: Optional[str] = None,
    ) -> PlanAdminView:
        """Asigna o cambia el plan del tenant delegando en O.8. Requiere PLAN_ASSIGN."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.PLAN_ASSIGN,
            correlation_id=correlation_id,
        )
        if not self.plan_entitlement_service:
            raise AdminConsoleError("PlanEntitlementService is not configured.")

        assignment = self.plan_entitlement_service.assign_plan(
            tenant_id=target_tenant_id,
            plan_id=plan_id,
            plan_version=plan_version,
            source_reason=reason,
            actor_id=session.identity_id,
            context=context,
        )

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="ASSIGN_PLAN",
            details={
                "plan_id": plan_id,
                "plan_version": plan_version,
                "assignment_id": assignment.assignment_id,
                "reason": reason,
            },
            correlation_id=context.correlation_id,
        )

        updated_plan = self.get_plan_view(session_id, target_tenant_id, correlation_id=correlation_id)
        if not updated_plan:
            raise AdminResourceNotFoundError(f"Plan '{plan_id}' could not be resolved after assignment.")
        return updated_plan

    # -------------------------------------------------------------------------
    # O.9 Billing & Subscription Management
    # -------------------------------------------------------------------------

    def get_billing_view(
        self,
        session_id: str,
        target_tenant_id: str,
        correlation_id: Optional[str] = None,
    ) -> BillingAdminView:
        """Obtiene resumen financiero sin PAN/CVV ni secretos. Requiere BILLING_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.BILLING_READ,
            correlation_id=correlation_id,
        )
        if not self.subscription_service:
            raise AdminConsoleError("SubscriptionService is not configured.")

        sub = self.subscription_service.get_active_subscription(
            tenant_id=target_tenant_id,
            current_time=self._now(),
            context=context,
        )

        invs_list = []
        if self.invoice_repository:
            invs = self.invoice_repository.list_invoices_for_tenant(target_tenant_id)
            for inv in invs[:10]:
                invs_list.append(BillingInvoiceSummary(
                    invoice_id=inv.invoice_id,
                    status=inv.status.value if hasattr(inv.status, "value") else str(inv.status),
                    total_amount=str(inv.total_amount.amount),
                    currency=inv.total_amount.currency,
                    due_date=inv.due_date.isoformat() if inv.due_date else None,
                    paid_at=inv.paid_at.isoformat() if inv.paid_at else None,
                ))

        return BillingAdminView(
            tenant_id=target_tenant_id,
            subscription_id=sub.subscription_id if sub else None,
            subscription_status=sub.status.value if sub and hasattr(sub.status, "value") else (str(sub.status) if sub else "NONE"),
            plan_id=sub.plan_id if sub else None,
            plan_version=sub.plan_version if sub else None,
            billing_cycle=sub.billing_cycle.value if sub and hasattr(sub.billing_cycle, "value") else None,
            current_period_start=sub.current_period.start_time.isoformat() if sub and sub.current_period else None,
            current_period_end=sub.current_period.end_time.isoformat() if sub and sub.current_period else None,
            invoices_count=len(invs_list),
            recent_invoices=invs_list,
        )

    def create_subscription(
        self,
        session_id: str,
        target_tenant_id: str,
        plan_id: str,
        plan_version: Optional[str] = None,
        billing_cycle: BillingCycle = BillingCycle.MONTHLY,
        auto_activate: bool = False,
        correlation_id: Optional[str] = None,
    ) -> BillingAdminView:
        """Crea suscripción comercial delegando en O.9. Requiere BILLING_MANAGE."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.BILLING_MANAGE,
            correlation_id=correlation_id,
        )
        if not self.subscription_service:
            raise AdminConsoleError("SubscriptionService is not configured.")

        sub = self.subscription_service.create_subscription(
            tenant_id=target_tenant_id,
            plan_id=plan_id,
            plan_version=plan_version,
            billing_cycle=billing_cycle,
            auto_activate=auto_activate,
            actor_id=session.identity_id,
            context=context,
        )

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="CREATE_SUBSCRIPTION",
            details={
                "subscription_id": sub.subscription_id,
                "plan_id": plan_id,
                "billing_cycle": billing_cycle.value if hasattr(billing_cycle, "value") else str(billing_cycle),
            },
            correlation_id=context.correlation_id,
        )

        return self.get_billing_view(session_id, target_tenant_id, correlation_id=correlation_id)

    def activate_subscription(
        self,
        session_id: str,
        target_tenant_id: str,
        subscription_id: str,
        correlation_id: Optional[str] = None,
    ) -> BillingAdminView:
        """Activa una suscripción. Requiere BILLING_MANAGE."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.BILLING_MANAGE,
            correlation_id=correlation_id,
        )
        if not self.subscription_service:
            raise AdminConsoleError("SubscriptionService is not configured.")

        sub = self.subscription_service.activate_subscription(
            subscription_id=subscription_id,
            actor_id=session.identity_id,
            context=context,
        )

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="ACTIVATE_SUBSCRIPTION",
            details={"subscription_id": subscription_id},
            correlation_id=context.correlation_id,
        )

        return self.get_billing_view(session_id, target_tenant_id, correlation_id=correlation_id)

    def cancel_subscription(
        self,
        session_id: str,
        target_tenant_id: str,
        subscription_id: str,
        immediately: bool = False,
        correlation_id: Optional[str] = None,
    ) -> BillingAdminView:
        """Cancela una suscripción preservando histórico. Requiere BILLING_MANAGE."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.BILLING_MANAGE,
            correlation_id=correlation_id,
        )
        if not self.subscription_service:
            raise AdminConsoleError("SubscriptionService is not configured.")

        sub = self.subscription_service.cancel_subscription(
            subscription_id=subscription_id,
            immediately=immediately,
            actor_id=session.identity_id,
            context=context,
        )

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="CANCEL_SUBSCRIPTION",
            details={
                "subscription_id": subscription_id,
                "immediately": immediately,
            },
            correlation_id=context.correlation_id,
        )

        return self.get_billing_view(session_id, target_tenant_id, correlation_id=correlation_id)

    # -------------------------------------------------------------------------
    # K.1 Audit & K.2 Trace Views
    # -------------------------------------------------------------------------

    def get_audit_records(
        self,
        session_id: str,
        target_tenant_id: str,
        limit: int = 50,
        correlation_id: Optional[str] = None,
    ) -> List[AuditAdminView]:
        """Obtiene hechos de auditoría del tenant sanitizados. Requiere AUDIT_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.AUDIT_READ,
            correlation_id=correlation_id,
        )
        if not self.audit_repository:
            return []

        records = self.audit_repository.list_records(limit=min(limit, 200))
        res = []
        for r in records:
            # Filtrar registros asociados a este tenant
            mission_id = r.mission_id or ""
            target_hint = r.details.get("target_tenant_id", "") if isinstance(r.details, dict) else ""
            if target_tenant_id in mission_id or target_hint == target_tenant_id or r.correlation_id == context.correlation_id:
                actor_masked = mask_pii(r.actor.actor_id) if r.actor else "UNKNOWN"
                res.append(AuditAdminView(
                    audit_id=r.record_id,
                    timestamp=r.timestamp.isoformat() if r.timestamp else "",
                    actor_id_masked=actor_masked or "",
                    record_type=r.record_type.value if hasattr(r.record_type, "value") else str(r.record_type),
                    operation=r.description or "",
                    target_tenant_id=target_tenant_id,
                    details_sanitized=dict(r.details) if r.details else {},
                ))
        return res

    def get_trace_records(
        self,
        session_id: str,
        target_tenant_id: str,
        limit: int = 50,
        correlation_id: Optional[str] = None,
    ) -> List[TraceAdminView]:
        """Obtiene trazas de ejecución sanitizadas. Requiere TRACE_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.TRACE_READ,
            correlation_id=correlation_id,
        )
        if not self.trace_repository:
            return []

        records = self.trace_repository.list_records(limit=min(limit, 200))
        res = []
        for tr in records:
            res.append(TraceAdminView(
                trace_id=tr.trace_id,
                execution_id=tr.execution_id,
                component_name=tr.component_name,
                step_number=tr.step_number,
                step_type=tr.step_type.value if hasattr(tr.step_type, "value") else str(tr.step_type),
                operation=tr.operation,
                status=tr.status.value if hasattr(tr.status, "value") else str(tr.status),
                correlation_id=tr.correlation_id,
                started_at=tr.started_at.isoformat() if tr.started_at else None,
                completed_at=tr.completed_at.isoformat() if tr.completed_at else None,
            ))
        return res

    # -------------------------------------------------------------------------
    # O.12 SaaS Observability (delegación mínima al servicio O.12)
    # -------------------------------------------------------------------------

    def _require_observability_service(self) -> TenantObservabilityServicePort:
        if not self.tenant_observability_service:
            raise AdminInvalidRequestError("TenantObservabilityService is not configured.")
        return self.tenant_observability_service

    def get_tenant_observability_snapshot(
        self,
        session_id: str,
        target_tenant_id: str,
        window_seconds: int = 3600,
        organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ObservabilityAdminView:
        """Vista segura de observabilidad del tenant. Requiere OBSERVABILITY_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.OBSERVABILITY_READ,
            organization_id=organization_id,
            correlation_id=correlation_id,
        )
        obs_service = self._require_observability_service()

        # Validación de ventana determinista (5m/1h/24h)
        if window_seconds not in (300, 3600, 86400):
            raise AdminInvalidRequestError(
                f"Unsupported window_seconds '{window_seconds}'. Supported windows: 300, 3600, 86400."
            )

        snapshot = obs_service.get_tenant_snapshot(
            tenant_id=target_tenant_id,
            organization_id=organization_id,
            window_seconds=window_seconds,
        )

        active_alerts = tuple(
            ObservabilityAlertAdminView(
                alert_id=a.alert_id,
                tenant_id=a.tenant_id,
                alert_type=a.alert_type.value if hasattr(a.alert_type, "value") else str(a.alert_type),
                severity=a.severity.value if hasattr(a.severity, "value") else str(a.severity),
                status=a.status.value if hasattr(a.status, "value") else str(a.status),
                summary=a.summary,
                triggered_at=a.triggered_at.isoformat() if a.triggered_at else "",
                resolved_at=a.resolved_at.isoformat() if a.resolved_at else None,
                acknowledged_at=a.acknowledged_at.isoformat() if a.acknowledged_at else None,
                organization_id=a.organization_id,
            )
            for a in snapshot.active_alerts
        )

        return ObservabilityAdminView(
            tenant_id=snapshot.tenant_id,
            health_status=snapshot.health_status.value if hasattr(snapshot.health_status, "value") else str(snapshot.health_status),
            window_seconds=snapshot.window_seconds,
            evaluated_at=snapshot.evaluated_at.isoformat(),
            request_count=snapshot.request_count,
            error_rate=snapshot.error_rate,
            avg_latency_ms=snapshot.avg_latency_ms,
            p95_latency_ms=snapshot.p95_latency_ms,
            total_tokens=snapshot.total_tokens,
            total_cost_usd=str(snapshot.total_cost_usd) if snapshot.total_cost_usd is not None else None,
            quota_status=snapshot.quota_status,
            billing_status=snapshot.billing_status,
            active_alerts_count=len(active_alerts),
            active_alerts=active_alerts,
            metrics=_serialize_usage_breakdown(
                {k: m.to_dict() for k, m in snapshot.metrics.items()}
            ),
            checksum=snapshot.checksum,
        )

    def list_tenant_observability_alerts(
        self,
        session_id: str,
        target_tenant_id: str,
        status: Optional[str] = None,
        organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> List[ObservabilityAlertAdminView]:
        """Lista alertas operacionales del tenant. Requiere OBSERVABILITY_READ."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.OBSERVABILITY_READ,
            organization_id=organization_id,
            correlation_id=correlation_id,
        )
        obs_service = self._require_observability_service()

        status_filter = None
        if status:
            status_upper = status.upper()
            valid_statuses = [s.value for s in AlertStatus]
            if status_upper not in valid_statuses:
                raise AdminInvalidRequestError(f"Invalid alert status '{status}'. Valid statuses: {valid_statuses}")
            status_filter = AlertStatus(status_upper)

        alerts = obs_service.list_alerts(
            tenant_id=target_tenant_id,
            status=status_filter,
            organization_id=organization_id,
        )

        return [
            ObservabilityAlertAdminView(
                alert_id=a.alert_id,
                tenant_id=a.tenant_id,
                alert_type=a.alert_type.value if hasattr(a.alert_type, "value") else str(a.alert_type),
                severity=a.severity.value if hasattr(a.severity, "value") else str(a.severity),
                status=a.status.value if hasattr(a.status, "value") else str(a.status),
                summary=a.summary,
                triggered_at=a.triggered_at.isoformat() if a.triggered_at else "",
                resolved_at=a.resolved_at.isoformat() if a.resolved_at else None,
                acknowledged_at=a.acknowledged_at.isoformat() if a.acknowledged_at else None,
                organization_id=a.organization_id,
            )
            for a in alerts
        ]

    def acknowledge_tenant_observability_alert(
        self,
        session_id: str,
        target_tenant_id: str,
        alert_id: str,
        organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ObservabilityAlertAdminView:
        """Reconoce una alerta operacional. Requiere OBSERVABILITY_ALERT_MANAGE (ACK)."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.OBSERVABILITY_ALERT_MANAGE,
            organization_id=organization_id,
            correlation_id=correlation_id,
        )
        obs_service = self._require_observability_service()

        alert = obs_service.acknowledge_alert(
            tenant_id=target_tenant_id,
            alert_id=alert_id,
            actor_id=session.identity_id,
        )

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="ACK_OBSERVABILITY_ALERT",
            details={"alert_id": alert_id},
            correlation_id=context.correlation_id,
        )

        return ObservabilityAlertAdminView(
            alert_id=alert.alert_id,
            tenant_id=alert.tenant_id,
            alert_type=alert.alert_type.value if hasattr(alert.alert_type, "value") else str(alert.alert_type),
            severity=alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity),
            status=alert.status.value if hasattr(alert.status, "value") else str(alert.status),
            summary=alert.summary,
            triggered_at=alert.triggered_at.isoformat() if alert.triggered_at else "",
            resolved_at=alert.resolved_at.isoformat() if alert.resolved_at else None,
            acknowledged_at=alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
            organization_id=alert.organization_id,
        )

    def resolve_tenant_observability_alert(
        self,
        session_id: str,
        target_tenant_id: str,
        alert_id: str,
        reason: str = "",
        organization_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ObservabilityAlertAdminView:
        """Resuelve una alerta operacional. Requiere OBSERVABILITY_ALERT_MANAGE (MANAGE)."""
        session, context = self._verify_session_and_authorize(
            session_id=session_id,
            target_tenant_id=target_tenant_id,
            action=AdminAction.OBSERVABILITY_ALERT_MANAGE,
            organization_id=organization_id,
            correlation_id=correlation_id,
        )
        obs_service = self._require_observability_service()

        alert = obs_service.resolve_alert(
            tenant_id=target_tenant_id,
            alert_id=alert_id,
            actor_id=session.identity_id,
            reason=reason,
        )

        self._record_audit_mutation(
            actor_id=session.identity_id,
            target_tenant_id=target_tenant_id,
            operation="RESOLVE_OBSERVABILITY_ALERT",
            details={"alert_id": alert_id, "reason": reason},
            correlation_id=context.correlation_id,
        )

        return ObservabilityAlertAdminView(
            alert_id=alert.alert_id,
            tenant_id=alert.tenant_id,
            alert_type=alert.alert_type.value if hasattr(alert.alert_type, "value") else str(alert.alert_type),
            severity=alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity),
            status=alert.status.value if hasattr(alert.status, "value") else str(alert.status),
            summary=alert.summary,
            triggered_at=alert.triggered_at.isoformat() if alert.triggered_at else "",
            resolved_at=alert.resolved_at.isoformat() if alert.resolved_at else None,
            acknowledged_at=alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
            organization_id=alert.organization_id,
        )
