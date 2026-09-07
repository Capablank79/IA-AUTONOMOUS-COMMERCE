"""
Tests de Integración y E2E para N.7 — Financial Limits (Transversal N Security, Governance y Safety).

Escenarios exigidos por la especificación:
A. authorized financial action + amount within limit -> executes.
B. amount above limit + no approval path -> zero calls.
C. amount above limit + N.6 approval required + no evidence -> zero calls.
D. amount above limit + valid N.6 approval -> executes only if policy permits exception.
E. wrong currency -> blocked/UNKNOWN.
F. resource/account-specific limit mismatch -> blocked.
G. N.3 DENY + amount within limit -> zero calls.
H. restart -> policies/financial state consistent.
I. tampered persisted limit/state -> blocked.
J. Audit/Trace safe (no secrets N.5, no CoT, structured events).
K. E2E N.7 Full Flow (Actor -> N.1 -> N.2 -> N.4 -> N.3 -> N.7 -> N.6 -> guarded execution mock).
"""

import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from src.domain.identity.models import IdentityType, IdentityReference
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.authorization.models import AuthorizationStatus
from src.domain.profit.models import Money
from src.domain.financial_limit.models import (
    FinancialLimitType,
    FinancialLimitStatus,
    FinancialLimitReasonCode,
    FinancialLimitRule,
    FinancialLimitPolicy,
    FinancialLimitRequest,
    compute_financial_decision_checksum,
)
from src.domain.approval.models import (
    ApprovalPolicy,
    ApprovalEvidence,
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
from src.application.authorization.authorization_guarded_action_executor import (
    AuthorizationGuardedActionExecutor,
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "n7_integration_db"


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
def evidence_repo(data_dir: Path) -> JsonApprovalEvidenceRepository:
    return JsonApprovalEvidenceRepository(data_dir / "approval_evidences")


@pytest.fixture
def financial_policy_repo(data_dir: Path) -> JsonFinancialLimitPolicyRepository:
    return JsonFinancialLimitPolicyRepository(data_dir / "financial_policies")


@pytest.fixture
def standard_financial_policy() -> FinancialLimitPolicy:
    rule_refund = FinancialLimitRule(
        rule_id="rule_refund_01",
        limit_type=FinancialLimitType.MAX_REFUND_AMOUNT,
        currency="USD",
        max_amount=Decimal("100.00"),
        allow_approval_override=True,
        target_action="ISSUE_REFUND",
    )
    rule_order = FinancialLimitRule(
        rule_id="rule_order_01",
        limit_type=FinancialLimitType.MAX_ORDER_VALUE,
        currency="USD",
        max_amount=Decimal("500.00"),
        min_amount=Decimal("10.00"),
        allow_approval_override=False,
        target_action="PLACE_ORDER",
    )
    rule_transfer_clp = FinancialLimitRule(
        rule_id="rule_transfer_clp_01",
        limit_type=FinancialLimitType.MAX_TRANSACTION_AMOUNT,
        currency="CLP",
        max_amount=Decimal("500000"),
        account_id="chile_store_01",
        allow_approval_override=True,
        target_action="TRANSFER_FUNDS",
    )
    return FinancialLimitPolicy(
        policy_name="commercial_financial_policy",
        version="1.0.0",
        description="Standard financial risk limits for autonomous commerce operations",
        currency="USD",
        rules=(rule_refund, rule_order, rule_transfer_clp),
    )


@pytest.fixture
def standard_approval_policy() -> ApprovalPolicy:
    return ApprovalPolicy(
        policy_name="commercial_governance_policy",
        version="1.0.0",
        description="Approval policy for high-impact operations",
        actions_not_requiring_approval=("MARKET_ANALYSIS", "SYNC_CATALOG", "market_analysis"),
        actions_requiring_approval=("ISSUE_REFUND", "TRANSFER_FUNDS", "PLACE_ORDER"),
        require_separation_of_duties=True,
        default_ttl_seconds=3600,
    )


@pytest.fixture
def approval_service(
    standard_approval_policy: ApprovalPolicy,
    evidence_repo: JsonApprovalEvidenceRepository,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> ApprovalPolicyService:
    policy_repo = InMemoryApprovalPolicyRepository(initial_policies=[standard_approval_policy])
    return ApprovalPolicyService(
        policy_repository=policy_repo,
        evidence_repository=evidence_repo,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
    )


@pytest.fixture
def financial_limit_service(
    financial_policy_repo: JsonFinancialLimitPolicyRepository,
    standard_financial_policy: FinancialLimitPolicy,
    audit_repo: JsonAuditRepository,
    virtual_clock: VirtualClock,
) -> FinancialLimitService:
    financial_policy_repo.save_policy(standard_financial_policy)
    return FinancialLimitService(
        policy_repository=financial_policy_repo,
        default_policy_name="commercial_financial_policy",
        audit_repository=audit_repo,
        clock=virtual_clock,
    )


@pytest.fixture
def authz_service(
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> AuthorizationService:
    return AuthorizationService(
        policy_engine=None,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
        default_policy_version="1.0.0",
    )


def build_authenticated_principal(
    identity_id: str,
    identity_type: IdentityType = IdentityType.AGENT,
) -> PrincipalContext:
    ref = IdentityReference(
        identity_id=identity_id,
        canonical_identifier=f"canonical_{identity_id}",
        identity_type=identity_type,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=datetime.now(timezone.utc),
    )
    return PrincipalContext(principal=ref, auth_result=auth_res)


# ---------------------------------------------------------------------------
# Escenario A: Authorized financial action + amount within limit -> executes
# ---------------------------------------------------------------------------
def test_scenario_a_authorized_action_within_financial_limit_executes(
    authz_service: AuthorizationService,
    financial_limit_service: FinancialLimitService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)
    mock_delegate.execute.return_value = {"status": "SUCCESS", "refund_id": "ref_100"}

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["ISSUE_REFUND"],
        financial_limit_service=financial_limit_service,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Process small customer refund",
        target="order_100",
        parameters={
            "action_type": "ISSUE_REFUND",
            "amount": Decimal("45.00"),
            "currency": "USD",
        },
    )
    state = LoopState(mission_id="m-scen-a", iteration=1, goal="Execute refund")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "SUCCESS"
    assert result["is_allowed"] is True
    assert result["is_executable"] is True
    assert mock_delegate.execute.call_count == 1
    assert guarded_executor.latest_financial_decision is not None
    assert guarded_executor.latest_financial_decision.status == FinancialLimitStatus.WITHIN_LIMIT


# ---------------------------------------------------------------------------
# Escenario B: Amount above limit + no approval path -> zero calls
# ---------------------------------------------------------------------------
def test_scenario_b_amount_above_limit_strict_rejection_zero_calls(
    authz_service: AuthorizationService,
    financial_limit_service: FinancialLimitService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["PLACE_ORDER"],
        financial_limit_service=financial_limit_service,
    )

    # PLACE_ORDER tiene max 500 USD y allow_approval_override=False
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Place massive order exceeding ceiling",
        target="order_bulk_999",
        parameters={
            "action_type": "PLACE_ORDER",
            "amount": Decimal("900.00"),
            "currency": "USD",
        },
    )
    state = LoopState(mission_id="m-scen-b", iteration=1, goal="Bulk order")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "FINANCIAL_LIMIT_EXCEEDED"
    assert result["is_allowed"] is True  # N.3 autorizó
    assert result["is_executable"] is False  # N.7 bloquea estrictamente
    assert mock_delegate.execute.call_count == 0  # CERO llamadas físicas
    assert guarded_executor.latest_financial_decision.status == FinancialLimitStatus.LIMIT_EXCEEDED


# ---------------------------------------------------------------------------
# Escenario C: Amount above limit + N.6 approval required + no evidence -> zero calls
# ---------------------------------------------------------------------------
def test_scenario_c_amount_above_limit_approval_required_without_evidence_zero_calls(
    authz_service: AuthorizationService,
    financial_limit_service: FinancialLimitService,
    approval_service: ApprovalPolicyService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["ISSUE_REFUND"],
        financial_limit_service=financial_limit_service,
        approval_service=approval_service,
    )

    # ISSUE_REFUND tiene límite 100.00 USD con allow_approval_override=True.
    # Monto 150.00 USD activa APPROVAL_REQUIRED pero no se pasa attached_evidence_id.
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Refund requiring exception approval",
        target="order_200",
        parameters={
            "action_type": "ISSUE_REFUND",
            "amount": Decimal("150.00"),
            "currency": "USD",
            "approval_policy_name": "commercial_governance_policy",
        },
    )
    state = LoopState(mission_id="m-scen-c", iteration=1, goal="Exception refund")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "APPROVAL_APPROVAL_REQUIRED" or result["status"] == "APPROVAL_REQUIRED"
    assert result["is_allowed"] is True
    assert result["is_executable"] is False
    assert result["requires_approval"] is True
    assert mock_delegate.execute.call_count == 0  # CERO llamadas físicas


# ---------------------------------------------------------------------------
# Escenario D: Amount above limit + valid N.6 approval -> executes only if policy permits
# ---------------------------------------------------------------------------
def test_scenario_d_amount_above_limit_with_valid_n6_approval_executes(
    authz_service: AuthorizationService,
    financial_limit_service: FinancialLimitService,
    approval_service: ApprovalPolicyService,
):
    requester = build_authenticated_principal("agent_commercial")
    supervisor_ref = IdentityReference(
        identity_id="human_cfo",
        canonical_identifier="canonical_human_cfo",
        identity_type=IdentityType.USER,
    )

    # Otorgar aprobación en N.6
    approval_service.grant_approval(
        approval_id="app_refund_exception_001",
        target_action="ISSUE_REFUND",
        target_resource="order_200",
        requesting_identity_id=requester.identity_id,
        approver_identity_id=supervisor_ref.identity_id,
        policy_name="commercial_governance_policy",
    )

    mock_delegate = MagicMock(spec=ActionExecutor)
    mock_delegate.execute.return_value = {"status": "SUCCESS", "refund_processed": True}

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=requester,
        default_allowed_actions=["ISSUE_REFUND"],
        financial_limit_service=financial_limit_service,
        approval_service=approval_service,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Execute high-value refund with CFO approval",
        target="order_200",
        parameters={
            "action_type": "ISSUE_REFUND",
            "amount": Decimal("150.00"),
            "currency": "USD",
            "approval_policy_name": "commercial_governance_policy",
            "attached_evidence_id": "app_refund_exception_001",
        },
    )
    state = LoopState(mission_id="m-scen-d", iteration=1, goal="Execute approved refund")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "SUCCESS"
    assert result["is_allowed"] is True
    assert result["is_executable"] is True
    assert mock_delegate.execute.call_count == 1
    assert guarded_executor.latest_financial_decision.status == FinancialLimitStatus.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# Escenario E: Wrong currency -> blocked/UNKNOWN -> zero calls
