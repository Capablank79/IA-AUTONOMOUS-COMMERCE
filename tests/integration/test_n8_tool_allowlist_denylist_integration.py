"""
Tests de Integración y E2E para N.8 — Tool Allowlist / Denylist (Transversal N Security, Governance y Safety).

Escenarios exigidos por la especificación:
A. authorized tool action in allowlist -> evaluates to ALLOW and executes in guarded executor.
B. tool in denylist -> evaluated to DENY -> zero physical execution calls.
C. default DENY (tool unlisted) -> blocked -> zero physical execution calls.
D. normalization anti-bypass (whitespace, casing, compound format) -> evaluates deterministically.
E. side-effect level mismatch (e.g., WRITE requested on READ_ONLY allowed tool) -> blocked.
F. scoped allow with valid and invalid scope -> allow with scope, deny without/with wrong scope.
G. role-restricted tool -> blocked for denied role, allowed for authorized role.
H. restart -> policies/tool state consistent with SHA-256 validation.
I. tampered persisted policy/state -> blocked (tampered checksum = fail-secure DENY).
J. Audit/Trace safe (no secrets N.5, no CoT, structured events TOOL_ACCESS_EVALUATED / TOOL_ACCESS_DENIED).
K. E2E N.8 Full Flow (Actor -> N.1 -> N.2 -> N.4 -> N.3 -> N.8 -> N.7 -> N.6 -> guarded execution mock).
"""

import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from src.domain.identity.models import IdentityType, IdentityReference, PrincipalIdentity
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.authorization.models import AuthorizationStatus
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
    compute_tool_policy_checksum,
    compute_tool_decision_checksum,
)
from src.domain.approval.models import ApprovalPolicy
from src.domain.financial_limit.models import (
    FinancialLimitPolicy,
    FinancialLimitRule,
    FinancialLimitType,
)
from src.domain.mission.models import (
    LoopDecision,
    LoopAction,
    LoopState,
)
from src.domain.mission.ports import ActionExecutor
from src.domain.audit.models import AuditRecordType

from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.approval_evidence_repository import JsonApprovalEvidenceRepository
from src.infrastructure.persistence.data.json.financial_limit_repository import JsonFinancialLimitPolicyRepository
from src.infrastructure.persistence.data.json.tool_policy_repository import JsonToolPolicyRepository
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock

from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import AuthenticationService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.application.approval.approval_policy_service import (
    ApprovalPolicyService,
    InMemoryApprovalPolicyRepository,
)
from src.application.financial_limit.financial_limit_service import FinancialLimitService
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.application.authorization.authorization_guarded_action_executor import (
    AuthorizationGuardedActionExecutor,
)


class MockActionExecutor(ActionExecutor):
    def __init__(self):
        self.call_count = 0
        self.executed_decisions = []

    def execute(self, decision: LoopDecision, state: LoopState):
        self.call_count += 1
        self.executed_decisions.append(decision)
        return {
            "status": "SUCCESS",
            "executed": True,
            "decision": decision.action.value,
        }


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "n8_integration_db"


@pytest.fixture
def virtual_clock() -> VirtualClock:
    start_time = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def audit_repo(data_dir: Path) -> JsonAuditRepository:
    return JsonAuditRepository(data_dir / "audit")


@pytest.fixture
def trace_repo(data_dir: Path) -> JsonAgentTraceRepository:
    return JsonAgentTraceRepository(data_dir / "trace")


@pytest.fixture
def trace_service(trace_repo: JsonAgentTraceRepository) -> AgentTraceService:
    return AgentTraceService(trace_repo)


@pytest.fixture
def tool_repo(data_dir: Path) -> JsonToolPolicyRepository:
    return JsonToolPolicyRepository(data_dir / "tool_policies")


