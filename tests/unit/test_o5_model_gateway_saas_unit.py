"""
Tests unitarios para Model Gateway SaaS (Hito O.5 — SaaS / Platformization).

Cubre exhaustivamente los 16 requisitos canónicos:
1. valid tenant inference
2. missing tenant blocked
3. unauthorized session blocked
4. tenant provider restriction enforced
5. tenant credential isolation
6. same prompt cross-tenant no cache hit
7. M.5 task selection reused
8. M.1 route reused
9. M.2 budget enforced
10. M.3 compression reused
11. M.6 cost policy reused
12. sensitive data minimized
13. forbidden model caller override rejected
14. provider error normalized
15. token/cost facts emitted
16. no O.6/O.7 implementation (fact emission without usage aggregation/quota)
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from typing import Dict, List, Optional, Any, Tuple, Sequence, Mapping

# Identity, Session & RBAC
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.organization.models import (
    Organization,
    UserMembership,
    MembershipStatus,
    MembershipRole,
    OrganizationStatus,
)
from src.domain.organization.ports import OrganizationRepositoryPort, MembershipRepositoryPort
from src.domain.tenant.models import TenantContext
from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
    PermissionStatus,
    RoleStatus,
)
from src.domain.rbac.ports import RoleRepositoryPort, RoleAssignmentRepositoryPort
from src.application.rbac.rbac_service import RBACService
from src.domain.authorization.models import AuthorizationRequest, AuthorizationDecision
from src.application.authorization.authorization_service import AuthorizationService
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule
from src.domain.session.ports import SaaSSessionRepositoryPort
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
)
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.domain.reliability.ports import ClockPort

# M.* Imports
from src.domain.model_routing.models import (
    ModelRoute,
    RoutingRequest,
    RoutingDecision,
    RoutingDecisionStatus,
    RouteCapability,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RouteStatus,
)
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry

from src.domain.model_selection.models import (
    StandardTaskType,
    TaskModelProfile,
    TaskComplexity,
    TaskSelectionRequest,
    ModelSelectionResult,
    SelectionStatus,
)
from src.application.model_selection.model_selection_service import (
    ModelSelectionByTaskService,
    DefaultTaskSelectionPolicyProvider,
)

from src.domain.context_budget.models import (
    ContextBudgetRequest,
    ContextBudgetDecision,
    ContextBudgetStatus,
    InputTokensBreakdown,
)
from src.application.context_budget.context_budget_service import ContextBudgetService

from src.domain.prompt_compression.models import (
    ContextItem,
    ContextComponentType,
    PriorityLevel,
    CompressionRequest,
    CompressionResult,
    CompressionStatus,
)
from src.application.prompt_compression.deterministic_compressor import DeterministicPromptCompressor

from src.domain.caching.models import (
    CacheLookupRequest,
    CacheLookupResult,
    CacheLookupStatus,
    CacheStoreRequest,
)
from src.domain.caching.ports import CacheRepositoryPort
from src.application.caching.inference_cache_service import InferenceCacheService
from src.infrastructure.persistence.data.in_memory.cache_repository import InMemoryCacheRepository

from src.domain.cost_aware_policy.models import (
    CostAwareRequest,
    CostAwareDecision,
    CostAwareDecisionStatus,
)
from src.application.cost_aware_policy.cost_aware_decision_service import CostAwareDecisionService
from src.domain.cost.models import PricingRate
from src.application.cost.pricing_catalog import InMemoryPricingCatalog

# N.* Imports
from src.domain.secrets.models import (
    SecretMetadata,
    SecretReference,
    SecretValue,
    SecretType,
    SecretStatus,
    SecretResolutionStatus,
    SecretResolutionResult,
)
from src.domain.secrets.ports import SecretResolverPort
from src.domain.security.sensitive_data_models import (
    DataClassification,
    DataHandlingPurpose,
    DataHandlingRequest,
    DataHandlingDecision,
)
from src.application.security.sensitive_data_handling_service import SensitiveDataHandlingService

from src.domain.tool_policy.models import (
    ToolAccessRequest,
    ToolAccessDecision,
    ToolAccessStatus,
)
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService

# K.* Imports
from src.domain.audit.models import AuditRecord, AuditRecordType
from src.domain.audit.ports import AuditRepositoryPort
from src.application.agent_trace.agent_trace_service import AgentTraceService

# O.5 Imports
from src.domain.model_gateway.models import (
    ModelGatewayStatus,
    ProviderErrorType,
    TenantModelConfig,
    ModelGatewayRequest,
    ModelGatewayResponse,
    ProviderRequestReference,
    ModelGatewayProviderError,
)
from src.domain.model_gateway.ports import (
    TenantModelConfigRepositoryPort,
    ModelProviderPort,
)
from src.application.model_gateway.model_gateway_service import ModelGatewayService


# --- IN-MEMORY & FAKE IMPLEMENTATIONS ---

class FakeClock(ClockPort):
    def __init__(self, current_time: datetime):
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        self._current = current_time

    def now(self) -> datetime:
        return self._current

    def set_time(self, new_time: datetime) -> None:
        if new_time.tzinfo is None:
            new_time = new_time.replace(tzinfo=timezone.utc)
        self._current = new_time

    def sleep(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


class InMemoryTenantModelConfigRepository(TenantModelConfigRepositoryPort):
    def __init__(self):
        self._configs: Dict[str, TenantModelConfig] = {}

    def get_config(self, tenant_id: str) -> Optional[TenantModelConfig]:
        return self._configs.get(tenant_id)

    def save_config(self, config: TenantModelConfig) -> None:
        self._configs[config.tenant_id] = config


class InMemorySaaSSessionRepo:
    def __init__(self):
        self._sessions: Dict[str, SaaSSession] = {}

    def get_by_id(self, session_id: str) -> Optional[SaaSSession]:
        return self._sessions.get(session_id)

    def save(self, session: SaaSSession) -> None:
        self._sessions[session.session_id] = session


class InMemoryOrgRepo:
    def __init__(self):
        self._orgs: Dict[str, Organization] = {}

    def get_by_id(self, org_id: str) -> Optional[Organization]:
        return self._orgs.get(org_id)

    def save(self, org: Organization) -> None:
        self._orgs[org.organization_id] = org


class InMemoryMembershipRepo(MembershipRepositoryPort):
    def __init__(self):
        self._memberships: Dict[Tuple[str, str, str], UserMembership] = {}

    def save(self, context: TenantContext, membership: UserMembership) -> None:
        key = (membership.tenant_id, membership.organization_id, membership.identity_id)
        self._memberships[key] = membership

    def get_by_id(self, context: TenantContext, membership_id: str) -> Optional[UserMembership]:
        for m in self._memberships.values():
            if m.membership_id == membership_id and m.tenant_id == context.tenant_id:
                return m
        return None

    def get_by_identity_and_org(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
    ) -> Optional[UserMembership]:
        return self._memberships.get((context.tenant_id, organization_id, identity_id))

    def list_by_organization(self, context: TenantContext, organization_id: str) -> List[UserMembership]:
        return [
            m for m in self._memberships.values()
            if m.tenant_id == context.tenant_id and m.organization_id == organization_id
        ]

    def list_by_identity(self, context: TenantContext, identity_id: str) -> List[UserMembership]:
        return [
            m for m in self._memberships.values()
            if m.tenant_id == context.tenant_id and m.identity_id == identity_id
        ]

    def delete(self, context: TenantContext, membership_id: str) -> bool:
        for k, m in list(self._memberships.items()):
            if m.membership_id == membership_id and m.tenant_id == context.tenant_id:
                del self._memberships[k]
                return True
        return False


class InMemoryRoleRepo(RoleRepositoryPort):
    def __init__(self):
        self._roles: Dict[str, Role] = {}
        self._permissions: Dict[str, Permission] = {}

    def save_role(self, role: Role) -> Role:
        self._roles[role.role_id] = role
        return role

    def get_role(self, role_id: str) -> Optional[Role]:
        return self._roles.get(role_id)

    def list_roles(self, limit: int = 100) -> Sequence[Role]:
        return list(self._roles.values())[:limit]

    def exists(self, role_id: str) -> bool:
        return role_id in self._roles

    def delete_role(self, role_id: str) -> bool:
        return self._roles.pop(role_id, None) is not None

    def save_permission(self, permission: Permission) -> Permission:
        self._permissions[permission.permission_id] = permission
        return permission

    def get_permission(self, permission_id: str) -> Optional[Permission]:
        return self._permissions.get(permission_id)

    def list_permissions(self, limit: int = 100) -> Sequence[Permission]:
        return list(self._permissions.values())[:limit]


class InMemoryRoleAssignmentRepo(RoleAssignmentRepositoryPort):
    def __init__(self):
        self._assignments: Dict[str, RoleAssignment] = {}

    def save_assignment(self, assignment: RoleAssignment) -> RoleAssignment:
        self._assignments[assignment.assignment_id] = assignment
        return assignment

    def get_assignment(self, assignment_id: str) -> Optional[RoleAssignment]:
        return self._assignments.get(assignment_id)

    def list_assignments_for_identity(
        self,
        identity_id: str,
        scope: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[RoleAssignment]:
        res = []
        for a in self._assignments.values():
            if a.identity_id == identity_id:
                if scope is None or a.applies_to_scope(scope):
                    res.append(a)
        return res[:limit]

    def list_assignments_for_role(self, role_id: str) -> Sequence[RoleAssignment]:
        return [a for a in self._assignments.values() if a.role_id == role_id]

    def list_all_assignments(self, limit: int = 200) -> Sequence[RoleAssignment]:
        return list(self._assignments.values())[:limit]

    def revoke_assignment(self, assignment_id: str) -> bool:
        return self._assignments.pop(assignment_id, None) is not None


class InMemorySecretResolver(SecretResolverPort):
    def __init__(self):
        self._secrets: Dict[str, SecretValue] = {}

    def register_secret(self, reference_id: str, value: str) -> None:
        self._secrets[reference_id] = SecretValue(raw_value=value)

    def resolve(self, reference: SecretReference) -> SecretResolutionResult:
        if reference.reference_id in self._secrets:
            return SecretResolutionResult(
                status=SecretResolutionStatus.RESOLVED,
                reference=reference,
                secret_value=self._secrets[reference.reference_id],
            )
        return SecretResolutionResult(
            status=SecretResolutionStatus.NOT_FOUND,
            reference=reference,
            error_message="Secret not found in test repository",
        )

    def rotate_secret(
        self,
        reference_id: str,
        new_value: str,
        new_version: Optional[str] = None,
    ) -> SecretMetadata:
        raise NotImplementedError()


class MockModelProvider(ModelProviderPort):
    def __init__(self):
        self.invocations: List[Dict[str, Any]] = []
        self.forced_exception: Optional[Exception] = None
        self.forced_status_code: Optional[int] = None
        self.default_response_text: str = "Inference completed successfully."

    def execute_inference(
        self,
        route: ModelRoute,
        prompt_payload: Any,
        secret: Optional[SecretValue] = None,
        tools: Optional[Sequence[str]] = None,
        temperature: float = 0.0,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.invocations.append({
            "route": route,
            "prompt_payload": prompt_payload,
            "secret": secret,
            "tools": tools,
            "temperature": temperature,
            "metadata": metadata,
        })
        if self.forced_exception:
            raise self.forced_exception
        if self.forced_status_code == 429:
            raise RuntimeError("Provider rate limit exceeded: HTTP 429 Too Many Requests")

        return {
            "content": self.default_response_text,
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "estimated_cost": Decimal("0.0003"),
            "actual_cost": Decimal("0.0003"),
            "provider_reference": ProviderRequestReference(
                provider_name=route.provider,
                model_id=route.model_id,
            ),
        }


# --- TEST FIXTURE HELPER ---

@pytest.fixture
def gateway_env():
    clock = FakeClock(datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc))

    # Repositories & Registries
    tenant_config_repo = InMemoryTenantModelConfigRepository()
    session_repo = InMemorySaaSSessionRepo()
    org_repo = InMemoryOrgRepo()
    membership_repo = InMemoryMembershipRepo()
    role_repo = InMemoryRoleRepo()
    assignment_repo = InMemoryRoleAssignmentRepo()
    route_registry = InMemoryModelRouteRegistry()
    cache_repo = InMemoryCacheRepository()
    pricing_catalog = InMemoryPricingCatalog()
    secret_resolver = InMemorySecretResolver()

    # Base Routes
    route_gpt4o = ModelRoute(
        route_id="route-gpt-4o",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.REASONING, RouteCapability.FUNCTION_CALLING, RouteCapability.STRUCTURED_OUTPUT, RouteCapability.TOOL_USE),
        quality_class=QualityRequirement.SUPERIOR,
        latency_class=LatencyRequirement.NORMAL,
        context_window=128000,
        priority=10,
    )
    route_claude = ModelRoute(
        route_id="route-claude-35",
        provider="anthropic",
        model_id="claude-3-5-sonnet",
        capabilities=(RouteCapability.REASONING, RouteCapability.FUNCTION_CALLING, RouteCapability.STRUCTURED_OUTPUT, RouteCapability.TOOL_USE),
        quality_class=QualityRequirement.SUPERIOR,
        latency_class=LatencyRequirement.NORMAL,
        context_window=200000,
        priority=20,
    )
    route_registry.register_route(route_gpt4o)
    route_registry.register_route(route_claude)

    # Base Pricing Rates
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
            provider="anthropic",
            service_or_model="claude-3-5-sonnet",
            currency="USD",
            input_rate=Decimal("3.00"),
            output_rate=Decimal("15.00"),
            rate_scale=Decimal("1000000"),
        )
    )

    # Core Application Services
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
    inference_cache_service = InferenceCacheService(repository=cache_repo, clock=clock)
    cost_aware_service = CostAwareDecisionService(pricing_catalog=pricing_catalog)
    sensitive_data_service = SensitiveDataHandlingService()
    tool_policy_service = ToolAccessPolicyService()

    provider_adapter = MockModelProvider()
    adapters = {
        "openai": provider_adapter,
        "anthropic": provider_adapter,
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
        clock=clock,
    )

    # Base Permission and Role
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
            allowed_providers=("openai", "anthropic"),
            allowed_models=("gpt-4o", "claude-3-5-sonnet"),
            credential_references={"openai": sec_ref, "anthropic": sec_ref},
            allow_fallback=True,
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
        "route_registry": route_registry,
        "cache_repo": cache_repo,
        "pricing_catalog": pricing_catalog,
        "secret_resolver": secret_resolver,
        "provider_adapter": provider_adapter,
        "gateway_service": gateway_service,
        "setup_tenant": setup_tenant,
    }


# ==============================================================================
# 16 UNIT TESTS
# ==============================================================================

def test_1_valid_tenant_inference(gateway_env):
    """1. Inferencia válida de tenant autorizada ejecuta provider adapter una vez con éxito."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        task_type=StandardTaskType.MARKET_DISCOVERY.value,
        prompt_payload="Analyze competitor pricing for retail e-commerce.",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert res.tenant_id == "tenant-alpha"
    assert res.output_content == "Inference completed successfully."
    assert res.route_used is not None
    assert res.route_used.model_id == "gpt-4o"
    assert len(env["provider_adapter"].invocations) == 1
    assert env["provider_adapter"].invocations[0]["secret"].reveal() == "sk-test-secret"