# ---------------------------------------------------------------------------
def test_scenario_e_wrong_currency_mismatch_blocks_execution(
    authz_service: AuthorizationService,
    financial_limit_service: FinancialLimitService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["ISSUE_REFUND"],
        financial_limit_service=financial_limit_service,
    )

    # Moneda EUR no coincide con política USD -> CURRENCY_MISMATCH / UNKNOWN
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Refund in unconfigured currency EUR",
        target="order_300",
        parameters={
            "action_type": "ISSUE_REFUND",
            "amount": Decimal("50.00"),
            "currency": "EUR",
        },
    )
    state = LoopState(mission_id="m-scen-e", iteration=1, goal="Refund in EUR")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "FINANCIAL_UNKNOWN"
    assert result["is_executable"] is False
    assert mock_delegate.execute.call_count == 0


# ---------------------------------------------------------------------------
# Escenario F: Resource/Account-specific limit mismatch -> blocked
# ---------------------------------------------------------------------------
def test_scenario_f_account_specific_limit_blocks_unauthorized_account(
    authz_service: AuthorizationService,
    financial_limit_service: FinancialLimitService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["TRANSFER_FUNDS"],
        financial_limit_service=financial_limit_service,
    )

    # TRANSFER_FUNDS está configurado sólo para chile_store_01 en CLP.
    # Al solicitar para 'brazil_store_01', no hay regla aplicable -> fail-safe UNKNOWN
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Fund transfer on unconfigured store account",
        target="account_transfer",
        parameters={
            "action_type": "TRANSFER_FUNDS",
            "amount": Decimal("20000"),
            "currency": "CLP",
            "account_id": "brazil_store_01",
        },
    )
    state = LoopState(mission_id="m-scen-f", iteration=1, goal="Fund transfer")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "FINANCIAL_UNKNOWN"
    assert result["is_executable"] is False
    assert mock_delegate.execute.call_count == 0


