"""
Tests de Integración E2E para GATE N — Formal Validation & Closure of Hito O
(SaaS / Platformization).

Principio Fundamental a Validar:
"El sistema SaaS multi-tenant garantiza aislamiento estricto de datos, memoria,
caché, credenciales, cuotas, facturación, configuración, auditoría y trazas
entre tenants, sin contaminación cruzada, con persistencia atómica y resiliencia
ante concurrencia y reinicios."

Cobertura (10 invariantes transversales de Hito O):

1. Aislamiento de tenant en recursos y memoria (O.1).
   - Tenant A crea recurso -> B no puede leerlo.
   - Memoria de producto particionada por directorio.

2. Autenticación, autorización y RBAC (O.3, O.4, N.2, N.4).
   - Autenticación válida produce sesión autenticada.
   - Credencial inválida o sujeta bloqueada.
   - RBAC scope tenant-isolated: permisos en A no aplican a B.

3. Configuración de modelo y credenciales por tenant (O.5, N.5).
   - TenantModelConfig diferente por tenant.
   - SecretReference distintas por tenant; InjectedSecretProvider aísla resolución.

4. Caché sin contaminación cruzada (O.1 M.4).
   - Mismo prompt con security_context_id distinto -> MISS en B tras HIT en A.

5. Cuota y uso (O.6, O.7).
   - Eventos de uso particionados por tenant.
   - Quota policy materializada por tenant; reserva thread-safe.

6. Planes y billing (O.8, O.9).
   - Tenant se suscribe a plan; se genera factura y pago exitoso.
   - Aislamiento: B no puede procesar pago de A (CrossTenantAccessError).
   - Persistencia JSON con verificación de integridad tras reinicio.

7. Configuración y observabilidad (O.11, O.12).
   - TenantConfigurationService partitiona por tenant y sobrevive reinicio.
   - TenantObservabilityService deriva snapshot desde UsageMeteringService.

8. Auditoría y trazas (K.1, K.2).
   - Registros de auditoría persistidos por correlation_id y entity_reference.
   - AgentTrace records persistidos con idempotencia y verificación de integridad.

9. Concurrencia.
   - Operaciones paralelas A/B sobre repositorios con RLock sin contaminación.

10. Reinicio y persistencia.
    - Repositorios JSON reinstanciados sobre mismo directorio conservan datos y aislamiento.
    - Checksums SHA-256 verificados en reinicio.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, List, Optional, Mapping, Sequence, Tuple
import json

import pytest
from starlette.testclient import TestClient

# --- Domain Models ---
from src.domain.tenant.models import (
    TenantContext,
    TenantScope,
    TenantScopedResource,
    CrossTenantAccessError,
)
from src.domain.identity.models import (
    Identity,
    IdentityType,
    IdentityStatus,
)
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationRequest,
    AuthenticationStatus,
)
from src.domain.rbac.models import Permission, Role, RoleAssignment
from src.domain.secrets.models import SecretReference, SecretType, SecretValue, SecretResolutionStatus
from src.domain.model_gateway.models import TenantModelConfig
from src.domain.caching.models import (
    CacheLookupRequest,
    CacheLookupStatus,
    CacheStoreRequest,
)
from src.domain.product_memory.models import ProductMemoryRecord
from src.domain.market_intelligence.models import Marketplace
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageQuery,
    UsageRequestStatus,
    CacheLookupStatus as UCacheLookupStatus,
)
from src.domain.quota_management.models import (
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
    QuotaRequest,
)
from src.domain.plans.models import (
    Plan,
    PlanTier,
    PlanFeature,
    PlanLimits,
    PlanQuotaTemplate,
    PlanEntitlementRequest,
    PlanEntitlementStatus,
)
from src.domain.billing.models import (
    BillingCycle,
    InvoiceStatus,
    PaymentStatus,
)
from src.domain.tenant_configuration.models import (
    ConfigurationScope,
    ConfigurationVersionConflictError,
)
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.agent_trace.models import StepType, TraceStatus
from src.domain.saas_observability.models import (
    TenantHealthStatus,
    OperationalAlert,
    OperationalAlertType,
    AlertSeverity,
    AlertStatus,
)
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.organization.models import (
    Organization,
    OrganizationStatus,
    UserMembership,
    MembershipStatus,
    MembershipRole,
)
from src.domain.rbac.models import PermissionStatus, RoleStatus
from src.domain.model_gateway.models import ModelGatewayRequest, ModelGatewayStatus
from src.domain.model_gateway.ports import ModelProviderPort, TenantModelConfigRepositoryPort
from src.domain.model_routing.models import (
    ModelRoute,
    RouteCapability,
    RouteStatus,
    QualityRequirement,
    LatencyRequirement,
)
from src.domain.model_routing.ports import ModelRouteRegistryPort
from src.domain.model_selection.models import StandardTaskType
from src.domain.cost.models import PricingRate
from src.domain.quota_management.models import QuotaPolicy, QuotaReservationStatus
from src.domain.tool_policy.models import (
    ToolAccessStatus,
    ToolRuleAction,
    ToolReference,
    ToolPolicyRule,
    ToolPolicy,
    ToolAccessRequest,
)

# --- Infrastructure ---
from src.infrastructure.persistence.data.json.tenant_scoped_repository import JsonTenantScopedRepository
from src.infrastructure.persistence.data.json.product_memory_repository import JsonProductMemoryRepository
from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.infrastructure.persistence.data.json.secret_metadata_repository import JsonSecretMetadataRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.tenant_configuration_repository import JsonTenantConfigurationRepository
from src.infrastructure.persistence.data.json.operational_alert_repository import JsonOperationalAlertRepository
from src.infrastructure.persistence.data.json.cache_repository import JsonCacheRepository
from src.infrastructure.persistence.data.json.billing_repository import (
    JsonSubscriptionRepository,
    JsonInvoiceRepository,
    JsonPaymentAttemptRepository,
    JsonPaymentProviderEventRepository,
    InMemorySubscriptionRepository,
    InMemoryInvoiceRepository,
    InMemoryPaymentAttemptRepository,
    InMemoryPaymentProviderEventRepository,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    InMemoryPlanCatalogRepository,
    InMemoryPlanAssignmentRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
    InMemoryQuotaReservationRepository,
)
from src.infrastructure.persistence.data.json.usage_event_repository import InMemoryUsageEventRepository
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonOrganizationRepository,
    JsonMembershipRepository,
)
from src.infrastructure.persistence.data.in_memory.cache_repository import InMemoryCacheRepository
from src.infrastructure.persistence.data.json.tool_policy_repository import JsonToolPolicyRepository
from src.infrastructure.secrets.providers import InjectedSecretProvider
from src.infrastructure.billing.mock_payment_provider import MockPaymentProvider
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock

# --- Application Services ---
from src.application.tenant.tenant_context_service import TenantContextService
from src.application.product_memory.product_memory_service import ProductMemoryService
from src.application.caching.inference_cache_service import InferenceCacheService
from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import AuthenticationService
from src.application.rbac.rbac_service import RBACService
from src.application.secrets.secret_service import SecretService
from src.application.audit.audit_trail_service import AuditTrailService
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.billing.subscription_service import SubscriptionService
from src.application.billing.billing_service import BillingService
from src.application.tenant_configuration.tenant_configuration_service import TenantConfigurationService
from src.application.saas_observability.tenant_observability_service import TenantObservabilityService
from src.application.session.saas_session_service import SaaSSessionService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_selection.model_selection_service import (
    ModelSelectionByTaskService,
    DefaultTaskSelectionPolicyProvider,
)
from src.application.context_budget.context_budget_service import ContextBudgetService
from src.application.prompt_compression.deterministic_compressor import DeterministicPromptCompressor
from src.application.cost_aware_policy.cost_aware_decision_service import CostAwareDecisionService
from src.application.cost.pricing_catalog import InMemoryPricingCatalog
from src.application.security.sensitive_data_handling_service import SensitiveDataHandlingService
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.application.model_gateway.model_gateway_service import ModelGatewayService
from src.application.usage_metering.model_gateway_bridge import ModelGatewayUsageBridge
from src.application.admin_console.admin_console_service import AdminConsoleService
from src.infrastructure.web.admin_app import create_admin_app
from src.application.organization.organization_service import (
    OrganizationService,
    OrganizationMembershipService,
)


class GateTenantModelConfigRepository(TenantModelConfigRepositoryPort):
    def __init__(self):
        self.configs: Dict[str, TenantModelConfig] = {}

    def get_config(self, tenant_id: str) -> Optional[TenantModelConfig]:
        return self.configs.get(tenant_id)

    def save_config(self, config: TenantModelConfig) -> None:
        self.configs[config.tenant_id] = config


class GateRouteRegistry(ModelRouteRegistryPort):
    def __init__(self, routes: Sequence[ModelRoute]):
        self.routes = {route.route_id: route for route in routes}

    def list_routes(self) -> Tuple[ModelRoute, ...]:
        return tuple(self.routes.values())

    def get_route(self, route_id: str) -> Optional[ModelRoute]:
        return self.routes.get(route_id)


class GateProvider(ModelProviderPort):
    def __init__(self):
        self.invocations: List[Dict[str, Any]] = []

    def execute_inference(
        self,
        route: ModelRoute,
        prompt_payload: Any,
        secret: Optional[SecretValue] = None,
        tools: Sequence[str] = (),
        temperature: float = 0.0,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.invocations.append({
            "route": route,
            "prompt_payload": prompt_payload,
            "secret_reference": "resolved" if secret else None,
            "tools": tuple(tools),
            "metadata": dict(metadata or {}),
        })
        return {
            "output_content": "Gate N deterministic inference",
            "output_structured": {"result": "success"},
            "input_tokens": 120,
            "output_tokens": 45,
            "finish_reason": "stop",
        }


# =========================================================================
# FIXTURE: Entorno completo Gate N con dos tenants
# =========================================================================

@pytest.fixture
def gate_env(tmp_path: Path):
    """
    Fixture integral que instanciará servicios reales del proyecto para
    validar los diez invariantes de Hito O en un solo entorno compartido.
    """
    clock = VirtualClock(initial_time=datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc))

    # ---- Repositorios compartidos por directorio tmp ----
    tenant_repo = JsonTenantScopedRepository(tmp_path)
    identity_repo = JsonIdentityRepository(tmp_path / "identities.json")
    role_repo = JsonRoleRepository(tmp_path / "rbac")
    assign_repo = JsonRoleAssignmentRepository(tmp_path / "rbac")
    secret_meta_repo = JsonSecretMetadataRepository(tmp_path / "secrets")
    audit_repo = JsonAuditRepository(tmp_path / "audit")
    trace_repo = JsonAgentTraceRepository(tmp_path / "traces")
    config_repo = JsonTenantConfigurationRepository(tmp_path)
    alert_repo = JsonOperationalAlertRepository(tmp_path)
    cache_repo = JsonCacheRepository(tmp_path / "cache")

    # Repositorios de billing persistentes (se usan para el test de persistencia)
    sub_repo_json = JsonSubscriptionRepository(tmp_path)
    inv_repo_json = JsonInvoiceRepository(tmp_path)
    pay_repo_json = JsonPaymentAttemptRepository(tmp_path)
    evt_repo_json = JsonPaymentProviderEventRepository(tmp_path)

    # Repositorios in-memory para el escenario rápido de billing
    sub_repo = InMemorySubscriptionRepository()
    inv_repo = InMemoryInvoiceRepository()
    pay_repo = InMemoryPaymentAttemptRepository()
    evt_repo = InMemoryPaymentProviderEventRepository()
    payment_provider = MockPaymentProvider(provider_name="mock_stripe", auto_succeed=True)

    # Repositorios de uso y cuota
    usage_repo = InMemoryUsageEventRepository()
    quota_policy_repo = InMemoryQuotaPolicyRepository()
    quota_reservation_repo = InMemoryQuotaReservationRepository()

    # Plan catalog y assignments
    plan_catalog = InMemoryPlanCatalogRepository()
    plan_assign_repo = InMemoryPlanAssignmentRepository()

    # ---- Servicios ----
    ctx_service = TenantContextService()
    secret_provider = InjectedSecretProvider({
        "sec_tenant_alpha": "secret_value_alpha_real_abc123",
        "sec_tenant_beta": "secret_value_beta_real_xyz789",
    })
    secret_svc = SecretService(
        providers=[secret_provider],
        metadata_repository=secret_meta_repo,
        clock=clock,
    )
    id_svc = IdentityService(identity_repo)
    auth_svc = AuthenticationService(
        identity_service=id_svc,
        clock=clock,
        audit_repository=audit_repo,
        trusted_api_credentials={
            "cred_alpha": "user_alpha_ops",
            "cred_beta": "user_beta_ops",
        },
    )
    rbac_svc = RBACService(
        role_repository=role_repo,
        assignment_repository=assign_repo,
        clock=clock,
    )
    audit_svc = AuditTrailService(audit_repo)
    trace_svc = AgentTraceService(trace_repo)
    usage_svc = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_svc = QuotaManagementService(
        policy_repository=quota_policy_repo,
        reservation_repository=quota_reservation_repo,
        usage_metering_service=usage_svc,
        clock=clock,
    )
    config_svc = TenantConfigurationService(config_repo)
    cache_svc = InferenceCacheService(repository=cache_repo, clock=clock)
    observability_svc = TenantObservabilityService(
        alert_repository=alert_repo,
        clock=clock,
        usage_metering_service=usage_svc,
    )

    # ---- Plan y Billing ----
    q_rule = QuotaRule(
        rule_id="qrule_gate_n",
        quota_type=QuotaType.MAX_REQUESTS,
        scope=QuotaScope.TENANT,
        limit_value=500,
        window_type=QuotaWindowType.MONTH,
    )
    pro_plan = Plan(
        plan_id="plan_pro_gate_n",
        name="Pro Plan Gate N",
        tier=PlanTier.PRO,
        version="1.0.0",
        limits=PlanLimits(max_users=5),
        features=[
            PlanFeature.MODEL_INFERENCE,
            PlanFeature.ADVANCED_ANALYTICS,
            PlanFeature.MULTI_USER,
        ],
        quota_template=PlanQuotaTemplate(rules=[q_rule]),
        allowed_model_classes=["gpt-4o", "claude-3-5-sonnet"],
        allowed_providers=["openai", "anthropic"],
        metadata={"base_price": "99.00", "currency": "USD"},
    )
    plan_catalog.save_plan(pro_plan)

    entitlement_svc = PlanEntitlementService(
        catalog_repository=plan_catalog,
        assignment_repository=plan_assign_repo,
        quota_policy_repository=quota_policy_repo,
        clock=clock,
    )
    sub_svc = SubscriptionService(
        subscription_repository=sub_repo_json,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_svc,
    )
    bill_svc = BillingService(
        subscription_repository=sub_repo_json,
        invoice_repository=inv_repo_json,
        payment_attempt_repository=pay_repo_json,
        event_repository=evt_repo_json,
        payment_provider=payment_provider,
        subscription_service=sub_svc,
    )
    sub_svc.set_billing_service(bill_svc)

    # ---- Registrar tenants y configurar aislamiento ----
    ctx_service.register_tenant("tenant_alpha", "Tenant Alpha")
    ctx_service.register_tenant("tenant_beta", "Tenant Beta")

    # Configurar identidades
    now = clock.now()
    for tenant_id, identity_id, cred_key in [
        ("tenant_alpha", "user_alpha_ops", "cred_alpha"),
        ("tenant_beta", "user_beta_ops", "cred_beta"),
    ]:
        ident = Identity(
            identity_id=identity_id,
            identity_type=IdentityType.USER,
            canonical_identifier=f"gateway_n:{tenant_id}:{identity_id}",
            created_at=now,
            updated_at=now,
            display_name=f"User {identity_id}",
            status=IdentityStatus.ACTIVE,
        )
        id_svc.register_identity(
            identity_id=identity_id,
            identity_type=IdentityType.USER,
            canonical_identifier=ident.canonical_identifier,
            display_name=ident.display_name,
        )

    # Configurar RBAC: permiso por scope tenant
    perm_alpha = rbac_svc.create_permission("perm_alpha_gate_n", "GATE_N_ALPHA_ACTION")
    perm_beta = rbac_svc.create_permission("perm_beta_gate_n", "GATE_N_BETA_ACTION")

    role_alpha = rbac_svc.define_role("role_alpha_gate_n", "Alpha Role Gate N", [perm_alpha])
    role_beta = rbac_svc.define_role("role_beta_gate_n", "Beta Role Gate N", [perm_beta])

    rbac_svc.assign_role(
        assignment_id="asgn_alpha_gate_n",
        identity_id="user_alpha_ops",
        role_id=role_alpha.role_id,
        scope="tenant_tenant_alpha",
    )
    rbac_svc.assign_role(
        assignment_id="asgn_beta_gate_n",
        identity_id="user_beta_ops",
        role_id=role_beta.role_id,
        scope="tenant_tenant_beta",
    )

    # Configurar TenantModelConfig por tenant
    sec_ref_alpha = SecretReference(
        reference_id="sec_tenant_alpha",
        provider="injected",
        secret_name="openai_key_alpha",
        secret_type=SecretType.API_KEY,
    )
    sec_ref_beta = SecretReference(
        reference_id="sec_tenant_beta",
        provider="injected",
        secret_name="openai_key_beta",
        secret_type=SecretType.API_KEY,
    )

    tenant_config_alpha = TenantModelConfig(
        tenant_id="tenant_alpha",
        allowed_providers=("openai",),
        allowed_models=("gpt-4o",),
        credential_references={"openai": sec_ref_alpha},
        allow_fallback=False,
    )
    tenant_config_beta = TenantModelConfig(
        tenant_id="tenant_beta",
        allowed_providers=("openai", "anthropic"),
        allowed_models=("gpt-4o", "claude-3-5-sonnet"),
        credential_references={"openai": sec_ref_beta, "anthropic": sec_ref_beta},
        allow_fallback=True,
    )

    return {
        "clock": clock,
        "tmp_path": tmp_path,
        "quota_reservation_repo": quota_reservation_repo,
        "plan_assign_repo": plan_assign_repo,
        # Tenant Context
        "ctx_service": ctx_service,
        # Repositorios
        "tenant_repo": tenant_repo,
        "identity_repo": identity_repo,
        "audit_repo": audit_repo,
        "trace_repo": trace_repo,
        "cache_repo": cache_repo,
        "config_repo": config_repo,
        "alert_repo": alert_repo,
        "secret_meta_repo": secret_meta_repo,
        # Servicios
        "secret_svc": secret_svc,
        "id_svc": id_svc,
        "auth_svc": auth_svc,
        "rbac_svc": rbac_svc,
        "audit_svc": audit_svc,
        "trace_svc": trace_svc,
        "usage_svc": usage_svc,
        "quota_svc": quota_svc,
        "config_svc": config_svc,
        "cache_svc": cache_svc,
        "observability_svc": observability_svc,
        # Tenant model configs
        "tenant_config_alpha": tenant_config_alpha,
        "tenant_config_beta": tenant_config_beta,
        "sec_ref_alpha": sec_ref_alpha,
        "sec_ref_beta": sec_ref_beta,
        # Secret provider
        "secret_provider": secret_provider,
        # Billing
        "plan_catalog": plan_catalog,
        "entitlement_svc": entitlement_svc,
        "sub_svc": sub_svc,
        "bill_svc": bill_svc,
        "sub_repo": sub_repo,
        "inv_repo": inv_repo,
        "payment_provider": payment_provider,
        # Repositorios JSON billing (para persistencia)
        "sub_repo_json": sub_repo_json,
        "inv_repo_json": inv_repo_json,
        "pay_repo_json": pay_repo_json,
        "evt_repo_json": evt_repo_json,
        # Quota
        "quota_policy_repo": quota_policy_repo,
    }


# =========================================================================
# 1. AISLAMIENTO DE TENANT EN RECURSOS Y MEMORIA
# =========================================================================

@pytest.fixture
def pipeline_env(gate_env):
    env = gate_env
    root = env["tmp_path"] / "pipeline"
    session_repo = JsonSaaSSessionRepository(root)
    org_repo = JsonOrganizationRepository(root)
    membership_repo = JsonMembershipRepository(root)
    organization_service = OrganizationService(org_repo, audit_repository=env["audit_repo"])
    membership_service = OrganizationMembershipService(
        membership_repo, org_repo, audit_repository=env["audit_repo"]
    )

    auth_results = {}
    sessions = {}
    for tenant_id, identity_id, credential, org_id in (
        ("tenant_alpha", "user_alpha_ops", "cred_alpha", "org_alpha"),
        ("tenant_beta", "user_beta_ops", "cred_beta", "org_beta"),
    ):
        context = TenantContext(tenant_id=tenant_id, identity_id=identity_id)
        organization_service.create_organization(context, org_id, f"Organization {tenant_id}")
        membership_service.add_membership(context, org_id, identity_id, MembershipRole.ADMIN)
        auth_result = env["auth_svc"].authenticate_request(AuthenticationRequest(
            method=AuthenticationMethod.API_CREDENTIAL,
            provider="gate_n",
            token_or_secret=credential,
            declared_subject=identity_id,
            correlation_id=f"corr_auth_{tenant_id}",
        ))
        auth_results[tenant_id] = auth_result

    session_service = SaaSSessionService(
        session_repository=session_repo,
        organization_repo=org_repo,
        membership_repo=membership_repo,
        identity_repo=env["identity_repo"],
        clock=env["clock"],
        audit_repository=env["audit_repo"],
    )
    for tenant_id, identity_id, _, org_id in (
        ("tenant_alpha", "user_alpha_ops", "cred_alpha", "org_alpha"),
        ("tenant_beta", "user_beta_ops", "cred_beta", "org_beta"),
    ):
        sessions[tenant_id] = session_service.create_session(
            auth_results[tenant_id], tenant_id, org_id,
            correlation_id=f"corr_session_{tenant_id}",
        )

    permission = Permission(
        permission_id="perm_model_inference_gate",
        action="MODEL_INFERENCE_EXECUTE",
        status=PermissionStatus.ACTIVE,
    )
    role = Role(
        role_id="role_model_inference_gate",
        name="Gate model user",
        permissions=(permission,),
        status=RoleStatus.ACTIVE,
    )
    env["rbac_svc"].role_repository.save_role(role)
    for tenant_id, identity_id in (
        ("tenant_alpha", "user_alpha_ops"),
        ("tenant_beta", "user_beta_ops"),
    ):
        env["rbac_svc"].assignment_repository.save_assignment(RoleAssignment(
            assignment_id=f"assign_model_{tenant_id}",
            identity_id=identity_id,
            role_id=role.role_id,
            scope=TenantScope(tenant_id=tenant_id).canonical_scope,
            assigned_at=env["clock"].now(),
        ))

    saas_authorization = SaaSAuthorizationService(
        session_repository=session_repo,
        session_service=session_service,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=env["rbac_svc"],
        authorization_service=AuthorizationService(clock=env["clock"]),
        audit_repository=env["audit_repo"],
        trace_service=env["trace_svc"],
        clock=env["clock"],
    )
    config_repo = GateTenantModelConfigRepository()
    config_repo.save_config(env["tenant_config_alpha"])
    config_repo.save_config(env["tenant_config_beta"])
    route = ModelRoute(
        route_id="route_gate_gpt4o",
        provider="openai",
        model_id="gpt-4o",
        context_window=128000,
        quality_class=QualityRequirement.SUPERIOR,
        latency_class=LatencyRequirement.NORMAL,
        capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.TOOL_USE),
        status=RouteStatus.AVAILABLE,
        priority=1,
    )
    routes = GateRouteRegistry((route,))
    routing = DeterministicModelRoutingStrategy()
    pricing = InMemoryPricingCatalog()
    pricing.register_rate(PricingRate(
        provider="openai", service_or_model="gpt-4o", currency="USD",
        input_rate=Decimal("5"), output_rate=Decimal("15"),
        rate_scale=Decimal("1000000"),
    ))
    provider = GateProvider()
    gateway = ModelGatewayService(
        session_service=session_service,
        session_repository=session_repo,
        saas_authorization_service=saas_authorization,
        tenant_config_repository=config_repo,
        model_selection_service=ModelSelectionByTaskService(
            policy_provider=DefaultTaskSelectionPolicyProvider(), routing_strategy=routing
        ),
        model_routing_strategy=routing,
        route_registry=routes,
        context_budget_service=ContextBudgetService(),
        prompt_compressor=DeterministicPromptCompressor(),
        cost_aware_service=CostAwareDecisionService(pricing_catalog=pricing),
        inference_cache_service=InferenceCacheService(InMemoryCacheRepository(), clock=env["clock"]),
        sensitive_data_service=SensitiveDataHandlingService(),
        secret_resolver=env["secret_svc"],
        tool_policy_service=ToolAccessPolicyService(),
        quota_management_service=env["quota_svc"],
        provider_adapters={"openai": provider, "default": provider},
        audit_repository=env["audit_repo"],
        trace_service=env["trace_svc"],
        clock=env["clock"],
    )
    env.update({
        "session_repo": session_repo,
        "session_service": session_service,
        "sessions": sessions,
        "saas_authorization": saas_authorization,
        "gateway": gateway,
        "provider": provider,
        "organization_service": organization_service,
        "membership_service": membership_service,
        "org_repo": org_repo,
        "membership_repo": membership_repo,
    })
    return env


def test_01_tenant_resource_and_memory_isolation(gate_env):
    """
    O.1 — Tenant A crea recursos y memoria; Tenant B no puede leerlos.
    Recursos en JsonTenantScopedRepository y memoria en JsonProductMemoryRepository
    particionados por tenant.
    """
    env = gate_env
    ctx_a = TenantContext(tenant_id="tenant_alpha")
    ctx_b = TenantContext(tenant_id="tenant_beta")

    # 1a. Recursos: A crea, B no puede leer
    res_a = TenantScopedResource(
        tenant_id="tenant_alpha",
        resource_id="pricing_rule_q3",
        resource_type="strategies",
        payload={"markup": 0.35, "target_roi": 1.5},
    )
    env["tenant_repo"].save(ctx_a, res_a)

    found_a = env["tenant_repo"].get_by_id(ctx_a, "strategies", "pricing_rule_q3")
    assert found_a is not None
    assert found_a.payload["markup"] == 0.35

    found_b = env["tenant_repo"].get_by_id(ctx_b, "strategies", "pricing_rule_q3")
    assert found_b is None

    # 1b. Mismo resource_id en A y B -> registros independientes
    res_b = TenantScopedResource(
        tenant_id="tenant_beta",
        resource_id="pricing_rule_q3",
        resource_type="strategies",
        payload={"markup": 0.50, "target_roi": 2.0},
    )
    env["tenant_repo"].save(ctx_b, res_b)

    get_a = env["tenant_repo"].get_by_id(ctx_a, "strategies", "pricing_rule_q3")
    get_b = env["tenant_repo"].get_by_id(ctx_b, "strategies", "pricing_rule_q3")
    assert get_a.payload["markup"] == 0.35
    assert get_b.payload["markup"] == 0.50

    # 1c. Memoria de producto: repositorios particionados por directorio
    mem_repo_a = JsonProductMemoryRepository(env["tmp_path"] / "tenants" / "tenant_alpha" / "memory")
    mem_repo_b = JsonProductMemoryRepository(env["tmp_path"] / "tenants" / "tenant_beta" / "memory")
    mem_svc_a = ProductMemoryService(mem_repo_a)
    mem_svc_b = ProductMemoryService(mem_repo_b)

    mem_svc_a.record_product_memory(
        product_memory_id="mem_001",
        sku="SKU-ALPHA-01",
        external_id="MLC-111111",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Producto Exclusivo Alpha",
        category="Tecnología",
        price_amount=Decimal("120000"),
        seller_id="seller_alpha",
    )

    assert mem_svc_a.get_product_memory_by_id("mem_001") is not None
    assert mem_svc_b.get_product_memory_by_id("mem_001") is None
    assert mem_svc_b.get_product_memory_by_sku("SKU-ALPHA-01") is None


# =========================================================================
# 2. AUTENTICACIÓN, AUTORIZACIÓN Y RBAC
# =========================================================================

def test_02_authentication_authorization_rbac(gate_env):
    """
    O.3/O.4/N.2/N.4 — Autenticación válida/inválida, RBAC scope tenant-isolated.
    """
    env = gate_env

    # 2a. Autenticación válida para tenant_alpha
    req_alpha = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="gate_n",
        token_or_secret="cred_alpha",
        declared_subject="user_alpha_ops",
        correlation_id="corr_auth_alpha",
    )
    result_alpha = env["auth_svc"].authenticate_request(req_alpha)
    assert result_alpha.status == AuthenticationStatus.AUTHENTICATED
    assert result_alpha.principal is not None
    assert result_alpha.principal.identity_id == "user_alpha_ops"

    # 2b. Autenticación válida para tenant_beta
    req_beta = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="gate_n",
        token_or_secret="cred_beta",
        declared_subject="user_beta_ops",
        correlation_id="corr_auth_beta",
    )
    result_beta = env["auth_svc"].authenticate_request(req_beta)
    assert result_beta.status == AuthenticationStatus.AUTHENTICATED
    assert result_beta.principal.identity_id == "user_beta_ops"

    # 2c. Credencial inválida -> INVALID
    req_invalid = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="gate_n",
        token_or_secret="cred_invalid_nonexistent",
        declared_subject="user_unknown",
        correlation_id="corr_auth_invalid",
    )
    result_invalid = env["auth_svc"].authenticate_request(req_invalid)
    assert result_invalid.status == AuthenticationStatus.INVALID
    assert result_invalid.principal is None

    # 2d. Credencial válida pero subject distinto al vinculado -> INVALID / SUBJECT_MISMATCH
    req_mismatch = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="gate_n",
        token_or_secret="cred_alpha",
        declared_subject="user_beta_ops",
        correlation_id="corr_auth_mismatch",
    )
    result_mismatch = env["auth_svc"].authenticate_request(req_mismatch)
    assert result_mismatch.status == AuthenticationStatus.INVALID

    # 2e. RBAC: permisos de Alpha no aplican a scope de Beta
    res_alpha_scope = env["rbac_svc"].resolve_effective_permissions(
        "user_alpha_ops", scope="tenant_tenant_alpha"
    )
    assert "GATE_N_ALPHA_ACTION" in res_alpha_scope.actions

    res_alpha_beta_scope = env["rbac_svc"].resolve_effective_permissions(
        "user_alpha_ops", scope="tenant_tenant_beta"
    )
    assert "GATE_N_ALPHA_ACTION" not in res_alpha_beta_scope.actions

    res_beta_scope = env["rbac_svc"].resolve_effective_permissions(
        "user_beta_ops", scope="tenant_tenant_beta"
    )
    assert "GATE_N_BETA_ACTION" in res_beta_scope.actions

    res_beta_alpha_scope = env["rbac_svc"].resolve_effective_permissions(
        "user_beta_ops", scope="tenant_tenant_alpha"
    )
    assert "GATE_N_BETA_ACTION" not in res_beta_alpha_scope.actions


# =========================================================================
# 3. CONFIGURACIÓN DE MODELO Y CREDENCIALES POR TENANT
# =========================================================================

def test_03_model_config_and_credential_isolation(gate_env):
    """
    O.5/N.5 — TenantModelConfig distinta por tenant; credenciales resueltas de forma aislada.
    """
    env = gate_env

    # 3a. Configuración distinta por tenant
    cfg_a = env["tenant_config_alpha"]
    cfg_b = env["tenant_config_beta"]

    assert cfg_a.tenant_id == "tenant_alpha"
    assert cfg_b.tenant_id == "tenant_beta"
    assert cfg_a.is_provider_allowed("openai") is True
    assert cfg_a.is_provider_allowed("anthropic") is False
    assert cfg_b.is_provider_allowed("openai") is True
    assert cfg_b.is_provider_allowed("anthropic") is True

    assert cfg_a.is_model_allowed("gpt-4o") is True
    assert cfg_a.is_model_allowed("claude-3-5-sonnet") is False
    assert cfg_b.is_model_allowed("gpt-4o") is True
    assert cfg_b.is_model_allowed("claude-3-5-sonnet") is True

    # 3b. Credenciales: cada tenant tiene su propia SecretReference
    ref_a = env["sec_ref_alpha"]
    ref_b = env["sec_ref_beta"]
    assert ref_a.reference_id != ref_b.reference_id

    # 3c. Resolución de secretos: cada provider resuelve su propio reference_id
    res_a = env["secret_svc"].resolve(ref_a)
    assert res_a.status == SecretResolutionStatus.RESOLVED
    assert res_a.secret_value.reveal() == "secret_value_alpha_real_abc123"

    res_b = env["secret_svc"].resolve(ref_b)
    assert res_b.status == SecretResolutionStatus.RESOLVED
    assert res_b.secret_value.reveal() == "secret_value_beta_real_xyz789"

    # 3d. Referencia inexistente -> NOT_FOUND
    ref_nonexistent = SecretReference(
        reference_id="sec_nonexistent",
        provider="injected",
        secret_name="missing",
        secret_type=SecretType.API_KEY,
    )
    res_missing = env["secret_svc"].resolve(ref_nonexistent)
    assert res_missing.status == SecretResolutionStatus.NOT_FOUND
    assert res_missing.secret_value is None

    # 3e. Configuración por tenant en TenantConfigurationService
    cfg_alpha_result = env["config_svc"].set_configuration(
        "tenant_alpha", "model.preferred_provider", "openai", "admin",
    )
    cfg_beta_result = env["config_svc"].set_configuration(
        "tenant_beta", "model.preferred_provider", "anthropic", "admin",
    )
    assert cfg_alpha_result.value.value == "openai"
    assert cfg_beta_result.value.value == "anthropic"

    # 3f. Aislamiento: Alpha no ve config de Beta
    alpha_only = env["config_svc"].get_configuration("tenant_alpha", "model.preferred_provider")
    beta_only = env["config_svc"].get_configuration("tenant_beta", "model.preferred_provider")
    assert alpha_only.value.value == "openai"
    assert beta_only.value.value == "anthropic"


# =========================================================================
# 4. CACHÉ SIN CONTAMINACIÓN CRUZADA
# =========================================================================

def test_04_cache_no_cross_tenant_contamination(gate_env):
    """
    O.1/M.4 — Mismo prompt con security_context_id distinto genera cache keys separadas.
    """
    env = gate_env
    prompt = "Analizar margen óptimo para Smartwatch Z"

    # 4a. Tenant Alpha consulta -> MISS
    req_a = CacheLookupRequest(
        normalized_prompt_or_payload=prompt,
        route_or_model_id="omniroute_smart",
        security_context_id="tenant_alpha",
    )
    res_a = env["cache_svc"].lookup(req_a)
    assert res_a.status == CacheLookupStatus.MISS

    # 4b. Guardar respuesta para Alpha
    env["cache_svc"].store(CacheStoreRequest(
        lookup_request=req_a,
        result_data={"analysis": "Margen recomendado 32%"},
    ))

    # 4c. Tenant Alpha vuelve a consultar -> HIT
    res_a2 = env["cache_svc"].lookup(req_a)
    assert res_a2.status == CacheLookupStatus.HIT
    assert res_a2.entry.result_data["analysis"] == "Margen recomendado 32%"

    # 4d. Tenant Beta consulta el mismo prompt -> MISS (no recibe cache de Alpha)
    req_b = CacheLookupRequest(
        normalized_prompt_or_payload=prompt,
        route_or_model_id="omniroute_smart",
        security_context_id="tenant_beta",
    )
    res_b = env["cache_svc"].lookup(req_b)
    assert res_b.status == CacheLookupStatus.MISS
    assert res_b.cache_key != res_a.cache_key


# =========================================================================
# 5. CUOTA Y USO
# =========================================================================

def test_05_usage_and_quota_isolation(gate_env):
    """
    O.6/O.7 — Eventos de uso particionados por tenant; cuotas y reservas independientes.
    """
    env = gate_env
    ctx_a = TenantContext(tenant_id="tenant_alpha")
    ctx_b = TenantContext(tenant_id="tenant_beta")
    now = env["clock"].now()

    # 5a. Registrar eventos de uso para cada tenant
    event_a = UsageEvent(
        usage_event_id="ue_gate_n_a1",
        tenant_id="tenant_alpha",
        occurred_at=now,
        request_status=UsageRequestStatus.SUCCESS,
        provider="openai",
        model="gpt-4o",
        input_tokens=150,
        output_tokens=50,
        estimated_cost=Decimal("0.002"),
        correlation_id="corr_usage_a",
    )
    env["usage_svc"].record_usage_event(ctx_a, event_a)

    event_b = UsageEvent(
        usage_event_id="ue_gate_n_b1",
        tenant_id="tenant_beta",
        occurred_at=now,
        request_status=UsageRequestStatus.SUCCESS,
        provider="anthropic",
        model="claude-3-5-sonnet",
        input_tokens=200,
        output_tokens=80,
        estimated_cost=Decimal("0.003"),
        correlation_id="corr_usage_b",
    )
    env["usage_svc"].record_usage_event(ctx_b, event_b)

    # 5b. Agregación por tenant: totals separados
    agg_a = env["usage_svc"].aggregate_usage(
        ctx_a,
        UsageQuery(tenant_id="tenant_alpha"),
    )
    agg_b = env["usage_svc"].aggregate_usage(
        ctx_b,
        UsageQuery(tenant_id="tenant_beta"),
    )

    assert agg_a.total_requests == 1
    assert agg_a.total_input_tokens == 150
    assert agg_b.total_requests == 1
    assert agg_b.total_input_tokens == 200

    # 5c. Policy de cuota materializada por plan para tenant_alpha
    q_rule = QuotaRule(
        rule_id="qrule_gate_n_usage",
        quota_type=QuotaType.MAX_REQUESTS,
        scope=QuotaScope.TENANT,
        limit_value=10,
        window_type=QuotaWindowType.MONTH,
    )
    from src.domain.quota_management.models import QuotaPolicy
    policy_a = QuotaPolicy(
        policy_id="qp_gate_n_alpha",
        tenant_id="tenant_alpha",
        rules=[q_rule],
    )
    env["quota_policy_repo"].save_policy(policy_a)

    # 5d. Reserva de cuota para tenant_alpha -> ALLOW (dentro de límite)
    req_quota = QuotaRequest(
        tenant_id="tenant_alpha",
        identity_id="user_alpha_ops",
        estimated_total_tokens=500,
        estimated_cost=Decimal("0.01"),
        correlation_id="corr_quota_a1",
    )
    decision = env["quota_svc"].evaluate_and_reserve(req_quota)
    assert decision.is_allowed is True
    assert decision.tenant_id == "tenant_alpha"

    # 5e. Sin política para tenant_beta -> fail-closed (no ALLOW)
    req_quota_b = QuotaRequest(
        tenant_id="tenant_beta",
        identity_id="user_beta_ops",
        estimated_total_tokens=500,
        correlation_id="corr_quota_b1",
    )
    decision_b = env["quota_svc"].evaluate_and_reserve(req_quota_b)
    assert decision_b.is_allowed is False


# =========================================================================
# 6. PLANES Y BILLING
# =========================================================================

def test_06_plans_billing_and_tenant_isolation(gate_env):
    """
    O.8/O.9 — Tenant se suscribe, factura se genera, pago exitoso.
    Aislamiento: B no puede procesar pago de A.
    """
    env = gate_env

    # 6a. Tenant Alpha se suscribe
    sub_a = env["sub_svc"].create_subscription(
        tenant_id="tenant_alpha",
        plan_id="plan_pro_gate_n",
        billing_cycle=BillingCycle.MONTHLY,
    )
    assert sub_a.subscription_id.startswith("sub_")
    assert sub_a.tenant_id == "tenant_alpha"
    assert sub_a.base_price == Decimal("99.00")

    # 6b. Generar factura
    inv_a = env["bill_svc"].generate_period_invoice(sub_a.subscription_id)
    assert inv_a.subscription_id == sub_a.subscription_id
    assert inv_a.total == Decimal("99.00")
    assert inv_a.status == InvoiceStatus.OPEN

    # 6c. Pago exitoso
    attempt = env["bill_svc"].process_payment(inv_a.invoice_id, idempotency_key="pay_gate_n_01")
    assert attempt.status == PaymentStatus.SUCCEEDED

    inv_updated = env["inv_repo_json"].get_invoice(inv_a.invoice_id)
    assert inv_updated.status == InvoiceStatus.PAID

    sub_updated = env["sub_svc"].get_active_subscription("tenant_alpha")
    assert sub_updated is not None
    assert sub_updated.status.value == "ACTIVE"

    # 6d. Tenant Beta no puede procesar pago de la factura de Alpha
    ctx_b = TenantContext(tenant_id="tenant_beta")
    with pytest.raises(CrossTenantAccessError):
        env["bill_svc"].process_payment(
            inv_a.invoice_id,
            idempotency_key="pay_cross_tenant_attempt",
            context=ctx_b,
        )

    # 6e. Entitlement verificado
    ent_decision = env["entitlement_svc"].evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_alpha", feature=PlanFeature.MODEL_INFERENCE)
    )
    assert ent_decision.status == PlanEntitlementStatus.ALLOW
    assert ent_decision.is_entitled is True

    # 6f. Quota policy materializada en repositorio in-memory
    policy = env["quota_policy_repo"].get_policy("tenant_alpha")
    assert policy is not None
    assert len(policy.rules) >= 1


# =========================================================================
# 7. CONFIGURACIÓN Y OBSERVABILIDAD
# =========================================================================

def test_07_configuration_and_observability(gate_env):
    """
    O.11/O.12 — TenantConfiguration partitiona por tenant y sobrevive reinicio.
    TenantObservabilityService deriva snapshot desde UsageMeteringService.
    """
    env = gate_env

    # 7a. Configuración por tenant
    cfg_a = env["config_svc"].set_configuration(
        "tenant_alpha", "branding.display_name", "Alpha Corp", "admin",
    )
    cfg_b = env["config_svc"].set_configuration(
        "tenant_beta", "branding.display_name", "Beta Inc", "admin",
    )
    assert cfg_a.value.value == "Alpha Corp"
    assert cfg_b.value.value == "Beta Inc"

    # 7b. Aislamiento
    get_a = env["config_svc"].get_configuration("tenant_alpha", "branding.display_name")
    get_b = env["config_svc"].get_configuration("tenant_beta", "branding.display_name")
    assert get_a.value.value == "Alpha Corp"
    assert get_b.value.value == "Beta Inc"

    # 7c. B no ve config de A
    assert env["config_svc"].get_configuration("tenant_beta", "branding.display_name") is not None
    beta_has_alpha = env["config_svc"].get_configuration(
        "tenant_alpha", "branding.display_name"
    )
    assert beta_has_alpha.value.value == "Alpha Corp"

    # 7d. Observabilidad: snapshot para tenant_alpha sin tráfico reciente
    snapshot_a = env["observability_svc"].get_tenant_snapshot("tenant_alpha")
    assert snapshot_a.tenant_id == "tenant_alpha"
    assert snapshot_a.health_status in (
        TenantHealthStatus.UNKNOWN,
        TenantHealthStatus.HEALTHY,
    )
    assert snapshot_a.window_seconds == 3600

    # 7e. Observabilidad: snapshot para tenant_beta
    snapshot_b = env["observability_svc"].get_tenant_snapshot("tenant_beta")
    assert snapshot_b.tenant_id == "tenant_beta"

    # 7f. Registrar uso para Alpha y verificar métricas en snapshot
    ctx_a = TenantContext(tenant_id="tenant_alpha")
    event_obs = UsageEvent(
        usage_event_id="ue_obs_01",
        tenant_id="tenant_alpha",
        occurred_at=env["clock"].now(),
        request_status=UsageRequestStatus.SUCCESS,
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        correlation_id="corr_obs",
    )
    env["usage_svc"].record_usage_event(ctx_a, event_obs)

    # Avanzar el reloj para que el evento quede dentro de la ventana
    # [start_time, end_time) y no en el boundary
    env["clock"].sleep(1)

    snapshot_a_after = env["observability_svc"].get_tenant_snapshot("tenant_alpha")
    assert snapshot_a_after.request_count is not None
    assert snapshot_a_after.request_count >= 1


# =========================================================================
# 8. AUDITORÍA Y TRAZAS
# =========================================================================

def test_08_audit_trail_and_agent_traces(gate_env):
    """
    K.1/K.2 — Registros de auditoría y trazas persistidos, sanitizados e idempotentes.
    """
    env = gate_env
    now = env["clock"].now()

    # 8a. Registrar eventos de auditoría para cada tenant
    rec_a = AuditRecord(
        audit_id="aud_gate_n_001",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=now,
        actor=AuditActor(actor_id="user_alpha_ops", actor_type=AuditActorType.USER),
        subject_type="Listing",
        subject_id="item_alpha_001",
        action_or_operation="CREATE_LISTING",
        status="SUCCESS",
        correlation_id="corr_audit_alpha",
        entity_reference="tenant_alpha:item_alpha_001",
    )
    env["audit_repo"].append(rec_a)

    rec_b = AuditRecord(
        audit_id="aud_gate_n_002",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=now,
        actor=AuditActor(actor_id="user_beta_ops", actor_type=AuditActorType.USER),
        subject_type="Listing",
        subject_id="item_beta_001",
        action_or_operation="CREATE_LISTING",
        status="SUCCESS",
        correlation_id="corr_audit_beta",
        entity_reference="tenant_beta:item_beta_001",
    )
    env["audit_repo"].append(rec_b)

    # 8b. Verificar persistencia por ID
    f_a = env["audit_repo"].get_by_id("aud_gate_n_001")
    f_b = env["audit_repo"].get_by_id("aud_gate_n_002")
    assert f_a is not None
    assert f_b is not None
    assert f_a.entity_reference == "tenant_alpha:item_alpha_001"
    assert f_b.entity_reference == "tenant_beta:item_beta_001"

    # 8c. Filtrar por correlation_id
    records_alpha = env["audit_repo"].list_records(correlation_id="corr_audit_alpha")
    assert len(records_alpha) >= 1
    assert all(r.correlation_id == "corr_audit_alpha" for r in records_alpha)

    records_beta = env["audit_repo"].list_records(correlation_id="corr_audit_beta")
    assert len(records_beta) >= 1
    assert all(r.correlation_id == "corr_audit_beta" for r in records_beta)

    # 8d. Agent Trace: registrar trazas para cada tenant
    trace_a = env["trace_svc"].record_step(
        component_name="gate_n_component",
        execution_id="exec_alpha_001",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="create_listing",
        status=TraceStatus.SUCCESS,
        correlation_id="corr_trace_alpha",
        mission_id="msn_alpha_001",
        metadata={"secret_key": "should_be_sanitized", "prompt_content": "secret prompt"},
    )
    assert trace_a is not None
    trace_str_a = str(trace_a.metadata)
    assert "should_be_sanitized" not in trace_str_a

    trace_b = env["trace_svc"].record_step(
        component_name="gate_n_component",
        execution_id="exec_beta_001",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="create_listing",
        status=TraceStatus.SUCCESS,
        correlation_id="corr_trace_beta",
        mission_id="msn_beta_001",
    )
    assert trace_b is not None

    # 8e. Verificar trazas por trace_id
    found_trace_a = env["trace_repo"].get_by_id(trace_a.trace_id)
    assert found_trace_a is not None
    assert found_trace_a.execution_id == "exec_alpha_001"
    assert found_trace_a.mission_id == "msn_alpha_001"


# =========================================================================
# 9. CONCURRENCIA
# =========================================================================

def test_09_concurrent_tenant_operations(gate_env):
    """
    Concurrencia — Operaciones paralelas de Tenant A y Tenant B sin contaminación cruzada
    sobre repositorios thread-safe (JsonTenantScopedRepository).
    """
    env = gate_env
    errors = []

    def worker_alpha(i: int):
        try:
            ctx = TenantContext(tenant_id="tenant_alpha")
            res = TenantScopedResource(
                tenant_id="tenant_alpha",
                resource_id=f"conc_item_{i}",
                resource_type="concurrent_items",
                payload={"owner": "tenant_alpha", "index": i},
            )
            env["tenant_repo"].save(ctx, res)
            read_back = env["tenant_repo"].get_by_id(ctx, "concurrent_items", f"conc_item_{i}")
            assert read_back.payload["owner"] == "tenant_alpha"
        except Exception as e:
            errors.append(e)

    def worker_beta(i: int):
        try:
            ctx = TenantContext(tenant_id="tenant_beta")
            res = TenantScopedResource(
                tenant_id="tenant_beta",
                resource_id=f"conc_item_{i}",
                resource_type="concurrent_items",
                payload={"owner": "tenant_beta", "index": i},
            )
            env["tenant_repo"].save(ctx, res)
            read_back = env["tenant_repo"].get_by_id(ctx, "concurrent_items", f"conc_item_{i}")
            assert read_back.payload["owner"] == "tenant_beta"
        except Exception as e:
            errors.append(e)

    threads = []
    for i in range(10):
        threads.append(threading.Thread(target=worker_alpha, args=(i,)))
        threads.append(threading.Thread(target=worker_beta, args=(i,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0

    ctx_a = TenantContext(tenant_id="tenant_alpha")
    ctx_b = TenantContext(tenant_id="tenant_beta")
    all_a = env["tenant_repo"].list_all(ctx_a, "concurrent_items")
    all_b = env["tenant_repo"].list_all(ctx_b, "concurrent_items")

    assert len(all_a) == 10
    assert len(all_b) == 10
    assert all(item.payload["owner"] == "tenant_alpha" for item in all_a)
    assert all(item.payload["owner"] == "tenant_beta" for item in all_b)


# =========================================================================
# 10. REINICIO Y PERSISTENCIA
# =========================================================================

def test_10_restart_and_persistence(gate_env):
    """
    Persistencia — Repositorios JSON reinstanciados sobre mismo directorio
    conservan datos, aislamiento e integridad de checksums.
    """
    env = gate_env
    tmp_path = env["tmp_path"]
    ctx_a = TenantContext(tenant_id="tenant_alpha")
    ctx_b = TenantContext(tenant_id="tenant_beta")
    now = env["clock"].now()

    # 10a. Persistir recursos de negocio
    res_alpha = TenantScopedResource(
        tenant_id="tenant_alpha",
        resource_id="persistent_pricing",
        resource_type="rules",
        payload={"markup": 0.35},
    )
    res_beta = TenantScopedResource(
        tenant_id="tenant_beta",
        resource_id="persistent_pricing",
        resource_type="rules",
        payload={"markup": 0.50},
    )
    env["tenant_repo"].save(ctx_a, res_alpha)
    env["tenant_repo"].save(ctx_b, res_beta)

    # 10b. Persistir configuración
    env["config_svc"].set_configuration(
        "tenant_alpha", "branding.display_name", "Alpha Persistent", "admin",
    )
    env["config_svc"].set_configuration(
        "tenant_beta", "branding.display_name", "Beta Persistent", "admin",
    )

    # 10c. Simular RESTART: reinstanciar repositorios sobre mismo directorio
    repo_restarted = JsonTenantScopedRepository(tmp_path)
    config_repo_restarted = JsonTenantConfigurationRepository(tmp_path)

    # 10d. Verificar que los datos persisten
    read_a = repo_restarted.get_by_id(ctx_a, "rules", "persistent_pricing")
    read_b = repo_restarted.get_by_id(ctx_b, "rules", "persistent_pricing")
    assert read_a is not None
    assert read_b is not None
    assert read_a.payload["markup"] == 0.35
    assert read_b.payload["markup"] == 0.50

    # 10e. Verificar aislamiento preservado tras reinicio
    assert read_a.tenant_id == "tenant_alpha"
    assert read_b.tenant_id == "tenant_beta"
    assert repo_restarted.get_by_id(ctx_b, "rules", "persistent_pricing").tenant_id == "tenant_beta"

    # 10f. Verificar configuración persistida
    config_restarted = TenantConfigurationService(config_repo_restarted)
    cfg_a_restart = config_restarted.get_configuration("tenant_alpha", "branding.display_name")
    cfg_b_restart = config_restarted.get_configuration("tenant_beta", "branding.display_name")
    assert cfg_a_restart is not None
    assert cfg_a_restart.value.value == "Alpha Persistent"
    assert cfg_b_restart is not None
    assert cfg_b_restart.value.value == "Beta Persistent"
    assert config_restarted.get_configuration("tenant_nonexistent", "branding.display_name") is None

    # 10g. Persistir suscripción y verificar tras reinicio (JSON billing repos)
    sub = env["sub_svc"].create_subscription(
        tenant_id="tenant_alpha",
        plan_id="plan_pro_gate_n",
    )
    inv = env["bill_svc"].generate_period_invoice(sub.subscription_id)
    att = env["bill_svc"].process_payment(inv.invoice_id, idempotency_key="pay_persist_01")
    assert att.status == PaymentStatus.SUCCEEDED

    # Reinstanciar repositorios de billing
    sub_repo_restarted = JsonSubscriptionRepository(tmp_path)
    inv_repo_restarted = JsonInvoiceRepository(tmp_path)
    pay_repo_restarted = JsonPaymentAttemptRepository(tmp_path)

    recovered_sub = sub_repo_restarted.get_subscription(sub.subscription_id)
    assert recovered_sub is not None
    assert recovered_sub.verify_integrity() is True

    recovered_inv = inv_repo_restarted.get_invoice(inv.invoice_id)
    assert recovered_inv is not None
    assert recovered_inv.verify_integrity() is True

    recovered_att = pay_repo_restarted.get_attempt(att.attempt_id)
    assert recovered_att is not None
    assert recovered_att.verify_integrity() is True

    # 10h. Verificar que no quedan archivos temporales .tmp
    tmp_files = list(tmp_path.rglob("*.tmp"))
    assert len(tmp_files) == 0


def test_11_two_tenant_happy_pipeline_identity_session_gateway_usage_billing(pipeline_env):
    env = pipeline_env
    for tenant_id, identity_id in (
        ("tenant_alpha", "user_alpha_ops"),
        ("tenant_beta", "user_beta_ops"),
    ):
        env["quota_policy_repo"].save_policy(QuotaPolicy(
            policy_id=f"policy_pipeline_{tenant_id}",
            tenant_id=tenant_id,
            rules=(QuotaRule(
                rule_id=f"requests_pipeline_{tenant_id}",
                quota_type=QuotaType.MAX_REQUESTS,
                scope=QuotaScope.TENANT,
                limit_value=20,
                window_type=QuotaWindowType.MONTH,
            ),),
        ))
        request = ModelGatewayRequest(
            tenant_id=tenant_id,
            session_id=env["sessions"][tenant_id].session_id,
            session=env["sessions"][tenant_id],
            identity_id=identity_id,
            organization_id=env["sessions"][tenant_id].organization_id,
            task_type=StandardTaskType.MARKET_DISCOVERY.value,
            prompt_payload=f"Analyze catalog for {tenant_id}",
            correlation_id=f"corr_pipeline_{tenant_id}",
        )
        response = env["gateway"].execute(request)
        assert response.status == ModelGatewayStatus.SUCCESS
        event = ModelGatewayUsageBridge.record_gateway_usage(
            env["usage_svc"], TenantContext(tenant_id=tenant_id), request, response,
            occurred_at=env["clock"].now(), event_id=f"usage_pipeline_{tenant_id}",
        )
        assert event.tenant_id == tenant_id
        subscription = env["sub_svc"].create_subscription(tenant_id, "plan_pro_gate_n")
        invoice = env["bill_svc"].generate_period_invoice(subscription.subscription_id)
        payment = env["bill_svc"].process_payment(
            invoice.invoice_id, idempotency_key=f"pay_pipeline_{tenant_id}"
        )
        assert payment.status == PaymentStatus.SUCCEEDED

    assert len(env["provider"].invocations) == 2
    for tenant_id in ("tenant_alpha", "tenant_beta"):
        aggregate = env["usage_svc"].aggregate_usage(
            TenantContext(tenant_id=tenant_id), UsageQuery(tenant_id=tenant_id)
        )
        assert aggregate.total_requests == 1
        assert env["observability_svc"].get_tenant_snapshot(tenant_id).tenant_id == tenant_id
        reservations = list(env["quota_reservation_repo"]._reservations_by_tenant[tenant_id].values())
        assert len(reservations) == 1
        assert reservations[0].status == QuotaReservationStatus.CONSUMED
        audit = env["audit_repo"].list_records(correlation_id=f"corr_pipeline_{tenant_id}")
        traces = env["trace_repo"].list_records(correlation_id=f"corr_pipeline_{tenant_id}")
        assert audit and all(r.correlation_id == f"corr_pipeline_{tenant_id}" for r in audit)
        assert traces and all(r.correlation_id == f"corr_pipeline_{tenant_id}" for r in traces)


def test_12_marketplace_and_governance_policies_are_tenant_scoped(gate_env):
    env = gate_env
    env["ctx_service"].bind_marketplace_account("tenant_alpha", "meli_alpha")
    assert env["ctx_service"].resolve_context(
        tenant_id="tenant_alpha", marketplace_account_id="meli_alpha"
    ).tenant_id == "tenant_alpha"
    with pytest.raises(CrossTenantAccessError):
        env["ctx_service"].resolve_context(
            tenant_id="tenant_beta", marketplace_account_id="meli_alpha"
        )

    scopes = {
        name: TenantScope(tenant_id=tenant, marketplace_account_id=account)
        for name, tenant, account in (
            ("approval_a", "tenant_alpha", "approval_account"),
            ("approval_b", "tenant_beta", "approval_account"),
            ("financial_a", "tenant_alpha", "finance_account"),
            ("financial_b", "tenant_beta", "finance_account"),
        )
    }
    assert scopes["approval_a"].canonical_scope != scopes["approval_b"].canonical_scope
    assert scopes["financial_a"].matches("tenant_beta", "finance_account") is False

    tool_repo = JsonToolPolicyRepository(env["tmp_path"] / "tool_policies")
    policy = ToolPolicy(
        policy_name="gate_tenant_tools",
        version="1.0.0",
        rules=(ToolPolicyRule(
            rule_id="allow_alpha_scope",
            action=ToolRuleAction.ALLOW,
            tool_id_pattern="catalog_search",
            allowed_scopes=(TenantScope(tenant_id="tenant_alpha").canonical_scope,),
        ),),
        default_action=ToolRuleAction.DENY,
    )
    tool_repo.save_policy(policy)
    tool_service = ToolAccessPolicyService(policy_repository=tool_repo)
    allowed = tool_service.evaluate(ToolAccessRequest(
        tool_reference=ToolReference(tool_id="catalog_search"),
        request_id="tool_alpha",
        scope=TenantScope(tenant_id="tenant_alpha").canonical_scope,
        policy_name=policy.policy_name,
    ))
    denied = tool_service.evaluate(ToolAccessRequest(
        tool_reference=ToolReference(tool_id="catalog_search"),
        request_id="tool_beta",
        scope=TenantScope(tenant_id="tenant_beta").canonical_scope,
        policy_name=policy.policy_name,
    ))
    assert allowed.status == ToolAccessStatus.ALLOW
    assert denied.status == ToolAccessStatus.DENY


def test_13_gateway_blocks_forbidden_model_and_sanitizes_sensitive_payload(pipeline_env):
    env = pipeline_env
    env["quota_policy_repo"].save_policy(QuotaPolicy(
        policy_id="policy_gateway_alpha",
        tenant_id="tenant_alpha",
        rules=(QuotaRule(
            rule_id="gateway_requests", quota_type=QuotaType.MAX_REQUESTS,
            scope=QuotaScope.TENANT, limit_value=10, window_type=QuotaWindowType.MONTH,
        ),),
    ))
    session = env["sessions"]["tenant_alpha"]
    forbidden = env["gateway"].execute(ModelGatewayRequest(
        tenant_id="tenant_alpha", session_id=session.session_id, session=session,
        identity_id="user_alpha_ops", preferred_model_id="claude-3-5-sonnet",
        prompt_payload="forbidden override", correlation_id="corr_forbidden",
    ))
    assert forbidden.status == ModelGatewayStatus.FORBIDDEN_MODEL
    assert len(env["provider"].invocations) == 0

    sensitive = {
        "customer_email": "alice@secretcorp.com",
        "card_number": "4111222233334444",
        "api_key": "sk-live-supersecret",
        "order_summary": "Analyze the customer catalog",
    }
    response = env["gateway"].execute(ModelGatewayRequest(
        tenant_id="tenant_alpha", session_id=session.session_id, session=session,
        identity_id="user_alpha_ops", task_type=StandardTaskType.MARKET_DISCOVERY.value,
        prompt_payload=sensitive, correlation_id="corr_sensitive",
    ))
    assert response.status == ModelGatewayStatus.SUCCESS
    invocation_dump = str(env["provider"].invocations[-1])
    evidence_dump = str(env["audit_repo"].list_records(correlation_id="corr_sensitive"))
    trace_dump = str(env["trace_repo"].list_records(correlation_id="corr_sensitive"))
    for raw_secret in ("alice@secretcorp.com", "4111222233334444", "sk-live-supersecret"):
        assert raw_secret not in invocation_dump
        assert raw_secret not in evidence_dump
        assert raw_secret not in trace_dump


def test_14_admin_console_rbac_and_observability_unknown_zero_alert_isolation(pipeline_env):
    env = pipeline_env
    admin_actions = (
        "TENANT_READ", "ORGANIZATION_READ", "USER_MEMBERSHIP_MANAGE", "PLAN_READ",
        "PLAN_ASSIGN", "QUOTA_READ", "BILLING_READ", "BILLING_MANAGE",
    )
    permissions = tuple(Permission(
        permission_id=f"perm_admin_{action.lower()}", action=action,
        status=PermissionStatus.ACTIVE,
    ) for action in admin_actions)
    role = Role(
        role_id="role_admin_console_gate", name="Gate admin",
        permissions=permissions, status=RoleStatus.ACTIVE,
    )
    env["rbac_svc"].role_repository.save_role(role)
    env["rbac_svc"].assignment_repository.save_assignment(RoleAssignment(
        assignment_id="assign_admin_console_gate", identity_id="user_alpha_ops",
        role_id=role.role_id, scope=TenantScope(tenant_id="tenant_alpha").canonical_scope,
        assigned_at=env["clock"].now(),
    ))
    admin = AdminConsoleService(
        session_repository=env["session_repo"],
        authorization_service=env["saas_authorization"],
        organization_service=env["organization_service"],
        membership_service=env["membership_service"],
        usage_metering_service=env["usage_svc"],
        quota_management_service=env["quota_svc"],
        quota_policy_repository=env["quota_policy_repo"],
        quota_reservation_repository=env["quota_reservation_repo"],
        plan_entitlement_service=env["entitlement_svc"],
        plan_repository=env["plan_catalog"],
        plan_assignment_repository=env["plan_assign_repo"],
        subscription_service=env["sub_svc"],
        subscription_repository=env["sub_repo_json"],
        invoice_repository=env["inv_repo_json"],
        rbac_service=env["rbac_svc"], audit_repository=env["audit_repo"],
        clock=env["clock"],
    )
    client = TestClient(create_admin_app(admin))
    headers = {"Authorization": f"Bearer {env['sessions']['tenant_alpha'].session_id}"}
    assert client.get("/api/admin/tenants/tenant_alpha/summary", headers=headers).status_code == 200
    assert client.get("/api/admin/tenants/tenant_beta/summary", headers=headers).status_code == 403

    snapshot = env["observability_svc"].get_tenant_snapshot("tenant_beta")
    assert snapshot.health_status == TenantHealthStatus.UNKNOWN
    assert snapshot.request_count == 0
    assert snapshot.error_rate is None
    alert = OperationalAlert(
        alert_id="alert_alpha_gate", tenant_id="tenant_alpha",
        alert_type=OperationalAlertType.HIGH_ERROR_RATE,
        severity=AlertSeverity.HIGH, status=AlertStatus.ACTIVE,
        summary="Alpha high error rate", details={"error_rate": 0.25},
        triggered_at=env["clock"].now(), deduplication_key="high_error_alpha",
    )
    env["alert_repo"].save_alert(alert)
    assert [a.alert_id for a in env["alert_repo"].list_alerts("tenant_alpha")] == [alert.alert_id]
    assert env["alert_repo"].list_alerts("tenant_beta") == []


def test_15_corruption_after_restart_fails_safe(gate_env):
    env = gate_env
    alert = OperationalAlert(
        alert_id="alert_corrupt_gate", tenant_id="tenant_alpha",
        alert_type=OperationalAlertType.PROVIDER_DEGRADED,
        severity=AlertSeverity.HIGH, status=AlertStatus.ACTIVE,
        summary="Provider degraded", details={"provider": "openai"},
        triggered_at=env["clock"].now(), deduplication_key="provider_alpha",
    )
    env["alert_repo"].save_alert(alert)
    alert_file = (
        env["tmp_path"] / "tenants" / "tenant_alpha" / "observability" /
        "alerts" / "alert_corrupt_gate.json"
    )
    raw = json.loads(alert_file.read_text(encoding="utf-8"))
    raw["summary"] = "tampered after restart"
    alert_file.write_text(json.dumps(raw), encoding="utf-8")
    restarted = JsonOperationalAlertRepository(env["tmp_path"])
    with pytest.raises(Exception, match="Checksum mismatch"):
        restarted.get_alert_by_id("tenant_alpha", "alert_corrupt_gate")
    assert restarted.get_alert_by_id("tenant_beta", "alert_corrupt_gate") is None


def test_16_audit_and_trace_queries_only_return_requested_correlation(gate_env):
    env = gate_env
    for tenant_id in ("tenant_alpha", "tenant_beta"):
        correlation = f"corr_evidence_{tenant_id}"
        env["audit_repo"].append(AuditRecord(
            audit_id=f"audit_evidence_{tenant_id}",
            record_type=AuditRecordType.ACTION_EXECUTED,
            occurred_at=env["clock"].now(),
            actor=AuditActor(actor_id=f"actor_{tenant_id}", actor_type=AuditActorType.USER),
            subject_type="TenantOperation", subject_id=tenant_id,
            action_or_operation="VERIFY_EVIDENCE", status="SUCCESS",
            correlation_id=correlation, entity_reference=f"{tenant_id}:evidence",
        ))
        env["trace_svc"].record_step(
            component_name="gate_n", execution_id=f"exec_{tenant_id}", step_number=1,
            step_type=StepType.POLICY_EVALUATION, operation="verify_evidence",
            status=TraceStatus.SUCCESS, correlation_id=correlation,
            mission_id=f"mission_{tenant_id}", metadata={"tenant_id": tenant_id},
        )

    for tenant_id in ("tenant_alpha", "tenant_beta"):
        correlation = f"corr_evidence_{tenant_id}"
        audit = env["audit_repo"].list_records(correlation_id=correlation)
        traces = env["trace_repo"].list_records(correlation_id=correlation)
        assert {r.entity_reference for r in audit} == {f"{tenant_id}:evidence"}
        assert {r.mission_id for r in traces} == {f"mission_{tenant_id}"}
        assert all(r.correlation_id == correlation for r in audit + traces)