def test_2_missing_tenant_blocked(gateway_env):
    """2. Petición sin tenant_id o con tenant_id inválido se bloquea sin invocar provider."""
    env = gateway_env

    # 1. Objeto inválido capturado por validación temprana
    with pytest.raises((ValueError, Exception)):
        _ = ModelGatewayRequest(
            tenant_id="",
            session_id="sess-missing",
            prompt_payload="Hello without tenant",
        )

    # 2. Petición con tenant_id no existente / sin contexto en gateway
    req = ModelGatewayRequest(
        tenant_id="tenant-nonexistent",
        session_id="sess-missing",
        prompt_payload="Hello without tenant",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.TENANT_INVALID or res.status == ModelGatewayStatus.UNAUTHORIZED
    assert len(env["provider_adapter"].invocations) == 0


def test_3_unauthorized_session_blocked(gateway_env):
    """3. Sesión no autorizada, expirada o sin permiso MODEL_INFERENCE_EXECUTE se bloquea."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-expired")

    # Expiramos la sesión
    env["clock"].set_time(session.expires_at + timedelta(minutes=5))

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        prompt_payload="Prompt with expired session",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.UNAUTHORIZED
    assert len(env["provider_adapter"].invocations) == 0


def test_4_tenant_provider_restriction_enforced(gateway_env):
    """4. Tenant policy restringe proveedores permitidos. Si ruta viola provider, se descarta/bloquea."""
    env = gateway_env
    session, config, _ = env["setup_tenant"]("tenant-beta", "org-beta", "user-2", "sess-2")

    # Restringir tenant a sólo Anthropic
    restricted_config = TenantModelConfig(
        tenant_id="tenant-beta",
        allowed_providers=("anthropic",),
        allowed_models=("claude-3-5-sonnet",),
        credential_references=config.credential_references,
        allow_fallback=True,
    )
    env["tenant_config_repo"].save_config(restricted_config)

    req = ModelGatewayRequest(
        tenant_id="tenant-beta",
        session_id=session.session_id,
        session=session,
        task_type=StandardTaskType.MARKET_DISCOVERY.value,
        prompt_payload="Analyze task with restricted provider.",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert res.route_used.provider == "anthropic"
    assert res.route_used.model_id == "claude-3-5-sonnet"
    assert env["provider_adapter"].invocations[0]["route"].provider == "anthropic"


def test_5_tenant_credential_isolation(gateway_env):
    """5. Aislamiento de credenciales: Tenant A no puede resolver ni usar credenciales de Tenant B."""
    env = gateway_env
    _, _, sec_ref_a = env["setup_tenant"]("tenant-a", "org-a", "user-a", "sess-a", api_key="sk-secret-tenant-a")
    session_b, _, _ = env["setup_tenant"]("tenant-b", "org-b", "user-b", "sess-b", api_key="sk-secret-tenant-b")

    # Tenant B intenta configurar la referencia de secreto de Tenant A
    malicious_config_b = TenantModelConfig(
        tenant_id="tenant-b",
        allowed_providers=("openai",),
        allowed_models=("gpt-4o",),
        credential_references={"openai": sec_ref_a},  # Cross-tenant pointer!
    )
    env["tenant_config_repo"].save_config(malicious_config_b)

    req = ModelGatewayRequest(
        tenant_id="tenant-b",
        session_id=session_b.session_id,
        session=session_b,
        prompt_payload="Attempt cross-tenant credential access",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.CREDENTIAL_ERROR
    assert res.reason_code == "CROSS_TENANT_CREDENTIAL_BLOCKED"
    assert len(env["provider_adapter"].invocations) == 0


def test_6_same_prompt_cross_tenant_no_cache_hit(gateway_env):
    """6. Mismo prompt entre tenants distintos no produce cross-tenant cache hit."""
    env = gateway_env
    sess_a, _, _ = env["setup_tenant"]("tenant-a", "org-a", "user-a", "sess-a")
    sess_b, _, _ = env["setup_tenant"]("tenant-b", "org-b", "user-b", "sess-b")

    common_prompt = "Explain quantum computing in three words."

    # 1. Tenant A llama -> MISS -> STORE
    req_a = ModelGatewayRequest(
        tenant_id="tenant-a",
        session_id=sess_a.session_id,
        session=sess_a,
        prompt_payload=common_prompt,
    )
    res_a = env["gateway_service"].execute(req_a)
    assert res_a.status == ModelGatewayStatus.SUCCESS
    assert res_a.cache_status == CacheLookupStatus.MISS
    assert len(env["provider_adapter"].invocations) == 1

    # 2. Tenant B llama con idéntico prompt -> MISS (no hit from tenant A)
    req_b = ModelGatewayRequest(
        tenant_id="tenant-b",
        session_id=sess_b.session_id,
        session=sess_b,
        prompt_payload=common_prompt,
    )
    res_b = env["gateway_service"].execute(req_b)
    assert res_b.status == ModelGatewayStatus.SUCCESS
    assert res_b.cache_status == CacheLookupStatus.MISS
    assert len(env["provider_adapter"].invocations) == 2

    # 3. Tenant A repite -> HIT
    res_a2 = env["gateway_service"].execute(req_a)
    assert res_a2.status == ModelGatewayStatus.CACHED
    assert res_a2.cache_status == CacheLookupStatus.HIT
    assert len(env["provider_adapter"].invocations) == 2  # No new invocation


def test_7_m5_task_selection_reused(gateway_env):
    """7. M.5 Task Selection es reutilizada para derivar requerimientos y perfil de modelo."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        task_type=StandardTaskType.MARKET_DISCOVERY.value,
        prompt_payload="Discovery prompt",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert res.route_used.model_id == "gpt-4o"


def test_8_m1_route_reused(gateway_env):
    """8. M.1 Model Routing Strategy es reutilizada con filtrado determinista de rutas de tenant."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        preferred_model_id="claude-3-5-sonnet",
        prompt_payload="Route reuse test",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert res.route_used.model_id == "claude-3-5-sonnet"


def test_9_m2_budget_enforced(gateway_env):
    """9. M.2 Context Budgeting evalúa y bloquea si el contexto excede sin poder ser comprimido."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    # Creamos un contexto masivo protegido que excede el límite
    massive_context = tuple(
        ContextItem(
            item_id=f"massive_sys_{i}",
            component_type=ContextComponentType.SYSTEM_INSTRUCTIONS,
            content="Protected massive system directive " * 100,
            token_count=10000,
            priority=PriorityLevel.PROTECTED,
        )
        for i in range(15)
    )

    # Restringimos el tenant a un max_budget_tokens estricto
    tenant_cfg = env["tenant_config_repo"].get_config("tenant-alpha")
    strict_cfg = TenantModelConfig(
        tenant_id="tenant-alpha",
        allowed_providers=tenant_cfg.allowed_providers,
        allowed_models=tenant_cfg.allowed_models,
        credential_references=tenant_cfg.credential_references,
        max_budget_tokens=5000,
    )
    env["tenant_config_repo"].save_config(strict_cfg)

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        prompt_payload="Massive payload",
        context_items=massive_context,
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.OVER_BUDGET
    assert len(env["provider_adapter"].invocations) == 0


def test_10_m3_compression_reused(gateway_env):
    """10. M.3 Deterministic Compression se activa ante OVER_BUDGET y permite inferencia exitosa."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    # Contexto comprimible (unprotected)
    compressible_items = tuple(
        ContextItem(
            item_id=f"history_{i}",
            component_type=ContextComponentType.CONVERSATION_HISTORY,
            content="Volatile chat turn history detailed context line " * 20,
            token_count=500,
            priority=PriorityLevel.LOW_PRIORITY,
        )
        for i in range(10)
    )

    tenant_cfg = env["tenant_config_repo"].get_config("tenant-alpha")
    strict_cfg = TenantModelConfig(
        tenant_id="tenant-alpha",
        allowed_providers=tenant_cfg.allowed_providers,
        allowed_models=tenant_cfg.allowed_models,
        credential_references=tenant_cfg.credential_references,
        max_budget_tokens=2000,
    )
    env["tenant_config_repo"].save_config(strict_cfg)

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        prompt_payload="Short prompt requiring compression of history",
        context_items=compressible_items,
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert len(env["provider_adapter"].invocations) == 1


def test_11_m6_cost_policy_reused(gateway_env):
    """11. M.6 Cost-aware Decision Policy evalúa el coste estimado contra el límite del tenant."""
    env = gateway_env
    session, tenant_cfg, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    # Establecer un límite de costo ínfimo
    low_cost_cfg = TenantModelConfig(
        tenant_id="tenant-alpha",
        allowed_providers=tenant_cfg.allowed_providers,
        allowed_models=tenant_cfg.allowed_models,
        credential_references=tenant_cfg.credential_references,
        max_cost_limit=Decimal("0.000000001"),
    )
    env["tenant_config_repo"].save_config(low_cost_cfg)

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        prompt_payload="This request will exceed the micro cost budget limit.",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.COST_REJECTED
    assert len(env["provider_adapter"].invocations) == 0


def test_12_sensitive_data_minimized(gateway_env):
    """12. N.9 Sensitive Data Handling clasifica y redacta datos sensibles antes de enviarlos al provider."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    prompt_with_pii = {"customer_email": "john.doe@example.com", "phone": "+1-555-0199"}

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        prompt_payload=prompt_with_pii,
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    invoked_payload = env["provider_adapter"].invocations[0]["prompt_payload"]
    assert "john.doe@example.com" not in str(invoked_payload)


def test_13_forbidden_model_caller_override_rejected(gateway_env):
    """13. Intento de caller de forzar un modelo no permitido por la política del tenant es rechazado."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        preferred_model_id="deepseek-r1-unapproved",  # Not in allowed_models
        prompt_payload="Try to force unapproved model",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.FORBIDDEN_MODEL
    assert len(env["provider_adapter"].invocations) == 0


def test_14_provider_error_normalized(gateway_env):
    """14. Errores del proveedor (timeouts, rate limits, 429) son normalizados sin filtrar secretos."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    # Forzar error 429 en el mock
    env["provider_adapter"].forced_status_code = 429

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        prompt_payload="Trigger provider rate limit",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.PROVIDER_ERROR
    assert res.error_type == ProviderErrorType.RATE_LIMITED


def test_15_token_cost_facts_emitted(gateway_env):
    """15. La respuesta emite facts estructurados de tokens y costo para futuras capas O.6/O.7."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        session_id=session.session_id,
        session=session,
        prompt_payload="Token and cost accounting fact test",
    )
    res = env["gateway_service"].execute(req)

    assert res.status == ModelGatewayStatus.SUCCESS
    assert res.input_tokens == 10
    assert res.output_tokens == 20
    assert res.total_tokens == 30
    assert res.actual_cost == Decimal("0.0003")
    assert res.provider_reference is not None
    assert res.provider_reference.provider_name == "openai"
    assert res.provider_reference.model_id == "gpt-4o"


def test_16_no_o6_o7_implementation(gateway_env):
    """16. Confirma que O.5 emite facts sin realizar agregación de uso (O.6) ni aplicar cuotas/rate limiting SaaS (O.7)."""
    env = gateway_env
    session, _, _ = env["setup_tenant"]("tenant-alpha", "org-alpha", "user-1", "sess-1")

    # Ejecutamos 3 llamadas consecutivas
    for i in range(3):
        req = ModelGatewayRequest(
            tenant_id="tenant-alpha",
            session_id=session.session_id,
            session=session,
            prompt_payload=f"Fact generation iteration {i}",
        )
        res = env["gateway_service"].execute(req)
        assert res.status == ModelGatewayStatus.SUCCESS
        # Los facts se emiten puramente por request
        assert res.total_tokens == 30

    # Verificamos que no existan módulos ni llamadas a tablas de agregación de consumo O.6 / O.7
    assert not hasattr(env["gateway_service"], "meter_usage")
    assert not hasattr(env["gateway_service"], "enforce_quota")
    assert not hasattr(env["gateway_service"], "record_billing_event")