# ---------------------------------------------------------------------------
# Escenario G: N.3 DENY + amount within limit -> zero calls
# ---------------------------------------------------------------------------
def test_scenario_g_n3_deny_with_amount_within_limit_blocks_execution(
    financial_limit_service: FinancialLimitService,
):
    class DenyAllAuthzService:
        def authorize(self, request, **kwargs):
            from src.domain.authorization.models import AuthorizationDecision
            return AuthorizationDecision(
                decision_id="authz_deny_strict",
                identity_id="restricted_agent",
                action=request.action,
                resource=request.resource,
                status=AuthorizationStatus.DENY,
                reason_codes=("ACTION_DENIED",),
                reasons=("Forbidden by core authorization",),
                evaluated_at=datetime.now(timezone.utc),
            )

    principal = build_authenticated_principal("restricted_agent")
    mock_delegate = MagicMock(spec=ActionExecutor)

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=DenyAllAuthzService(),  # type: ignore
        principal_context=principal,
        financial_limit_service=financial_limit_service,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Attempt refund when unauthorized by N.3",
        target="order_400",
        parameters={
            "action_type": "ISSUE_REFUND",
            "amount": Decimal("10.00"),  # Muy por debajo del límite de 100 USD
            "currency": "USD",
        },
    )
    state = LoopState(mission_id="m-scen-g", iteration=1, goal="Refund attempt")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "AUTHORIZATION_DENY"
    assert result["is_allowed"] is False
    assert mock_delegate.execute.call_count == 0  # N.7 no se salta N.3 DENY