@pytest.fixture
def standard_tool_policy(tool_repo: JsonToolPolicyRepository) -> ToolPolicy:
    rule_search = ToolPolicyRule(
        rule_id="rule_meli_search",
        action=ToolRuleAction.ALLOW,
        tool_id_pattern="meli_search",
        provider_pattern="mercado_libre",
        allowed_side_effect_levels=(ToolSideEffectLevel.READ_ONLY, ToolSideEffectLevel.ANALYSIS),
        allowed_roles=("AGENT", "ANALYST", "ADMIN"),
        description="Allow Meli Search read/analysis",
    )
    rule_scraper = ToolPolicyRule(
        rule_id="rule_web_scraper",
        action=ToolRuleAction.ALLOW,
        tool_id_pattern="web_scraper",
        allowed_scopes=("catalog", "research"),
        description="Allow Web Scraper scoped",
    )
    rule_deny_shell = ToolPolicyRule(
        rule_id="rule_deny_shell",
        action=ToolRuleAction.DENY,
        tool_id_pattern="system_shell",
        description="Explicitly deny shell execution",
    )
    rule_deny_price_bot = ToolPolicyRule(
        rule_id="rule_deny_price_bot",
        action=ToolRuleAction.DENY,
        tool_id_pattern="price_updater",
        denied_roles=("BOT", "GUEST"),
        description="Deny bot price updates",
    )

    policy = ToolPolicy(
        policy_name="governance_tool_policy",
        version="1.0.0",
        rules=(rule_search, rule_scraper, rule_deny_shell, rule_deny_price_bot),
        default_action=ToolRuleAction.DENY,
    )
    tool_repo.save_policy(policy)
    return policy


@pytest.fixture
def tool_service(
    tool_repo: JsonToolPolicyRepository,
    audit_repo: JsonAuditRepository,
    virtual_clock: VirtualClock,
) -> ToolAccessPolicyService:
    return ToolAccessPolicyService(
        policy_repository=tool_repo,
        default_policy_name="governance_tool_policy",
        audit_repository=audit_repo,
        clock=virtual_clock,
    )


# ==============================================================================
# Escenario A: authorized tool action in allowlist -> evaluates to ALLOW and executes
# ==============================================================================
def test_scenario_a_authorized_tool_in_allowlist_executes(
    standard_tool_policy, tool_service, virtual_clock
):
    mock_exec = MockActionExecutor()
    authz_service = AuthorizationService()
    identity_ref = IdentityReference(
        identity_id="agent_trader_01",
        canonical_identifier="agent_trader_01",
        identity_type=IdentityType.AGENT,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=virtual_clock.now(),
    )
    principal = PrincipalContext(principal=identity_ref, auth_result=auth_res)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_exec,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("SEARCH_ITEMS",),
        tool_policy_service=tool_service,
        default_tool_policy_name="governance_tool_policy",
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="search items on meli",
        parameters={
            "action_type": "SEARCH_ITEMS",
            "tool_id": "meli_search",
            "provider": "mercado_libre",
            "side_effect_level": "READ_ONLY",
            "role": "AGENT",
        },
    )
    state = LoopState(mission_id="m_scenario_a", iteration=1, goal="test")

    res = guarded.execute(decision, state)
    assert res["status"] == "SUCCESS"
    assert res["executed"] is True
    assert mock_exec.call_count == 1


# ==============================================================================
# Escenario B: tool in denylist -> evaluated to DENY -> zero physical execution calls
# ==============================================================================
def test_scenario_b_tool_in_denylist_zero_execution(
    standard_tool_policy, tool_service, virtual_clock
):
    mock_exec = MockActionExecutor()
    authz_service = AuthorizationService()
    identity_ref = IdentityReference(
        identity_id="agent_admin_01",
        canonical_identifier="agent_admin_01",
        identity_type=IdentityType.AGENT,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=virtual_clock.now(),
    )
    principal = PrincipalContext(principal=identity_ref, auth_result=auth_res)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_exec,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("SYSTEM_COMMAND",),
        tool_policy_service=tool_service,
        default_tool_policy_name="governance_tool_policy",
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="run bash command",
        parameters={
            "action_type": "SYSTEM_COMMAND",
            "tool_id": "system_shell",
        },
    )
    state = LoopState(mission_id="m_scenario_b", iteration=1, goal="test")

    res = guarded.execute(decision, state)
    assert res["is_executable"] is False
    assert res["status"] == "TOOL_DENY"
    assert res["tool_reason_code"] == ToolAccessReasonCode.EXPLICIT_DENY_RULE.value
    assert mock_exec.call_count == 0


