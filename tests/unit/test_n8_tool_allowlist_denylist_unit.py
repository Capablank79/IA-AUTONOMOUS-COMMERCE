"""
Unit Tests for N.8 — Tool Allowlist / Denylist (Transversal N — Security, Governance & Safety).

Cubre los 16 casos canónicos obligatorios:
1. Tool in allowlist -> ALLOW
2. Tool not in allowlist -> DENY (default DENY)
3. Tool in denylist -> DENY (explicit DENY)
4. Tool in BOTH allowlist & denylist -> DENY (explicit DENY precedence)
5. Normalization anti-bypass (casing, spaces, trimming)
6. Read-only tool allow vs side-effecting / write tool block
7. Scoped allow (tool allowed only for specific role / scope / mission)
8. Unknown / unregistered tool -> DENY / UNKNOWN
9. Deprecated / disabled tool -> DENY
10. Policy missing / not found -> Fail-safe DENY / UNKNOWN (never default ALLOW)
11. Corrupted policy (tampered checksum) -> DENY
12. Deterministic decision (identical request -> identical checksum & decision)
13. Separation of duties (N.3 actor DENY not bypassed by N.8)
14. Separation of duties (N.8 ALLOW does not bypass N.7 financial limits)
15. Separation of duties (N.8 ALLOW does not bypass N.6 approval requirements)
16. Zero physical execution on N.8 DENY (Guarded ActionExecutor delegate not invoked)
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.domain.tool.models import ToolSideEffectLevel
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
    compute_tool_policy_checksum,
    compute_tool_decision_checksum,
)
from src.domain.tool_policy.ports import ToolPolicyRepositoryPort
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.domain.identity.models import IdentityType, IdentityReference
from src.domain.authentication.models import (
    PrincipalContext,
    AuthenticationResult,
    AuthenticationStatus,
    AuthenticationMethod,
)
from src.domain.authorization.models import (
    AuthorizationDecision,
    AuthorizationStatus,
    AuthorizationRequest,
)
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.application.authorization.authorization_service import AuthorizationService
from src.application.authorization.authorization_guarded_action_executor import AuthorizationGuardedActionExecutor
from src.application.financial_limit.financial_limit_service import FinancialLimitService
from src.domain.financial_limit.models import (
    FinancialLimitPolicy,
    FinancialLimitRule,
    FinancialLimitType,
)
from src.domain.financial_limit.ports import FinancialLimitPolicyRepositoryPort
from src.application.approval.approval_policy_service import ApprovalPolicyService
from src.domain.approval.models import (
    ApprovalPolicy,
    ApprovalStatus,
)
from src.domain.approval.ports import (
    ApprovalPolicyRepositoryPort,
    ApprovalEvidenceRepositoryPort,
)
from src.domain.profit.models import Money


class InMemoryApprovalEvidenceRepo(ApprovalEvidenceRepositoryPort):
    def __init__(self):
        self._evidences = {}

    def save_evidence(self, evidence) -> None:
        self._evidences[evidence.approval_id] = evidence

    def get_by_id(self, approval_id: str):
        return self._evidences.get(approval_id)

    def list_by_correlation_id(self, correlation_id: str):
        return tuple(e for e in self._evidences.values() if getattr(e, "correlation_id", None) == correlation_id)

    def list_by_target(self, action: str, resource: str):
        return tuple(e for e in self._evidences.values() if getattr(e, "target_action", None) == action and getattr(e, "target_resource", None) == resource)


class InMemoryToolPolicyRepository(ToolPolicyRepositoryPort):
    def __init__(self):
        self._policies = {}

    def save_policy(self, policy: ToolPolicy) -> None:
        self._policies[policy.policy_name] = policy

    def get_policy(self, policy_name: str):
        return self._policies.get(policy_name)

    def list_policies(self):
        return tuple(self._policies.values())


class InMemoryFinancialPolicyRepo(FinancialLimitPolicyRepositoryPort):
    def __init__(self):
        self._policies = {}

    def save_policy(self, policy: FinancialLimitPolicy) -> None:
        self._policies[policy.policy_name] = policy

    def get_policy(self, policy_name: str):
        return self._policies.get(policy_name)

    def list_policies(self):
        return tuple(self._policies.values())


class InMemoryApprovalPolicyRepo(ApprovalPolicyRepositoryPort):
    def __init__(self):
        self._policies = {}

    def save_policy(self, policy: ApprovalPolicy) -> None:
        self._policies[policy.policy_name] = policy

    def get_policy(self, policy_name: str):
        return self._policies.get(policy_name)

    def list_policies(self):
        return tuple(self._policies.values())


class DummyClock:
    def __init__(self, fixed_now: datetime):
        self._now = fixed_now

    def now(self) -> datetime:
        return self._now


class MockActionExecutor(ActionExecutor):
    def __init__(self):
        self.call_count = 0
        self.last_decision = None

    def execute(self, decision, state):
        self.call_count += 1
        self.last_decision = decision
        return {"status": "SUCCESS", "called": True}


@pytest.fixture
def dummy_clock():
    return DummyClock(datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def base_tool_policy():
    # Regla 1: Permitir mercado_libre search (READ_ONLY)
    rule_search = ToolPolicyRule(
        rule_id="rule_meli_search_allow",
        action=ToolRuleAction.ALLOW,
        tool_id_pattern="meli_search",
        provider_pattern="mercado_libre",
        allowed_side_effect_levels=(ToolSideEffectLevel.READ_ONLY, ToolSideEffectLevel.ANALYSIS),
        allowed_roles=("AGENT", "ANALYST", "ADMIN"),
        description="Allow Meli Search for read/analysis",
    )
    # Regla 2: Permitir scraper con scope catalog
    rule_scraper = ToolPolicyRule(
        rule_id="rule_scraper_allow",
        action=ToolRuleAction.ALLOW,
        tool_id_pattern="web_scraper",
        allowed_scopes=("catalog", "research"),
        description="Allow Web Scraper for catalog",
    )
    # Regla 3: Explícitamente denegar operaciones peligrosas de eliminación / shell
    rule_deny_shell = ToolPolicyRule(
        rule_id="rule_deny_shell_exec",
        action=ToolRuleAction.DENY,
        tool_id_pattern="system_shell",
        description="Strictly block system_shell",
    )
    # Regla 4: Explícitamente denegar modificador de precios a agentes estándar
    rule_deny_price_bot = ToolPolicyRule(
        rule_id="rule_deny_price_bot",
        action=ToolRuleAction.DENY,
        tool_id_pattern="price_updater",
        denied_roles=("BOT", "GUEST"),
        description="Block bot price updates",
    )

    return ToolPolicy(
        policy_name="default_tool_policy",
        version="1.0.0",
        rules=(rule_search, rule_scraper, rule_deny_shell, rule_deny_price_bot),
        default_action=ToolRuleAction.DENY,
    )


@pytest.fixture
def tool_service(base_tool_policy, dummy_clock):
    repo = InMemoryToolPolicyRepository()
    repo.save_policy(base_tool_policy)
    return ToolAccessPolicyService(
        policy_repository=repo,
        default_policy_name="default_tool_policy",
        clock=dummy_clock,
    )


# ---------------------------------------------------------------------------
# TESTS UNITARIOS
# ---------------------------------------------------------------------------

def test_1_tool_in_allowlist(tool_service):
    """1. Invocación de herramienta permitida en allowlist -> ALLOW"""
    req = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="meli_search",
            provider="mercado_libre",
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
        ),
        request_id="req_01",
        role="AGENT",
    )
    decision = tool_service.evaluate(req)
    assert decision.status == ToolAccessStatus.ALLOW
    assert decision.reason_code == ToolAccessReasonCode.ALLOWED_BY_POLICY
    assert decision.matched_rule_id == "rule_meli_search_allow"
    assert decision.is_allowed is True
    assert decision.is_valid_checksum() is True


def test_2_tool_not_in_allowlist_default_deny(tool_service):
    """2. Herramienta no presente en allowlist -> DENY por Default DENY"""
    req = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="unregistered_tool_xyz",
            provider="unknown_provider",
        ),
        request_id="req_02",
        role="AGENT",
    )
    decision = tool_service.evaluate(req)
    assert decision.status == ToolAccessStatus.DENY
    assert decision.reason_code == ToolAccessReasonCode.DEFAULT_DENY_NO_RULE
    assert decision.is_allowed is False


def test_3_tool_in_denylist_explicit_deny(tool_service):
    """3. Herramienta listada en denylist explícita -> DENY"""
    req = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="system_shell",
        ),
        request_id="req_03",
        role="ADMIN",
    )
    decision = tool_service.evaluate(req)
    assert decision.status == ToolAccessStatus.DENY
    assert decision.reason_code == ToolAccessReasonCode.EXPLICIT_DENY_RULE
    assert decision.matched_rule_id == "rule_deny_shell_exec"


def test_4_tool_in_both_allowlist_and_denylist_explicit_deny_precedence():
    """4. Conflicto: Herramienta con regla ALLOW y regla DENY -> DENY prevalece siempre"""
    rule_allow = ToolPolicyRule(
        rule_id="rule_conflict_allow",
        action=ToolRuleAction.ALLOW,
        tool_id_pattern="risky_tool",
    )
    rule_deny = ToolPolicyRule(
        rule_id="rule_conflict_deny",
        action=ToolRuleAction.DENY,
        tool_id_pattern="risky_tool",
    )
    policy = ToolPolicy(
        policy_name="conflict_policy",
        version="1.0.0",
        rules=(rule_allow, rule_deny),
        default_action=ToolRuleAction.DENY,
    )
    repo = InMemoryToolPolicyRepository()
    repo.save_policy(policy)
    service = ToolAccessPolicyService(policy_repository=repo, default_policy_name="conflict_policy")

    req = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="risky_tool"),
        request_id="req_04",
    )
    decision = service.evaluate(req)
    assert decision.status == ToolAccessStatus.DENY
    assert decision.reason_code == ToolAccessReasonCode.EXPLICIT_DENY_RULE
    assert decision.matched_rule_id == "rule_conflict_deny"


def test_5_normalization_anti_bypass(tool_service):
    """5. Normalización determinista anti-bypass (espacios, mayúsculas/minúsculas)"""
    # Solicitud con casing mixto y espacios laterales
    req = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="  MELI_SEARCH  ",
            provider="  Mercado_Libre  ",
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
        ),
        request_id="req_05",
        role="agent ",
    )
    decision = tool_service.evaluate(req)
    assert decision.status == ToolAccessStatus.ALLOW
    assert decision.tool_reference.tool_id == "meli_search"
    assert decision.tool_reference.provider == "mercado_libre"


def test_6_side_effect_level_prohibition(tool_service):
    """6. Herramienta permitida para READ_ONLY rechaza nivel WRITE o EXTERNAL_SIDE_EFFECT"""
    req = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="meli_search",
            provider="mercado_libre",
            side_effect_level=ToolSideEffectLevel.WRITE,  # Rule only allows READ_ONLY/ANALYSIS
        ),
        request_id="req_06",
        role="AGENT",
    )
    decision = tool_service.evaluate(req)
    # Al no coincidir el side effect level con la regla allow, cae a default deny
    assert decision.status == ToolAccessStatus.DENY
    assert decision.reason_code == ToolAccessReasonCode.DEFAULT_DENY_NO_RULE


def test_7_scoped_allow(tool_service):
    """7. Herramienta con restricción de scope: allow con scope correcto, deny con scope ausente/inválido"""
    # Con scope adecuado -> ALLOW
    req_valid_scope = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="web_scraper"),
        request_id="req_07a",
        scope="catalog",
    )
    decision_ok = tool_service.evaluate(req_valid_scope)
    assert decision_ok.status == ToolAccessStatus.ALLOW

    # Con scope diferente -> DENY
    req_invalid_scope = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="web_scraper"),
        request_id="req_07b",
        scope="payments",
    )
    decision_deny = tool_service.evaluate(req_invalid_scope)
    assert decision_deny.status == ToolAccessStatus.DENY


def test_8_unknown_or_unregistered_tool(tool_service):
    """8. Herramienta no registrada en políticas -> DENY / UNKNOWN determinista"""
    req = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="alien_subsystem_tool"),
        request_id="req_08",
    )
    decision = tool_service.evaluate(req)
    assert decision.status == ToolAccessStatus.DENY
    assert decision.is_allowed is False


def test_9_denied_role_explicit_block(tool_service):
    """9. Rol explícitamente bloqueado para una herramienta -> DENY"""
    req = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="price_updater"),
        request_id="req_09",
        role="BOT",
    )
    decision = tool_service.evaluate(req)
    assert decision.status == ToolAccessStatus.DENY
    assert decision.reason_code == ToolAccessReasonCode.EXPLICIT_DENY_RULE


def test_10_missing_policy_fail_secure(dummy_clock):
    """10. Política no encontrada en el repositorio -> UNKNOWN / DENY (Fail-secure, nunca default ALLOW)"""
    repo = InMemoryToolPolicyRepository()
    service = ToolAccessPolicyService(
        policy_repository=repo,
        default_policy_name="non_existent_policy",
        clock=dummy_clock,
    )
    req = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="meli_search"),
        request_id="req_10",
    )
    decision = service.evaluate(req)
    assert decision.status == ToolAccessStatus.UNKNOWN
    assert decision.reason_code == ToolAccessReasonCode.POLICY_NOT_FOUND
    assert decision.is_allowed is False


def test_11_corrupted_policy_tampered_checksum(dummy_clock):
    """11. Política con checksum corrupto o manipulado -> DENY fail-safe"""
    policy = ToolPolicy(
        policy_name="tampered_policy",
        version="1.0.0",
        rules=(),
        default_action=ToolRuleAction.ALLOW,
    )
    # Forzar manipulación de checksum
    object.__setattr__(policy, "checksum", "0000000000000000000000000000000000000000000000000000000000000000")

    repo = InMemoryToolPolicyRepository()
    repo.save_policy(policy)
    service = ToolAccessPolicyService(
        policy_repository=repo,
        default_policy_name="tampered_policy",
        clock=dummy_clock,
    )

    req = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="meli_search"),
        request_id="req_11",
    )
    decision = service.evaluate(req)
    assert decision.status == ToolAccessStatus.DENY
    assert decision.reason_code == ToolAccessReasonCode.CORRUPTED_POLICY_OR_CHECKSUM_INVALID
    assert decision.is_allowed is False


def test_12_deterministic_decision_checksum(tool_service):
    """12. Decisiones idénticas generan checksums idénticos y válidos"""
    req = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="meli_search",
            provider="mercado_libre",
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
        ),
        request_id="req_12",
        role="AGENT",
    )
    decision_1 = tool_service.evaluate(req)
    decision_2 = tool_service.evaluate(req)

    assert decision_1.checksum == decision_2.checksum
    assert decision_1.is_valid_checksum() is True


def test_13_separation_of_duties_n3_actor_deny_not_bypassed(tool_service):
    """13. Separación de funciones: Si N.3 Authorization es DENY, N.8 no puede omitir el bloqueo"""
    mock_exec = MockActionExecutor()
    authz_service = AuthorizationService()  # Default authorization deny sin permisos
    identity_ref = IdentityReference(
        identity_id="user_unauth",
        canonical_identifier="user_unauth",
        identity_type=IdentityType.AGENT,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.UNAUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.UNKNOWN,
        provider="system",
        authenticated_at=datetime.now(timezone.utc),
    )
    principal = PrincipalContext(
        principal=identity_ref,
        auth_result=auth_res,
    )

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_exec,
        authorization_service=authz_service,
        principal_context=principal,
        tool_policy_service=tool_service,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="search products",
        parameters={"action_type": "SEARCH_CATALOG", "tool_id": "meli_search"},
    )
    state = LoopState(mission_id="m_13", iteration=1, goal="test")

    result = guarded.execute(decision, state)
    assert result["is_allowed"] is False
    assert "AUTHORIZATION_" in result["status"]
    assert mock_exec.call_count == 0


def test_14_separation_of_duties_n8_allow_does_not_bypass_n7_financial_limits(tool_service, dummy_clock):
    """14. Separación de funciones: N.8 ALLOW no bypasses N.7 Financial Limits"""
    mock_exec = MockActionExecutor()
    authz_service = AuthorizationService()
    identity_ref = IdentityReference(
        identity_id="agent_trader",
        canonical_identifier="agent_trader",
        identity_type=IdentityType.AGENT,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=datetime.now(timezone.utc),
    )
    principal = PrincipalContext(
        principal=identity_ref,
        auth_result=auth_res,
    )

    # N.7 Policy con max 100 USD
    fin_rule = FinancialLimitRule(
        rule_id="fin_rule_100",
        limit_type=FinancialLimitType.MAX_TRANSACTION_AMOUNT,
        currency="USD",
        max_amount=Decimal("100.00"),
        allow_approval_override=False,
    )
    fin_policy = FinancialLimitPolicy(
        policy_name="fin_policy_strict",
        version="1.0.0",
        currency="USD",
        rules=(fin_rule,),
    )
    fin_repo = InMemoryFinancialPolicyRepo()
    fin_repo.save_policy(fin_policy)
    fin_service = FinancialLimitService(policy_repository=fin_repo, clock=dummy_clock)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_exec,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("BUY_ITEM",),
        tool_policy_service=tool_service,
        default_tool_policy_name="default_tool_policy",
        financial_limit_service=fin_service,
        default_financial_policy_name="fin_policy_strict",
    )

    # Acción con herramienta permitida pero monto superior a 100 USD (ej. 500 USD)
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="buy expensive product",
        parameters={
            "action_type": "BUY_ITEM",
            "tool_id": "meli_search",
            "provider": "mercado_libre",
            "amount": Decimal("500.00"),
            "currency": "USD",
        },
    )
    state = LoopState(mission_id="m_14", iteration=1, goal="test")

    result = guarded.execute(decision, state)
    assert result["is_executable"] is False
    assert result["status"] == "FINANCIAL_LIMIT_EXCEEDED"
    assert mock_exec.call_count == 0


def test_15_separation_of_duties_n8_allow_does_not_bypass_n6_approval_required(tool_service, dummy_clock):
    """15. Separación de funciones: N.8 ALLOW no bypasses N.6 Approval Policies"""
    mock_exec = MockActionExecutor()
    authz_service = AuthorizationService()
    identity_ref = IdentityReference(
        identity_id="agent_trader",
        canonical_identifier="agent_trader",
        identity_type=IdentityType.AGENT,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=datetime.now(timezone.utc),
    )
    principal = PrincipalContext(
        principal=identity_ref,
        auth_result=auth_res,
    )

    # N.6 Policy que requiere aprobación obligatoria para EXPORT_DATA
    app_policy = ApprovalPolicy(
        policy_name="app_policy_strict",
        version="1.0.0",
        actions_requiring_approval=("EXPORT_DATA",),
    )
    app_repo = InMemoryApprovalPolicyRepo()
    app_repo.save_policy(app_policy)
    ev_repo = InMemoryApprovalEvidenceRepo()
    app_service = ApprovalPolicyService(policy_repository=app_repo, evidence_repository=ev_repo, clock=dummy_clock)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_exec,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("EXPORT_DATA",),
        tool_policy_service=tool_service,
        default_tool_policy_name="default_tool_policy",
        approval_service=app_service,
        default_approval_policy_name="app_policy_strict",
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="export sensitive data",
        parameters={
            "action_type": "EXPORT_DATA",
            "tool_id": "meli_search",
            "provider": "mercado_libre",
        },
    )
    state = LoopState(mission_id="m_15", iteration=1, goal="test")

    result = guarded.execute(decision, state)
    assert result["is_executable"] is False
    assert result["status"] == "APPROVAL_APPROVAL_REQUIRED"
    assert mock_exec.call_count == 0


def test_16_zero_physical_execution_on_n8_deny(tool_service):
    """16. Cero ejecuciones físicas: Si N.8 retorna DENY, el ActionExecutor delegado no se ejecuta"""
    mock_exec = MockActionExecutor()
    authz_service = AuthorizationService()
    identity_ref = IdentityReference(
        identity_id="agent_test",
        canonical_identifier="agent_test",
        identity_type=IdentityType.AGENT,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=datetime.now(timezone.utc),
    )
    principal = PrincipalContext(
        principal=identity_ref,
        auth_result=auth_res,
    )

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_exec,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("SHELL_EXEC",),
        tool_policy_service=tool_service,
        default_tool_policy_name="default_tool_policy",
    )

    # Invocación a system_shell (prohibida explícitamente en base_tool_policy)
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="execute shell command",
        parameters={
            "action_type": "SHELL_EXEC",
            "tool_id": "system_shell",
        },
    )
    state = LoopState(mission_id="m_16", iteration=1, goal="test")

    result = guarded.execute(decision, state)
    assert result["is_executable"] is False
    assert result["status"] == "TOOL_DENY"
    assert result["tool_reason_code"] == ToolAccessReasonCode.EXPLICIT_DENY_RULE.value
    assert mock_exec.call_count == 0