# ---------------------------------------------------------------------------
# Escenario H: Restart -> policies/financial state consistent
# ---------------------------------------------------------------------------
def test_scenario_h_restart_preserves_policy_and_limits(
    data_dir: Path,
    standard_financial_policy: FinancialLimitPolicy,
    virtual_clock: VirtualClock,
    audit_repo: JsonAuditRepository,
):
    repo1 = JsonFinancialLimitPolicyRepository(data_dir / "financial_policies")
    repo1.save_policy(standard_financial_policy)

    # Simular reinicio creando nueva instancia del repositorio sobre el mismo disco
    repo2 = JsonFinancialLimitPolicyRepository(data_dir / "financial_policies")
    service2 = FinancialLimitService(
        policy_repository=repo2,
        default_policy_name="commercial_financial_policy",
        audit_repository=audit_repo,
        clock=virtual_clock,
    )

    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("50.00"), currency="USD"),
    )
    dec = service2.evaluate(req)

    assert dec.status == FinancialLimitStatus.WITHIN_LIMIT
    assert dec.policy_version == "1.0.0"
    assert dec.limit_value == Decimal("100.00")


# ---------------------------------------------------------------------------
# Escenario I: Tampered persisted policy -> blocked/corrupted
# ---------------------------------------------------------------------------
def test_scenario_i_tampered_policy_file_blocks_execution(
    data_dir: Path,
    standard_financial_policy: FinancialLimitPolicy,
    virtual_clock: VirtualClock,
):
    repo = JsonFinancialLimitPolicyRepository(data_dir / "financial_policies")
    repo.save_policy(standard_financial_policy)

    policy_file = data_dir / "financial_policies" / "financial_limit_policies" / "commercial_financial_policy.json"
    assert policy_file.exists()

    # Manipular el archivo JSON cambiando el límite sin actualizar checksum
    with open(policy_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["rules"][0]["max_amount"] = "999999.00"  # Manipulación no autorizada

    with open(policy_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Nuevo servicio que lee el archivo manipulado
    repo_reloaded = JsonFinancialLimitPolicyRepository(data_dir / "financial_policies")
    service = FinancialLimitService(
        policy_repository=repo_reloaded,
        default_policy_name="commercial_financial_policy",
        clock=virtual_clock,
    )

    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("50.00"), currency="USD"),
    )
    dec = service.evaluate(req)

    # Debe fallar cerrado por corrupción/manipulación de datos
    assert dec.status == FinancialLimitStatus.ERROR
    assert dec.reason_code == FinancialLimitReasonCode.CORRUPTED_POLICY_OR_STATE