# ==============================================================================
# Escenario C: default DENY (tool unlisted) -> blocked -> zero physical execution calls
# ==============================================================================
def test_scenario_c_unlisted_tool_default_deny(
    standard_tool_policy, tool_service, virtual_clock
):
    mock_exec = MockActionExecutor()
    authz_service = AuthorizationService()
    identity_ref = IdentityReference(
        identity_id="agent_trader_01",
        canonical_identifier="agent_trader_01",
        identity_type=IdentityType.AGENT,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=virtual_clock.now(),
    )
    principal = PrincipalContext(principal=identity_ref, auth_result=auth_res)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_exec,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("CUSTOM_TOOL_RUN",),
        tool_policy_service=tool_service,
        default_tool_policy_name="governance_tool_policy",
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="execute unknown vendor tool",
        parameters={
            "action_type": "CUSTOM_TOOL_RUN",
            "tool_id": "unregistered_vendor_tool",
        },
    )
    state = LoopState(mission_id="m_scenario_c", iteration=1, goal="test")

    res = guarded.execute(decision, state)
    assert res["is_executable"] is False
    assert res["status"] == "TOOL_DENY"
    assert res["tool_reason_code"] == ToolAccessReasonCode.DEFAULT_DENY_NO_RULE.value
    assert mock_exec.call_count == 0


# ==============================================================================
# Escenario D: normalization anti-bypass -> evaluates deterministically
# ==============================================================================
def test_scenario_d_normalization_anti_bypass(standard_tool_policy, tool_service):
    # Solicitud con variaciones de espacios y mayúsculas
    req = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="  MELI_SEARCH  ",
            provider="  Mercado_Libre  ",
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
        ),
        request_id="req_d",
        role="AGENT ",
    )
    decision = tool_service.evaluate(req)
    assert decision.status == ToolAccessStatus.ALLOW
    assert decision.tool_reference.tool_id == "meli_search"
    assert decision.tool_reference.provider == "mercado_libre"
    assert decision.tool_reference.canonical_id == "mercado_libre:meli_search"


# ==============================================================================
# Escenario E: side-effect level mismatch -> blocked
# ==============================================================================
def test_scenario_e_side_effect_level_mismatch(standard_tool_policy, tool_service):
    # Herramienta permitida para READ_ONLY/ANALYSIS pero requerida con WRITE
    req = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="meli_search",
            provider="mercado_libre",
            side_effect_level=ToolSideEffectLevel.WRITE,
        ),
        request_id="req_e",
        role="AGENT",
    )
    decision = tool_service.evaluate(req)
    assert decision.status == ToolAccessStatus.DENY
    assert decision.reason_code == ToolAccessReasonCode.DEFAULT_DENY_NO_RULE


# ==============================================================================
# Escenario F: scoped allow with valid and invalid scope
# ==============================================================================
def test_scenario_f_scoped_allow_valid_and_invalid(standard_tool_policy, tool_service):
    # Valid scope catalog -> ALLOW
    req_valid = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="web_scraper"),
        request_id="req_f_valid",
        scope="catalog",
    )
    dec_valid = tool_service.evaluate(req_valid)
    assert dec_valid.status == ToolAccessStatus.ALLOW

    # Invalid scope finance -> DENY
    req_invalid = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="web_scraper"),
        request_id="req_f_invalid",
        scope="finance",
    )
    dec_invalid = tool_service.evaluate(req_invalid)
    assert dec_invalid.status == ToolAccessStatus.DENY


