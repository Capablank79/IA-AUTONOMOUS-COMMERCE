"""
Tests de Integración y E2E para O.5 — Model Gateway SaaS (Multi-Tenant Model Routing & Provider Isolation).
Hito O — SaaS / Platformization.

Escenarios obligatorios:
A. Tenant A authorized + configured provider + own credential -> provider mock called once.
B. Tenant B same prompt -> separate cache/security context (no cross-tenant cache hit).
C. B cannot resolve A credential (strict N.5 isolation).
D. requested model forbidden by tenant policy -> zero provider calls.
E. M.1 fallback allowed -> permitted fallback executes deterministically.
F. fallback provider not tenant-approved -> blocked.
G. over-budget -> compression -> compliant request executes.
H. still over-budget -> zero provider calls.
I. N.9 restricted payload -> minimized/redacted before mock invocation.
J. provider 429 -> normalized rate limit result; no O.7 quota logic.
K. Audit/Trace contains safe metadata only.

E2E O.5 Pipeline:
N.2 Identity -> O.3 Session -> O.4 Authorization -> O.5 Gateway -> M.5 Selection -> M.1 Routing -> M.2 Budget -> M.3 Compression -> M.6 Cost Policy -> M.4 Cache -> N.9 Data Handling -> N.5 Credential Resolution -> Provider Mock -> K.1/K.2/K.3.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from types import SimpleNamespace
import pytest

from src.domain.identity.models import IdentityReference, IdentityType, IdentityStatus
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.tenant.models import TenantContext
from src.domain.organization.models import Organization, OrganizationStatus, UserMembership, MembershipStatus, MembershipRole
from src.domain.rbac.models import Permission, Role, RoleAssignment, PermissionStatus, RoleStatus
from src.domain.saas_authorization.models import SaaSAuthorizationRequest, SaaSAuthorizationDecision, SaaSAuthorizationStatus
from src.domain.model_routing.models import (
    ModelRoute,
    RouteCapability,
    RouteStatus,
    QualityRequirement,
    LatencyRequirement,
    TaskCriticality,
)
from src.domain.model_routing.ports import ModelRouteRegistryPort
from src.domain.model_selection.models import StandardTaskType
from src.domain.secrets.models import SecretReference, SecretType, SecretResolutionResult, SecretResolutionStatus, SecretValue
from src.domain.secrets.ports import SecretResolverPort
from src.domain.security.sensitive_data_models import DataHandlingPurpose, DataClassification
from src.domain.model_gateway.models import (
    ModelGatewayRequest,
    ModelGatewayResponse,
    TenantModelConfig,
    ModelGatewayStatus,
    ProviderErrorType,
    ModelGatewayProviderError,
)
from src.domain.model_gateway.ports import (
    ModelProviderPort,
    TenantModelConfigRepositoryPort,
)
from src.domain.prompt_compression.models import ContextItem, ContextComponentType, PriorityLevel
from src.domain.caching.models import CacheLookupStatus
from src.domain.usage_metering.models import UsageQuery, UsageRequestStatus
from src.domain.plans.models import PlanEntitlementStatus
from src.domain.quota_management.models import QuotaReservationStatus
from src.domain.audit.models import AuditRecordType, AuditRecord, MissionAuditTimeline
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import StepType, TraceStatus, AgentTraceRecord
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.rbac.rbac_service import RBACService
from src.application.authorization.authorization_service import AuthorizationService
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule
from src.application.model_selection.model_selection_service import (
    ModelSelectionByTaskService,
    DefaultTaskSelectionPolicyProvider,
)
from src.domain.cost.models import PricingRate
from src.application.context_budget.context_budget_service import ContextBudgetService
from src.application.prompt_compression.deterministic_compressor import DeterministicPromptCompressor
from src.application.caching.inference_cache_service import InferenceCacheService
from src.infrastructure.persistence.data.in_memory.cache_repository import InMemoryCacheRepository
from src.application.cost_aware_policy.cost_aware_decision_service import CostAwareDecisionService
from src.application.cost.pricing_catalog import InMemoryPricingCatalog
from src.application.security.sensitive_data_handling_service import SensitiveDataHandlingService
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.model_gateway.model_gateway_service import ModelGatewayService
from src.infrastructure.persistence.data.json.usage_event_repository import InMemoryUsageEventRepository


class FakeClock:
    def __init__(self, initial_time: Optional[datetime] = None):
        self._current_time = initial_time or datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def set_time(self, new_time: datetime):
        self._current_time = new_time


class InMemoryTenantModelConfigRepository(TenantModelConfigRepositoryPort):
    def __init__(self):
        self.configs: Dict[str, TenantModelConfig] = {}

    def get_config(self, tenant_id: str) -> Optional[TenantModelConfig]:
        return self.configs.get(tenant_id)

    def save_config(self, config: TenantModelConfig) -> None:
        self.configs[config.tenant_id] = config


class InMemorySaaSSessionRepo:
    def __init__(self):
        self.sessions: Dict[str, SaaSSession] = {}

    def get_by_id(self, session_id: str) -> Optional[SaaSSession]:
        return self.sessions.get(session_id)

    def save(self, session: SaaSSession) -> None:
        self.sessions[session.session_id] = session


class InMemoryOrgRepo:
    def __init__(self):
        self.orgs: Dict[str, Organization] = {}

    def get_by_id(self, tenant_context: TenantContext, org_id: str) -> Optional[Organization]:
        org = self.orgs.get(org_id)
        if org and org.tenant_id == tenant_context.tenant_id:
            return org
        return None

    def save(self, org: Organization) -> None:
        self.orgs[org.organization_id] = org


class InMemoryMembershipRepo:
    def __init__(self):
        self.memberships: Dict[Tuple[str, str, str], UserMembership] = {}

    def get_by_identity_and_org(self, context: TenantContext, organization_id: str, identity_id: str) -> Optional[UserMembership]:
        return self.memberships.get((context.tenant_id, organization_id, identity_id))

    def get_by_user_and_org(self, tenant_context: TenantContext, identity_id: str, org_id: str) -> Optional[UserMembership]:
        return self.get_by_identity_and_org(tenant_context, org_id, identity_id)

    def save(self, tenant_context: TenantContext, membership: UserMembership) -> None:
        self.memberships[(tenant_context.tenant_id, membership.organization_id, membership.identity_id)] = membership


class InMemoryRoleRepo:
    def __init__(self):
        self.roles: Dict[str, Role] = {}

    def get_role(self, role_id: str) -> Optional[Role]:
        return self.roles.get(role_id)

    def save_role(self, role: Role) -> None:
        self.roles[role.role_id] = role


class InMemoryRoleAssignmentRepo:
    def __init__(self):
        self.assignments: List[RoleAssignment] = []

    def get_assignments_for_identity(self, identity_id: str) -> Tuple[RoleAssignment, ...]:
        return tuple(a for a in self.assignments if a.identity_id == identity_id)

    def list_assignments_for_identity(
        self,
        identity_id: str,
        scope: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[RoleAssignment]:
        res = []
        for a in self.assignments:
            if a.identity_id == identity_id:
                if scope is None or a.applies_to_scope(scope):
                    res.append(a)
        return res[:limit]

    def save_assignment(self, assignment: RoleAssignment) -> None:
        self.assignments.append(assignment)


class InMemoryRouteRegistry(ModelRouteRegistryPort):
    def __init__(self, routes: Sequence[ModelRoute]):
        self._routes = {r.route_id: r for r in routes}

    def list_routes(self) -> Tuple[ModelRoute, ...]:
        return tuple(self._routes.values())

    def get_route(self, route_id: str) -> Optional[ModelRoute]:
        return self._routes.get(route_id)


class InMemorySecretResolver(SecretResolverPort):
    def __init__(self):
        self.secrets: Dict[str, str] = {}

    def register_secret(self, ref_id: str, secret_val: str):
        self.secrets[ref_id] = secret_val

    def resolve(self, reference: SecretReference) -> SecretResolutionResult:
        if reference.reference_id in self.secrets:
            val_str = self.secrets[reference.reference_id]
            return SecretResolutionResult(
                status=SecretResolutionStatus.RESOLVED,
                reference=reference,
                secret_value=SecretValue(val_str),
            )
        return SecretResolutionResult(
            status=SecretResolutionStatus.NOT_FOUND,
            reference=reference,
        )

    def rotate_secret(
        self,
        reference_id: str,
        new_value: str,
        new_version: Optional[str] = None,
    ) -> Any:
        self.secrets[reference_id] = new_value
        return None


class InMemoryAuditRepository(AuditRepositoryPort):
    def __init__(self):
        self.records: List[AuditRecord] = []

    def append(self, record: AuditRecord) -> AuditRecord:
        self.records.append(record)
        return record

    def save(self, record: AuditRecord) -> None:
        self.append(record)

    def get_by_id(self, audit_id: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.audit_id == audit_id:
                return r
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.idempotency_key == idempotency_key:
                return r
        return None

    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        res = self.records
        if correlation_id:
            res = [r for r in res if r.correlation_id == correlation_id]
        if mission_id:
            res = [r for r in res if r.mission_id == mission_id]
        return res[:limit]

    def reconstruct_mission_timeline(self, mission_id: str) -> MissionAuditTimeline:
        matching = [r for r in self.records if r.mission_id == mission_id]
        return MissionAuditTimeline(
            mission_id=mission_id,
            records=tuple(matching),
            total_records=len(matching),
        )

    def find_by_correlation_id(self, correlation_id: str) -> Sequence[AuditRecord]:
        return [r for r in self.records if r.correlation_id == correlation_id]


class InMemoryTraceService:
    def __init__(self):
        self.traces: Dict[str, List[Dict[str, Any]]] = {}

    def record_step(
        self,
        trace_id: str,
        step_name: str,
        step_type: StepType,
        status: TraceStatus,
        metadata: Optional[Mapping[str, Any]] = None,
        duration_ms: Optional[float] = None,
    ) -> None:
        step = {
            "step_id": f"step_{len(self.traces.get(trace_id, [])) + 1}",
            "trace_id": trace_id,
            "step_name": step_name,
            "step_type": step_type,
            "status": status,
            "metadata": dict(metadata or {}),
        }
        if trace_id not in self.traces:
            self.traces[trace_id] = []
        self.traces[trace_id].append(step)


class MockModelProvider(ModelProviderPort):
    def __init__(self, behavior_mode: str = "success"):
        self.behavior_mode = behavior_mode
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
            "secret": secret,
            "tools": tools,
            "metadata": metadata,
        })
        if self.behavior_mode == "rate_limited":
            raise ModelGatewayProviderError(
                error_type=ProviderErrorType.RATE_LIMITED,
                message="Provider upstream 429 Too Many Requests rate limit.",
            )
        if self.behavior_mode == "timeout":
            raise ModelGatewayProviderError(
                error_type=ProviderErrorType.TIMEOUT,
                message="Provider gateway timeout after 30s.",
            )
        if self.behavior_mode == "auth_error":
            raise ModelGatewayProviderError(
                error_type=ProviderErrorType.AUTHENTICATION_ERROR,
                message="Invalid API Key provided to upstream adapter.",
            )

        return {
            "output_content": "Inference completed successfully from integration mock.",
            "output_structured": {"result": "success"},
            "input_tokens": 120,
            "output_tokens": 45,
            "finish_reason": "stop",
        }


@pytest.fixture
def integration_env():
    clock = FakeClock()
    tenant_config_repo = InMemoryTenantModelConfigRepository()
    session_repo = InMemorySaaSSessionRepo()
    org_repo = InMemoryOrgRepo()
    membership_repo = InMemoryMembershipRepo()
    role_repo = InMemoryRoleRepo()
    assignment_repo = InMemoryRoleAssignmentRepo()
    audit_repo = InMemoryAuditRepository()
    trace_service = InMemoryTraceService()
    secret_resolver = InMemorySecretResolver()

    # Routes M.1
    route_openai_gpt4o = ModelRoute(
        route_id="route_gpt_4o",
        provider="openai",
        model_id="gpt-4o",
        context_window=128000,
        quality_class=QualityRequirement.SUPERIOR,
        latency_class=LatencyRequirement.NORMAL,
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
        status=RouteStatus.AVAILABLE,
        priority=1,
    )
    route_openai_gpt4o_mini = ModelRoute(
        route_id="route_gpt_4o_mini",
        provider="openai",
        model_id="gpt-4o-mini",
        context_window=128000,
        quality_class=QualityRequirement.STANDARD,
        latency_class=LatencyRequirement.LOW_LATENCY,
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
        status=RouteStatus.AVAILABLE,
        priority=3,
    )
    route_anthropic_sonnet = ModelRoute(
        route_id="route_claude_35_sonnet",
        provider="anthropic",
        model_id="claude-3-5-sonnet",
        context_window=200000,
        quality_class=QualityRequirement.SUPERIOR,
        latency_class=LatencyRequirement.NORMAL,
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        status=RouteStatus.AVAILABLE,
        priority=2,
    )
    route_gemini_flash = ModelRoute(
        route_id="route_gemini_15_flash",
        provider="google",
        model_id="gemini-1.5-flash",
        context_window=1000000,
        quality_class=QualityRequirement.STANDARD,
        latency_class=LatencyRequirement.LOW_LATENCY,
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        status=RouteStatus.AVAILABLE,
        priority=5,
    )

    route_registry = InMemoryRouteRegistry([
        route_openai_gpt4o,
        route_openai_gpt4o_mini,
        route_anthropic_sonnet,
        route_gemini_flash,
    ])

    pricing_catalog = InMemoryPricingCatalog()
    pricing_catalog.register_rate(
        PricingRate(
            provider="openai",
            service_or_model="gpt-4o",
            currency="USD",
            input_rate=Decimal("5.00"),
            output_rate=Decimal("15.00"),
            rate_scale=Decimal("1000000"),
        )
    )
    pricing_catalog.register_rate(
        PricingRate(
            provider="openai",
            service_or_model="gpt-4o-mini",
            currency="USD",
            input_rate=Decimal("0.15"),
            output_rate=Decimal("0.60"),
            rate_scale=Decimal("1000000"),
        )
    )
    pricing_catalog.register_rate(
        PricingRate(
            provider="anthropic",
            service_or_model="claude-3-5-sonnet",
            currency="USD",
            input_rate=Decimal("3.00"),
            output_rate=Decimal("15.00"),
            rate_scale=Decimal("1000000"),
        )
    )
    pricing_catalog.register_rate(
        PricingRate(
            provider="google",
            service_or_model="gemini-1.5-flash",
            currency="USD",
            input_rate=Decimal("0.075"),
            output_rate=Decimal("0.30"),
            rate_scale=Decimal("1000000"),
        )
    )

    rbac_service = RBACService(
        role_repository=role_repo,
        assignment_repository=assignment_repo,
        clock=clock,
    )
    auth_service = AuthorizationService(
        policy_engine=PolicyEngine(rules=[AuthorizationPolicyRule()]),
        clock=clock,
    )
    saas_auth_service = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        authorization_service=auth_service,
        rbac_service=rbac_service,
        clock=clock,
    )

    routing_strategy = DeterministicModelRoutingStrategy()
    policy_provider = DefaultTaskSelectionPolicyProvider()
    model_selection_service = ModelSelectionByTaskService(
        policy_provider=policy_provider,
        routing_strategy=routing_strategy,
    )
    context_budget_service = ContextBudgetService()
    prompt_compressor = DeterministicPromptCompressor()
    cache_repo = InMemoryCacheRepository()
    inference_cache_service = InferenceCacheService(repository=cache_repo, clock=clock)
    cost_aware_service = CostAwareDecisionService(pricing_catalog=pricing_catalog)
    sensitive_data_service = SensitiveDataHandlingService()
    tool_policy_service = ToolAccessPolicyService()

    provider_adapter = MockModelProvider()
    adapters = {
        "openai": provider_adapter,
        "anthropic": provider_adapter,
        "google": provider_adapter,
        "default": provider_adapter,
    }

    gateway_service = ModelGatewayService(
        session_repository=session_repo,
        saas_authorization_service=saas_auth_service,
        tenant_config_repository=tenant_config_repo,
        model_selection_service=model_selection_service,
        model_routing_strategy=routing_strategy,
        route_registry=route_registry,
        context_budget_service=context_budget_service,
        prompt_compressor=prompt_compressor,
        cost_aware_service=cost_aware_service,
        inference_cache_service=inference_cache_service,
        sensitive_data_service=sensitive_data_service,
        secret_resolver=secret_resolver,
        tool_policy_service=tool_policy_service,
        provider_adapters=adapters,
        audit_repository=audit_repo,
        trace_service=trace_service,
        clock=clock,
    )

    # Permiso Canónico O.4
    inference_perm = Permission(
        permission_id="perm_inference",
        action="MODEL_INFERENCE_EXECUTE",
        description="Allow Model Inference Execution",
        status=PermissionStatus.ACTIVE,
    )
    ai_role = Role(
        role_id="role_ai_user",
        name="AI User Role",
        permissions=(inference_perm,),
        status=RoleStatus.ACTIVE,
    )
    role_repo.save_role(ai_role)

    def setup_tenant(
        tenant_id: str,
        org_id: str,
        identity_id: str,
        session_id: str,
        api_key: str = "sk-test-secret",
        allowed_providers: Tuple[str, ...] = ("openai", "anthropic"),
        allowed_models: Tuple[str, ...] = ("gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet"),
        max_budget_tokens: Optional[int] = None,
        max_cost_limit: Optional[Decimal] = None,
    ):
        secret_id = f"sec_{tenant_id}_openai"
        secret_resolver.register_secret(secret_id, api_key)
        sec_ref = SecretReference(
            reference_id=secret_id,
            provider="openai",
            secret_name=f"openai_api_key_{tenant_id}",
            secret_type=SecretType.API_KEY,
        )

        tenant_config = TenantModelConfig(
            tenant_id=tenant_id,
            allowed_providers=allowed_providers,
            allowed_models=allowed_models,
            credential_references={"openai": sec_ref, "anthropic": sec_ref},
            allow_fallback=True,
            max_budget_tokens=max_budget_tokens,
            max_cost_limit=max_cost_limit,
        )
        tenant_config_repo.save_config(tenant_config)

        org = Organization(
            organization_id=org_id,
            tenant_id=tenant_id,
            name=f"Org {org_id}",
            status=OrganizationStatus.ACTIVE,
            created_at=clock.now(),
            updated_at=clock.now(),
        )
        org_repo.save(org)

        membership = UserMembership(
            membership_id=f"mem_{identity_id}",
            tenant_id=tenant_id,
            organization_id=org_id,
            identity_id=identity_id,
            role=MembershipRole.MEMBER,
            status=MembershipStatus.ACTIVE,
            joined_at=clock.now(),
        )
        t_ctx = TenantContext(tenant_id=tenant_id)
        membership_repo.save(t_ctx, membership)

        assignment = RoleAssignment(
            assignment_id=f"assign_{identity_id}",
            role_id=ai_role.role_id,
            identity_id=identity_id,
        )
        assignment_repo.save_assignment(assignment)

        session = SaaSSession(
            session_id=session_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            organization_id=org_id,
            status=SessionStatus.ACTIVE,
            created_at=clock.now(),
            expires_at=clock.now() + timedelta(hours=8),
        )
        session_repo.save(session)

        return session, tenant_config, sec_ref

    return {
        "clock": clock,
        "tenant_config_repo": tenant_config_repo,
        "session_repo": session_repo,
        "org_repo": org_repo,
        "membership_repo": membership_repo,
        "role_repo": role_repo,
        "assignment_repo": assignment_repo,
        "audit_repo": audit_repo,
        "trace_service": trace_service,
        "secret_resolver": secret_resolver,
        "provider_adapter": provider_adapter,
        "gateway_service": gateway_service,
        "setup_tenant": setup_tenant,
        "adapters": adapters,
        "route_registry": route_registry,
    }


# =========================================================================
# ESCENARIOS DE INTEGRACIÓN
# =========================================================================

def test_scenario_a_tenant_a_authorized_success(integration_env):
    """
    Escenario A:
    Tenant A autorizado + provider configurado + propia credencial -> provider mock invocado 1 sola vez.
    """
    env = integration_env
    session, _, _ = env["setup_tenant"]("tenant-a", "org-a", "user-a", "sess-a", api_key="sk-live-a")

    req = ModelGatewayRequest(
        tenant_id="tenant-a",
        session_id=session.session_id,
        session=session,
        task_type=StandardTaskType.MARKET_DISCOVERY.value,
        prompt_payload="Execute e-commerce pricing discovery analysis.",
        correlation_id="corr-scen-a",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert res.tenant_id == "tenant-a"
    assert res.output_content == "Inference completed successfully from integration mock."
    assert res.route_used is not None
    assert res.route_used.model_id == "gpt-4o"
    assert res.route_used.provider == "openai"
    assert len(env["provider_adapter"].invocations) == 1
    assert env["provider_adapter"].invocations[0]["secret"].reveal() == "sk-live-a"


def test_scenario_b_tenant_b_same_prompt_isolated_cache(integration_env):
    """
    Escenario B:
    Tenant B envía el mismo prompt idéntico que Tenant A -> contexto de caché separado (no cross-tenant cache hit).
    """
    env = integration_env
    sess_a, _, _ = env["setup_tenant"]("tenant-a", "org-a", "user-a", "sess-a")
    sess_b, _, _ = env["setup_tenant"]("tenant-b", "org-b", "user-b", "sess-b")

    common_prompt = "Explain semantic cache isolation in two sentences."

    # 1. Tenant A invoca -> MISS -> provider llamado
    req_a = ModelGatewayRequest(
        tenant_id="tenant-a",
        session_id=sess_a.session_id,
        session=sess_a,
        prompt_payload=common_prompt,
    )
    res_a = env["gateway_service"].execute(req_a)
    assert res_a.status == ModelGatewayStatus.SUCCESS
    assert len(env["provider_adapter"].invocations) == 1

    # 2. Tenant B invoca exactamente el mismo prompt -> No debe haber cache hit desde A -> provider llamado de nuevo
    req_b = ModelGatewayRequest(
        tenant_id="tenant-b",
        session_id=sess_b.session_id,
        session=sess_b,
        prompt_payload=common_prompt,
    )
    res_b = env["gateway_service"].execute(req_b)
    assert res_b.status == ModelGatewayStatus.SUCCESS
    assert res_b.cache_status != "HIT"
    assert len(env["provider_adapter"].invocations) == 2

    # 3. Tenant A invoca otra vez -> HIT dentro de su propio scope de tenant
    req_a_repeat = ModelGatewayRequest(
        tenant_id="tenant-a",
        session_id=sess_a.session_id,
        session=sess_a,
        prompt_payload=common_prompt,
    )
    res_a_repeat = env["gateway_service"].execute(req_a_repeat)
    assert res_a_repeat.status == ModelGatewayStatus.CACHED
    assert len(env["provider_adapter"].invocations) == 2


def test_scenario_c_b_cannot_resolve_a_credential(integration_env):
    """
    Escenario C:
    Tenant B no puede resolver credenciales pertenecientes a Tenant A.
    """
    env = integration_env
    _, _, sec_ref_a = env["setup_tenant"]("tenant-a", "org-a", "user-a", "sess-a", api_key="sk-secret-tenant-a")
    session_b, _, _ = env["setup_tenant"]("tenant-b", "org-b", "user-b", "sess-b", api_key="sk-secret-tenant-b")

    # Modificar configuración de Tenant B apuntando a la referencia de Tenant A
    malicious_config_b = TenantModelConfig(
        tenant_id="tenant-b",
        allowed_providers=("openai",),
        allowed_models=("gpt-4o",),
        credential_references={"openai": sec_ref_a},
    )
    env["tenant_config_repo"].save_config(malicious_config_b)

    req = ModelGatewayRequest(
        tenant_id="tenant-b",
        session_id=session_b.session_id,
        session=session_b,
        prompt_payload="Attempt cross-tenant secret usurpation",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.CREDENTIAL_ERROR
    assert res.reason_code == "CROSS_TENANT_CREDENTIAL_BLOCKED"
    assert len(env["provider_adapter"].invocations) == 0


def test_scenario_d_requested_model_forbidden_by_tenant_policy(integration_env):
    """
    Escenario D:
    Modelo solicitado no permitido por la política del tenant -> cero llamadas a provider.
    """
    env = integration_env
    # Tenant solo permite gpt-4o-mini
    session, _, _ = env["setup_tenant"](
        "tenant-strict",
        "org-strict",
        "user-s",
        "sess-s",
        allowed_models=("gpt-4o-mini",),
    )

    # Caller intenta forzar gpt-4o
    req = ModelGatewayRequest(
        tenant_id="tenant-strict",
        session_id=session.session_id,
        session=session,
        preferred_model_id="gpt-4o",
        prompt_payload="Run inference on forbidden model",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.FORBIDDEN_MODEL
    assert res.reason_code == "CALLER_MODEL_OVERRIDE_FORBIDDEN"
    assert len(env["provider_adapter"].invocations) == 0


def test_scenario_e_m1_fallback_allowed_executes(integration_env):
    """
    Escenario E:
    M.1 fallback permitido -> si el modelo preferido no está disponible o no califica, ejecuta fallback permitido.
    """
    env = integration_env
    # Permitimos claude-3-5-sonnet y gpt-4o-mini
    session, _, _ = env["setup_tenant"](
        "tenant-fallback",
        "org-fb",
        "user-fb",
        "sess-fb",
        allowed_providers=("openai", "anthropic"),
        allowed_models=("claude-3-5-sonnet", "gpt-4o-mini"),
    )

    # Tarea CLASSIFICATION requiere menor latencia y calidad estándar -> claude-3-5-sonnet es SUPERIOR/NORMAL, gpt-4o-mini es STANDARD/LOW_LATENCY
    req = ModelGatewayRequest(
        tenant_id="tenant-fallback",
        session_id=session.session_id,
        session=session,
        task_type=StandardTaskType.CLASSIFICATION.value,
        prompt_payload="Fast query requiring low latency route.",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert res.route_used.model_id == "gpt-4o-mini"
    assert len(env["provider_adapter"].invocations) == 1


def test_scenario_f_fallback_provider_not_tenant_approved_blocked(integration_env):
    """
    Escenario F:
    Proveedor fallback no aprobado por la política del tenant -> bloqueado sin llamada upstream.
    """
    env = integration_env
    # Tenant solo permite provider openai (no google/gemini ni anthropic)
    session, _, _ = env["setup_tenant"](
        "tenant-openai-only",
        "org-oo",
        "user-oo",
        "sess-oo",
        allowed_providers=("openai",),
        allowed_models=("gpt-4o",),
    )

    # Tarea de vision/multimodal que solo soportaría google o anthropic si openai estuviera desactivado
    req = ModelGatewayRequest(
        tenant_id="tenant-openai-only",
        session_id=session.session_id,
        session=session,
        preferred_provider="google",  # Prohibido por tenant
        prompt_payload="Inference requiring google provider",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.FORBIDDEN_MODEL
    assert res.reason_code == "CALLER_PROVIDER_OVERRIDE_FORBIDDEN"
    assert len(env["provider_adapter"].invocations) == 0


def test_scenario_g_over_budget_compression_executes(integration_env):
    """
    Escenario G:
    Over-budget -> M.3 prompt compression reduce tokens -> petición comprimida conforme se ejecuta.
    """
    env = integration_env
    session, _, _ = env["setup_tenant"]("tenant-budget", "org-b", "user-b", "sess-b", max_budget_tokens=1000)

    # Construir items de contexto que excedan el límite
    large_context = [
        {"component_type": "RETRIEVED_EVIDENCE", "content": "Evidence " * 200, "token_count": 800},
        {"component_type": "CONVERSATION_HISTORY", "content": "History " * 100, "token_count": 400},
        {"component_type": "USER_INPUT", "content": "Analyze these data points.", "token_count": 50},
    ]

    req = ModelGatewayRequest(
        tenant_id="tenant-budget",
        session_id=session.session_id,
        session=session,
        context_items=tuple(large_context),
        prompt_payload="Execute budget-managed query.",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert len(env["provider_adapter"].invocations) == 1


def test_scenario_h_still_over_budget_zero_provider_calls(integration_env):
    """
    Escenario H:
    Aún con compresión o con ventana excedida inalcanzable -> zero llamadas a provider.
    """
    env = integration_env
    session, _, _ = env["setup_tenant"]("tenant-tiny", "org-t", "user-t", "sess-t", max_budget_tokens=10)

    uncompressible_context = [
        {"component_type": "SYSTEM_INSTRUCTIONS", "content": "Critical instructions " * 50, "token_count": 300},
        {"component_type": "USER_INPUT", "content": "Uncompressible huge prompt", "token_count": 200},
    ]

    req = ModelGatewayRequest(
        tenant_id="tenant-tiny",
        session_id=session.session_id,
        session=session,
        context_items=tuple(uncompressible_context),
        prompt_payload="Will fail budget ceiling",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.OVER_BUDGET
    assert len(env["provider_adapter"].invocations) == 0


def test_scenario_i_sensitive_payload_minimized_redacted(integration_env):
    """
    Escenario I:
    Payload con información restringida o PII es clasificado y redactado por N.9 antes de la invocación.
    """
    env = integration_env
    session, _, _ = env["setup_tenant"]("tenant-sens", "org-s", "user-s", "sess-s")

    sensitive_payload = {
        "customer_name": "Alice Wonderland",
        "customer_email": "alice.wonderland@secretcorp.com",
        "phone": "+1-800-555-0149",
        "card_number": "4111222233334444",
        "order_summary": "Order 100 widgets",
    }

    req = ModelGatewayRequest(
        tenant_id="tenant-sens",
        session_id=session.session_id,
        session=session,
        prompt_payload=sensitive_payload,
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    invoked_payload = env["provider_adapter"].invocations[0]["prompt_payload"]
    invoked_str = str(invoked_payload)
    assert "alice.wonderland@secretcorp.com" not in invoked_str
    assert "4111222233334444" not in invoked_str
    assert "[REDACTED_FINANCIAL]" in invoked_str or "[REDACTED_EMAIL]" in invoked_str or "alice" in invoked_str


def test_scenario_j_provider_429_normalized_no_o7_logic(integration_env):
    """
    Escenario J:
    Error 429 upstream del proveedor es normalizado a RATE_LIMITED; no se ejecuta lógica de cuotas de tenant (O.7).
    """
    env = integration_env
    session, _, _ = env["setup_tenant"]("tenant-ratelimit", "org-rl", "user-rl", "sess-rl")

    rate_limited_mock = MockModelProvider(behavior_mode="rate_limited")
    env["adapters"]["openai"] = rate_limited_mock
    env["adapters"]["default"] = rate_limited_mock
    env["gateway_service"].provider_adapters["openai"] = rate_limited_mock
    env["gateway_service"].provider_adapters["default"] = rate_limited_mock

    req = ModelGatewayRequest(
        tenant_id="tenant-ratelimit",
        session_id=session.session_id,
        session=session,
        prompt_payload="Hit provider experiencing upstream concurrency spike.",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.PROVIDER_ERROR
    assert res.error_type == ProviderErrorType.RATE_LIMITED
    assert "429" in res.error_message or "rate limit" in res.error_message.lower()
    # Confirmar que no hay estado de cuotas de tenant
    assert not hasattr(res, "tenant_quota_remaining")


def test_scenario_k_audit_and_trace_contains_safe_metadata_only(integration_env):
    """
    Escenario K:
    Audit y Trace registran eventos canónicos con metadatos sanitizados sin secretos ni prompts completos.
    """
    env = integration_env
    session, _, _ = env["setup_tenant"]("tenant-audit", "org-aud", "user-aud", "sess-aud", api_key="sk-super-secret-key")

    correlation_id = "corr-audit-safety-check"
    req = ModelGatewayRequest(
        tenant_id="tenant-audit",
        session_id=session.session_id,
        session=session,
        prompt_payload="Confidential enterprise strategy data.",
        correlation_id=correlation_id,
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS

    # 1. Verificar Audit Records
    audit_records = env["audit_repo"].find_by_correlation_id(correlation_id)
    assert len(audit_records) >= 3  # REQUESTED, ROUTE_SELECTED, PROVIDER_INVOKED, COMPLETED
    record_types = [r.record_type for r in audit_records]
    assert AuditRecordType.MODEL_GATEWAY_REQUESTED in record_types
    assert AuditRecordType.MODEL_ROUTE_SELECTED in record_types
    assert AuditRecordType.MODEL_GATEWAY_COMPLETED in record_types

    for r in audit_records:
        meta_str = str(r.metadata)
        assert "sk-super-secret-key" not in meta_str
        assert "Confidential enterprise strategy data" not in meta_str

    # 2. Verificar Trace Steps
    traces = env["trace_service"].traces.get(correlation_id, [])
    assert len(traces) >= 2
    step_names = [s["step_name"] for s in traces]
    assert "MODEL_GATEWAY_STARTED" in step_names
    assert "MODEL_GATEWAY_COMPLETED" in step_names

    for step in traces:
        meta_str = str(step["metadata"])
        assert "sk-super-secret-key" not in meta_str


# =========================================================================
# E2E PIPELINE TEST O.5
# =========================================================================

def test_e2e_o5_full_pipeline_multi_tenant(integration_env):
    """
    E2E O.5 Multi-Tenant Pipeline:
    Demuestra:
    1. Authorized Tenant A -> correct provider/model -> call once.
    2. Tenant B identical request -> isolated credential/cache/context.
    3. Unauthorized tenant/model -> zero calls.
    4. Sensitive context -> provider receives only sanitized data.
    """
    env = integration_env

    # 1. Setup Tenant Alpha y Tenant Beta
    sess_alpha, _, _ = env["setup_tenant"]("tenant-alpha", "org-a", "user-a", "sess-a", api_key="sk-alpha-key")
    sess_beta, _, _ = env["setup_tenant"]("tenant-beta", "org-b", "user-b", "sess-b", api_key="sk-beta-key")

    # Step 1: Inferencia autorizada Tenant Alpha
    req_alpha = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=sess_alpha.session_id,
        session=sess_alpha,
        task_type=StandardTaskType.MARKET_ANALYSIS.value,
        prompt_payload="Generate multi-tenant tenant isolation validator function.",
        correlation_id="e2e-alpha-1",
    )
    res_alpha = env["gateway_service"].execute(req_alpha)
    assert res_alpha.status == ModelGatewayStatus.SUCCESS
    assert res_alpha.route_used.model_id == "gpt-4o"
    assert env["provider_adapter"].invocations[0]["secret"].reveal() == "sk-alpha-key"

    # Step 2: Tenant Beta con request idéntico -> aislamiento estricto
    req_beta = ModelGatewayRequest(
        tenant_id="tenant-beta",
        session_id=sess_beta.session_id,
        session=sess_beta,
        task_type=StandardTaskType.MARKET_ANALYSIS.value,
        prompt_payload="Generate multi-tenant tenant isolation validator function.",
        correlation_id="e2e-beta-1",
    )
    res_beta = env["gateway_service"].execute(req_beta)
    assert res_beta.status == ModelGatewayStatus.SUCCESS
    assert res_beta.cache_status != "HIT"
    assert len(env["provider_adapter"].invocations) == 2
    assert env["provider_adapter"].invocations[1]["secret"].reveal() == "sk-beta-key"

    # Step 3: Tenant no autorizado / sesión expirada -> cero calls
    env["clock"].set_time(sess_alpha.expires_at + timedelta(hours=1))
    req_unauth = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=sess_alpha.session_id,
        session=sess_alpha,
        prompt_payload="Attempt inference with expired session",
    )
    res_unauth = env["gateway_service"].execute(req_unauth)
    assert res_unauth.status == ModelGatewayStatus.UNAUTHORIZED
    assert len(env["provider_adapter"].invocations) == 2  # No increments

    # Step 4: Contexto sensible -> proveedor recibe datos sanitizados
    sess_beta_fresh, _, _ = env["setup_tenant"]("tenant-beta", "org-b", "user-b", "sess-b-fresh", api_key="sk-beta-key")
    req_sens = ModelGatewayRequest(
        tenant_id="tenant-beta",
        session_id=sess_beta_fresh.session_id,
        session=sess_beta_fresh,
        prompt_payload={"user_email": "dev@internal.net", "credit_card": "4532015978451236", "prompt": "Process transaction."},
    )
    res_sens = env["gateway_service"].execute(req_sens)
    assert res_sens.status == ModelGatewayStatus.SUCCESS
    assert len(env["provider_adapter"].invocations) == 3
    last_invoked = env["provider_adapter"].invocations[-1]["prompt_payload"]
    assert "4532015978451236" not in str(last_invoked)
    assert "dev@internal.net" not in str(last_invoked)


class GateNEntitlementStub:
    def __init__(self, status=PlanEntitlementStatus.ALLOW, is_entitled=True):
        self.status = status
        self.is_entitled = is_entitled
        self.requests = []

    def evaluate_entitlement(self, request, context=None):
        self.requests.append((request, context))
        return SimpleNamespace(
            status=self.status,
            is_entitled=self.is_entitled,
            reason_code="GATE_N_ENTITLEMENT",
            rationale="Gate N entitlement decision",
        )


class GateNQuotaStub:
    def __init__(self):
        self.reservations = []
        self.reconciliations = []

    def evaluate_and_reserve(self, request, context=None):
        reservation_id = f"qres_{len(self.reservations) + 1}"
        self.reservations.append((reservation_id, request, context))
        return SimpleNamespace(
            is_allowed=True,
            reservation_id=reservation_id,
            status=SimpleNamespace(value="ALLOW"),
            reason_codes=(),
            rationale="allowed",
        )

    def reconcile_reservation(self, **kwargs):
        self.reconciliations.append(kwargs)
        return SimpleNamespace(status=kwargs["actual_status"])


def _gate_n_request(env, tenant_id="tenant-gate-n"):
    session, _, _ = env["setup_tenant"](tenant_id, "org-gate-n", "user-gate-n", "sess-gate-n")
    return ModelGatewayRequest(
        tenant_id=tenant_id,
        session_id=session.session_id,
        session=session,
        prompt_payload="Gate N governed inference",
        correlation_id=f"corr-{tenant_id}",
    )


def test_gate_n_entitlement_denial_has_zero_provider_calls_and_zero_reservations(integration_env):
    env = integration_env
    request = _gate_n_request(env)
    entitlement = GateNEntitlementStub(status=PlanEntitlementStatus.DENY, is_entitled=False)
    quota = GateNQuotaStub()
    env["gateway_service"].plan_entitlement_service = entitlement
    env["gateway_service"].quota_management_service = quota

    response = env["gateway_service"].execute(request)

    assert response.status == ModelGatewayStatus.FORBIDDEN_MODEL
    assert len(entitlement.requests) == 1
    entitlement_request, context = entitlement.requests[0]
    assert entitlement_request.model_id == response.route_used.model_id
    assert entitlement_request.provider == response.route_used.provider
    assert context.tenant_id == request.tenant_id
    assert env["provider_adapter"].invocations == []
    assert quota.reservations == []


def test_gate_n_success_records_usage_automatically(integration_env):
    env = integration_env
    request = _gate_n_request(env)
    usage_service = UsageMeteringService(repository=InMemoryUsageEventRepository(), clock=env["clock"])
    env["gateway_service"].usage_metering_service = usage_service

    response = env["gateway_service"].execute(request)

    assert response.status == ModelGatewayStatus.SUCCESS
    aggregate = usage_service.aggregate_usage(
        TenantContext(tenant_id=request.tenant_id),
        UsageQuery(tenant_id=request.tenant_id),
    )
    assert aggregate.total_requests == 1
    assert aggregate.successful_requests == 1
    assert aggregate.total_tokens == response.total_tokens


@pytest.mark.parametrize("failure_mode", ["credential", "adapter"])
def test_gate_n_pre_provider_failures_release_reservation(integration_env, failure_mode):
    env = integration_env
    request = _gate_n_request(env)
    quota = GateNQuotaStub()
    env["gateway_service"].quota_management_service = quota
    if failure_mode == "credential":
        env["secret_resolver"].secrets.clear()
    else:
        env["gateway_service"].provider_adapters.clear()

    response = env["gateway_service"].execute(request)

    expected_status = (
        ModelGatewayStatus.CREDENTIAL_ERROR
        if failure_mode == "credential"
        else ModelGatewayStatus.PROVIDER_ERROR
    )
    assert response.status == expected_status
    assert len(quota.reconciliations) == 1
    assert quota.reconciliations[0]["actual_status"] == QuotaReservationStatus.RELEASED
    assert env["provider_adapter"].invocations == []


def test_gate_n_local_exception_after_reservation_releases_it(integration_env):
    env = integration_env
    request = _gate_n_request(env)
    quota = GateNQuotaStub()
    env["gateway_service"].quota_management_service = quota
    env["gateway_service"].sensitive_data_service = SimpleNamespace(
        evaluate=lambda request: (_ for _ in ()).throw(RuntimeError("local pipeline failure"))
    )

    with pytest.raises(RuntimeError, match="local pipeline failure"):
        env["gateway_service"].execute(request)

    assert len(quota.reconciliations) == 1
    assert quota.reconciliations[0]["actual_status"] == QuotaReservationStatus.RELEASED
    assert env["provider_adapter"].invocations == []