# ---------------------------------------------------------------------------
# Escenario J: Audit/Trace safe (no secrets N.5, no CoT)
# ---------------------------------------------------------------------------
def test_scenario_j_audit_trail_recorded_without_secrets(
    financial_limit_service: FinancialLimitService,
    audit_repo: JsonAuditRepository,
):
    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("150.00"), currency="USD"),
        context={"client_secret": "my_super_secret_token", "order_ref": "ord_99"},
    )
    dec = financial_limit_service.evaluate(req)

    assert dec.status == FinancialLimitStatus.APPROVAL_REQUIRED

    # Consultar repositorio de auditoría
    records = audit_repo.list_records(record_type=AuditRecordType.LIMIT_EXCEEDED)
    assert len(records) > 0

    latest_rec = records[-1]
    assert "client_secret" in latest_rec.metadata
    assert latest_rec.metadata["client_secret"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# Escenario K: E2E Full Pipeline N.1 -> N.2 -> N.4 -> N.3 -> N.7 -> N.6 -> Guard
# ---------------------------------------------------------------------------
def test_scenario_k_e2e_full_governance_pipeline(
    authz_service: AuthorizationService,
    financial_limit_service: FinancialLimitService,
    approval_service: ApprovalPolicyService,
):
    # 1. N.1 Identidad del agente y del supervisor
    agent_ref = IdentityReference(
        identity_id="agent_order_manager",
        canonical_identifier="canonical_agent_order_manager",
        identity_type=IdentityType.AGENT,
    )
    supervisor_ref = IdentityReference(
        identity_id="human_supervisor",
        canonical_identifier="canonical_human_supervisor",
        identity_type=IdentityType.USER,
    )

    # 2. N.2 Autenticación del agente
    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=agent_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=datetime.now(timezone.utc),
    )
    principal = PrincipalContext(principal=agent_ref, auth_result=auth_result)

    mock_delegate = MagicMock(spec=ActionExecutor)
    mock_delegate.execute.return_value = {"status": "SUCCESS", "order_id": "ord_vip_777"}

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["ISSUE_REFUND"],
        financial_limit_service=financial_limit_service,
        approval_service=approval_service,
    )

    # Paso 1: Acción que requiere excepción financiera (150 USD > 100 USD) sin aprobación -> BLOQUEADO
    step1_dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Process refund over policy ceiling",
        target="order_vip_777",
        parameters={
            "action_type": "ISSUE_REFUND",
            "amount": Decimal("150.00"),
            "currency": "USD",
            "approval_policy_name": "commercial_governance_policy",
        },
    )
    step1_state = LoopState(mission_id="m-e2e-n7", iteration=1, goal="E2E Pipeline")

    res1 = guarded_executor.execute(step1_dec, step1_state)
    assert res1["status"] in ("APPROVAL_APPROVAL_REQUIRED", "APPROVAL_REQUIRED")
    assert res1["is_executable"] is False
    assert mock_delegate.execute.call_count == 0

    # Paso 2: Supervisor otorga aprobación formal en N.6
    approval_service.grant_approval(
        approval_id="app_e2e_n7_exception",
        target_action="ISSUE_REFUND",
        target_resource="order_vip_777",
        requesting_identity_id=agent_ref.identity_id,
        approver_identity_id=supervisor_ref.identity_id,
        policy_name="commercial_governance_policy",
    )

    # Paso 3: Agente reintenta adjuntando la evidencia de aprobación -> EJECUTADO EXITOSAMENTE
    step3_dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Process refund with attached CFO approval",
        target="order_vip_777",
        parameters={
            "action_type": "ISSUE_REFUND",
            "amount": Decimal("150.00"),
            "currency": "USD",
            "approval_policy_name": "commercial_governance_policy",
            "attached_evidence_id": "app_e2e_n7_exception",
        },
    )
    res3 = guarded_executor.execute(step3_dec, step1_state)
    assert res3["status"] == "SUCCESS"
    assert res3["is_executable"] is True
    assert mock_delegate.execute.call_count == 1