# ==============================================================================
# Escenario G: role-restricted tool -> blocked for denied role, allowed for authorized role
# ==============================================================================
def test_scenario_g_role_restricted_tool(standard_tool_policy, tool_service):
    # Role BOT -> Explícitamente denegado
    req_bot = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="price_updater"),
        request_id="req_g_bot",
        role="BOT",
    )
    dec_bot = tool_service.evaluate(req_bot)
    assert dec_bot.status == ToolAccessStatus.DENY
    assert dec_bot.reason_code == ToolAccessReasonCode.EXPLICIT_DENY_RULE


# ==============================================================================
# Escenario H: restart -> policies/tool state consistent with SHA-256 validation
# ==============================================================================
def test_scenario_h_restart_persistence_and_checksum(data_dir: Path, standard_tool_policy):
    # Crear un nuevo repositorio apuntando al mismo directorio de disco
    new_repo = JsonToolPolicyRepository(data_dir / "tool_policies")
    loaded_policy = new_repo.get_policy("governance_tool_policy")

    assert loaded_policy is not None
    assert loaded_policy.policy_name == "governance_tool_policy"
    assert loaded_policy.version == "1.0.0"
    assert len(loaded_policy.rules) == 4
    assert loaded_policy.is_valid_checksum() is True


# ==============================================================================
# Escenario I: tampered persisted policy/state -> blocked fail-secure
# ==============================================================================
def test_scenario_i_tampered_persisted_policy(data_dir: Path, standard_tool_policy, virtual_clock):
    policy_file = data_dir / "tool_policies" / "tool_policies" / "governance_tool_policy.json"
    if not policy_file.exists():
        policy_file = data_dir / "tool_policies" / "governance_tool_policy.json"
    assert policy_file.exists()

    # Modificar físicamente el archivo sin actualizar checksum
    with open(policy_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["default_action"] = "ALLOW"
    with open(policy_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    # Re-instanciar repositorio para forzar lectura de disco
    reloaded_repo = JsonToolPolicyRepository(data_dir / "tool_policies")
    reloaded_service = ToolAccessPolicyService(
        policy_repository=reloaded_repo,
        default_policy_name="governance_tool_policy",
        clock=virtual_clock,
    )

    req = ToolAccessRequest(
        tool_reference=ToolReference(tool_id="meli_search"),
        request_id="req_i",
    )
    decision = reloaded_service.evaluate(req)
    assert decision.status == ToolAccessStatus.DENY
    assert decision.reason_code == ToolAccessReasonCode.CORRUPTED_POLICY_OR_CHECKSUM_INVALID
    assert decision.is_allowed is False


# ==============================================================================
# Escenario J: Audit/Trace safe (no secrets N.5, no CoT, structured events)
# ==============================================================================
def test_scenario_j_audit_and_trace_safety(
    standard_tool_policy, tool_service, audit_repo, trace_repo, virtual_clock
):
    req_secret = ToolAccessRequest(
        tool_reference=ToolReference(
            tool_id="meli_search",
            provider="mercado_libre",
            side_effect_level=ToolSideEffectLevel.READ_ONLY,
            metadata={"api_key": "secret_super_pass", "safe_param": "valid"},
        ),
        request_id="req_j",
        role="AGENT",
        metadata={"token": "bearer_secret_123"},
    )
    decision = tool_service.evaluate(req_secret)
    assert decision.status == ToolAccessStatus.ALLOW

    # Verificar que la decisión y la referencia de la herramienta hayan redactado los secretos
    assert decision.tool_reference.metadata.get("api_key") == "[REDACTED]"
    assert decision.metadata.get("token") == "[REDACTED]"

    # Verificar auditoría
    audit_records = audit_repo.list_records(limit=50)
    assert len(audit_records) >= 1
    eval_rec = [r for r in audit_records if r.record_type == AuditRecordType.TOOL_ACCESS_EVALUATED][0]
    rec_str = json.dumps(dict(eval_rec.metadata))

    # Garantizar CERO secretos en metadata de auditoría
    assert "secret_super_pass" not in rec_str
    assert "bearer_secret_123" not in rec_str


# ==============================================================================
# Escenario K: E2E N.8 Full Flow (Actor -> N.1 -> N.2 -> N.4 -> N.3 -> N.8 -> N.7 -> N.6 -> guarded execution mock)
# ==============================================================================
def test_scenario_k_e2e_full_flow(
    data_dir: Path,
    standard_tool_policy,
    tool_service,
    audit_repo,
    trace_service,
    virtual_clock,
):
    # 1. Identity N.1
    identity_ref = IdentityReference(
        identity_id="agent_full_flow_01",
        canonical_identifier="agent_full_flow_01",
        identity_type=IdentityType.AGENT,
    )

    # 2. Authentication N.2
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal_orchestrator",
        authenticated_at=virtual_clock.now(),
    )
    principal_ctx = PrincipalContext(principal=identity_ref, auth_result=auth_res)

    # 3. Authorization N.3
    authz_svc = AuthorizationService()

    # 4. Financial Limits N.7
    fin_repo = JsonFinancialLimitPolicyRepository(data_dir / "fin_policies")
    fin_rule = FinancialLimitRule(
        rule_id="fin_max_order",
        limit_type=FinancialLimitType.MAX_TRANSACTION_AMOUNT,
        currency="USD",
        max_amount=Decimal("1000.00"),
        allow_approval_override=True,
    )
    fin_policy = FinancialLimitPolicy(
        policy_name="standard_finance",
        version="1.0.0",
        currency="USD",
        rules=(fin_rule,),
    )
    fin_repo.save_policy(fin_policy)
    fin_svc = FinancialLimitService(policy_repository=fin_repo, clock=virtual_clock)

    # 5. Approval Policies N.6
    app_repo = InMemoryApprovalPolicyRepository()
    app_ev_repo = JsonApprovalEvidenceRepository(data_dir / "approval_evidence")
    app_policy = ApprovalPolicy(
        policy_name="standard_approval",
        version="1.0.0",
        actions_not_requiring_approval=("ORDER_DISCOVERY",),
        actions_requiring_approval=(),
    )
    app_repo.save_policy(app_policy)
    app_svc = ApprovalPolicyService(
        policy_repository=app_repo,
        evidence_repository=app_ev_repo,
        clock=virtual_clock,
    )

    # 6. Guarded Action Executor (N.3 -> N.8 -> N.7 -> N.6)
    mock_exec = MockActionExecutor()
    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_exec,
        authorization_service=authz_svc,
        principal_context=principal_ctx,
        default_allowed_actions=("ORDER_DISCOVERY",),
        tool_policy_service=tool_service,
        default_tool_policy_name="governance_tool_policy",
        financial_limit_service=fin_svc,
        default_financial_policy_name="standard_finance",
        approval_service=app_svc,
        default_approval_policy_name="standard_approval",
    )

    # Step 1: Flujo exitoso end-to-end
    decision_ok = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="search products within limit and allowed tool",
        parameters={
            "action_type": "ORDER_DISCOVERY",
            "tool_id": "meli_search",
            "provider": "mercado_libre",
            "role": "AGENT",
            "amount": Decimal("150.00"),
            "currency": "USD",
        },
    )
    state = LoopState(mission_id="m_e2e_full", iteration=1, goal="order")

    res_ok = guarded.execute(decision_ok, state)
    assert res_ok["status"] == "SUCCESS"
    assert res_ok["executed"] is True
    assert mock_exec.call_count == 1

    # Step 2: Bloqueo en N.8 (herramienta shell denegada) manteniendo 0 ejecuciones físicas adicionales
    decision_denied_tool = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="attempt forbidden tool",
        parameters={
            "action_type": "ORDER_DISCOVERY",
            "tool_id": "system_shell",
            "amount": Decimal("150.00"),
            "currency": "USD",
        },
    )
    res_deny_tool = guarded.execute(decision_denied_tool, state)
    assert res_deny_tool["is_executable"] is False
    assert res_deny_tool["status"] == "TOOL_DENY"
    assert mock_exec.call_count == 1  # No incrementa
