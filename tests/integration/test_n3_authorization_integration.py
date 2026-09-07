"""
Tests de Integración y E2E para N.3 — Authorization (Transversal N Security, Governance y Safety).

Escenarios exigidos por la especificación:
A. N.1 identity -> N.2 AUTHENTICATED -> N.3 ALLOW -> operation boundary / mock reached.
B. Authenticated actor -> N.3 DENY -> mock NOT called.
C. Unauthenticated actor -> blocked (no ALLOW, mock NOT called).
D. Same principal: READ allowed, external mutation denied (action granularity).
E. Resource / account mismatch -> DENY/UNKNOWN.
F. Audit / Trace contains safe decision (AUTHORIZATION_EVALUATED, no secrets).
G. Policy version change -> decision reevaluated.
H. Commercial external action cannot bypass N.3 (ActionExecutor boundary protection).
I. E2E N.3: Full pipeline (Actor -> N.1 -> N.2 -> N.3 -> ActionExecutor boundary).
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any
from unittest.mock import MagicMock
import pytest

from src.domain.identity.models import IdentityType, IdentityReference
from src.domain.oauth.models import OAuthConnection
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    PrincipalContext,
)
from src.domain.authorization.models import (
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
    AuthorizationRequest,
    AuthorizationDecision,
)
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import (
    AuthorizationPolicyRule,
    HumanApprovalPolicyRule,
    IdempotencyPolicyRule,
)
from src.domain.mission.models import LoopDecision, LoopAction, LoopState, MissionType, MissionStatus
from src.domain.audit.models import AuditRecordType

from src.infrastructure.persistence.data.json.identity_repository import (
    JsonIdentityRepository,
)
from src.infrastructure.persistence.data.json.audit_repository import (
    JsonAuditRepository,
)
from src.infrastructure.persistence.data.json.agent_trace_repository import (
    JsonAgentTraceRepository,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock

from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import AuthenticationService
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.authorization.authorization_guarded_action_executor import (
    AuthorizationGuardedActionExecutor,
)
from src.domain.mission.ports import ActionExecutor


TEST_SECRET_TOKEN = "APP_USR_SECRET_TOKEN_12345"
TEST_SECRET_REFRESH = "APP_USR_SECRET_REFRESH_67890"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "n3_authz_db"


@pytest.fixture
def identity_repo(data_dir: Path) -> JsonIdentityRepository:
    return JsonIdentityRepository(data_dir / "identity")


@pytest.fixture
def identity_service(identity_repo: JsonIdentityRepository) -> IdentityService:
    return IdentityService(identity_repo)


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
def virtual_clock() -> VirtualClock:
    start_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def auth_service(
    identity_service: IdentityService,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> AuthenticationService:
    return AuthenticationService(
        identity_service=identity_service,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
        trusted_internal_tokens={"internal-token-n3": "autonomous_loop"},
    )


@pytest.fixture
def policy_engine() -> PolicyEngine:
    return PolicyEngine(
        rules=[
            AuthorizationPolicyRule(),
            HumanApprovalPolicyRule(),
            IdempotencyPolicyRule(),
        ]
    )


@pytest.fixture
def authz_service(
    policy_engine: PolicyEngine,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> AuthorizationService:
    return AuthorizationService(
        policy_engine=policy_engine,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
        default_policy_version="1.0.0",
    )


# ---------------------------------------------------------------------------
# Escenario A: N.1 identity -> N.2 AUTHENTICATED -> N.3 ALLOW -> operation boundary mock reached
# ---------------------------------------------------------------------------
def test_scenario_a_pipeline_allow_reaches_boundary(
    auth_service: AuthenticationService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    # 1. N.1 & N.2: Autenticar conexión OAuth
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_90001",
        access_token=TEST_SECRET_TOKEN,
        refresh_token=TEST_SECRET_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=4),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn, correlation_id="corr-scen-a")
    assert auth_res.is_authenticated is True
    principal_ctx = PrincipalContext(principal=auth_res.principal, auth_result=auth_res)

    # 2. Mock de ejecutor físico subyacente
    mock_delegate = MagicMock(spec=ActionExecutor)
    mock_delegate.execute.return_value = {"status": "SUCCESS", "published_item_id": "MLA_ITEM_900"}

    # 3. Guardián de ejecución N.3
    guarded_executor = AuthorizationGuardedActionExecutor(
        authorization_service=authz_service,
        delegate_executor=mock_delegate,
        principal_context=principal_ctx,
        default_allowed_actions=["PUBLISH_LISTING", "READ"],
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publish verified opportunity",
        target="listing:MLA_ITEM_900",
        parameters={"action_type": "PUBLISH_LISTING", "title": "Test Product"},
    )
    state = LoopState(
        mission_id="mission-scen-a",
        iteration=1,
        goal="Publish listing to marketplace",
        current_target="listing:MLA_ITEM_900",
    )

    result = guarded_executor.execute(decision, state)

    # Verificaciones
    assert result["status"] == "SUCCESS"
    assert result["authorization_status"] == "ALLOW"
    assert mock_delegate.execute.call_count == 1
    assert guarded_executor.latest_decision is not None
    assert guarded_executor.latest_decision.status == AuthorizationStatus.ALLOW


# ---------------------------------------------------------------------------
# Escenario B: Authenticated actor -> N.3 DENY -> mock NOT called
# ---------------------------------------------------------------------------
def test_scenario_b_pipeline_deny_blocks_execution(
    auth_service: AuthenticationService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_90002",
        access_token=TEST_SECRET_TOKEN,
        refresh_token=TEST_SECRET_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=4),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn, correlation_id="corr-scen-b")
    principal_ctx = PrincipalContext(principal=auth_res.principal, auth_result=auth_res)

    mock_delegate = MagicMock(spec=ActionExecutor)
    guarded_executor = AuthorizationGuardedActionExecutor(
        authorization_service=authz_service,
        delegate_executor=mock_delegate,
        principal_context=principal_ctx,
        default_allowed_actions=["READ"],
        default_prohibited_actions=["CANCEL_ORDER"],
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Cancel suspicious order",
        target="order:ORD_900",
        parameters={"action_type": "CANCEL_ORDER"},
    )
    state = LoopState(
        mission_id="mission-scen-b",
        iteration=1,
        goal="Cancel order safely",
    )

    result = guarded_executor.execute(decision, state)

    # Verificaciones
    assert result["status"] == "AUTHORIZATION_DENY"
    assert result["is_allowed"] is False
    assert mock_delegate.execute.call_count == 0
    assert guarded_executor.latest_decision.status == AuthorizationStatus.DENY


# ---------------------------------------------------------------------------
# Escenario C: Unauthenticated actor -> blocked (mock NOT called)
# ---------------------------------------------------------------------------
def test_scenario_c_unauthenticated_actor_blocked(
    authz_service: AuthorizationService,
):
    mock_delegate = MagicMock(spec=ActionExecutor)
    guarded_executor = AuthorizationGuardedActionExecutor(
        authorization_service=authz_service,
        delegate_executor=mock_delegate,
        principal_context=None,  # No autenticado
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Attempt publication without auth",
        target="listing:MLA_999",
        parameters={"action_type": "PUBLISH_LISTING"},
    )
    state = LoopState(
        mission_id="mission-scen-c",
        iteration=1,
        goal="Attempt unauthenticated publication",
    )

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "AUTHORIZATION_DENY"
    assert result["is_allowed"] is False
    assert mock_delegate.execute.call_count == 0
    assert AuthorizationReasonCode.MISSING_PRINCIPAL_CONTEXT.value in result["reason_codes"]


# ---------------------------------------------------------------------------
# Escenario D: Same principal: READ allowed, external mutation denied
# ---------------------------------------------------------------------------
def test_scenario_d_same_principal_read_allowed_mutation_denied(
    auth_service: AuthenticationService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_90004",
        access_token=TEST_SECRET_TOKEN,
        refresh_token=TEST_SECRET_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=1),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn)
    principal_ctx = PrincipalContext(principal=auth_res.principal, auth_result=auth_res)

    # 1. READ action (en lista de permitidos)
    req_read = AuthorizationRequest(
        action="READ",
        principal_context=principal_ctx,
        commercial_context={"allowed_actions": ["READ"]},
    )
    dec_read = authz_service.authorize(req_read)
    assert dec_read.status == AuthorizationStatus.ALLOW
    assert dec_read.is_allowed is True

    # 2. External mutation action (DELETE_ACCOUNT / MUTATE_LISTING no permitida)
    req_mutation = AuthorizationRequest(
        action="DELETE_ACCOUNT",
        principal_context=principal_ctx,
        commercial_context={"allowed_actions": ["READ"]},
    )
    dec_mutation = authz_service.authorize(req_mutation)
    assert dec_mutation.status == AuthorizationStatus.DENY
    assert dec_mutation.is_allowed is False


# ---------------------------------------------------------------------------
# Escenario E: Resource / account mismatch -> DENY
# ---------------------------------------------------------------------------
def test_scenario_e_resource_account_mismatch_denied(
    auth_service: AuthenticationService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_ACCOUNT_A",
        access_token=TEST_SECRET_TOKEN,
        refresh_token=TEST_SECRET_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=2),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn)
    principal_ctx = PrincipalContext(principal=auth_res.principal, auth_result=auth_res)

    # Intentar operar sobre un recurso que pertenece a MLA_ACCOUNT_B
    target_res = ResourceReference(
        resource_type="listing",
        resource_id="MLA_LISTING_99",
        account_id="MLA_ACCOUNT_B",
        provider="mercadolibre",
    )

    req = AuthorizationRequest(
        action="UPDATE_PRICE",
        principal_context=principal_ctx,
        resource=target_res,
        commercial_context={"allowed_actions": ["UPDATE_PRICE"]},
    )

    decision = authz_service.authorize(req)

    assert decision.status == AuthorizationStatus.DENY
    assert AuthorizationReasonCode.RESOURCE_MISMATCH.value in decision.reason_codes


# ---------------------------------------------------------------------------
# Escenario F: Audit / Trace contains safe decision (no secrets)
# ---------------------------------------------------------------------------
def test_scenario_f_audit_trace_records_safe_decision(
    auth_service: AuthenticationService,
    authz_service: AuthorizationService,
    audit_repo: JsonAuditRepository,
    trace_repo: JsonAgentTraceRepository,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_90006",
        access_token=TEST_SECRET_TOKEN,
        refresh_token=TEST_SECRET_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=3),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn)
    principal_ctx = PrincipalContext(principal=auth_res.principal, auth_result=auth_res)

    corr_id = "corr-audit-trace-f"
    req = AuthorizationRequest(
        action="PUBLISH_LISTING",
        principal_context=principal_ctx,
        resource="listing:MLA_99",
        commercial_context={
            "allowed_actions": ["PUBLISH_LISTING"],
            "api_key": "SENSITIVE_API_KEY",
            "password": "SUPER_SECRET_PASSWORD",
        },
        correlation_id=corr_id,
    )

    decision = authz_service.authorize(req)
    assert decision.status == AuthorizationStatus.ALLOW

    # 1. Verificar registros de auditoría
    audit_records = audit_repo.list_records(
        correlation_id=corr_id,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
    )
    assert len(audit_records) >= 1
    rec = audit_records[0]
    assert rec.actor.actor_id == "usr_mercadolibre_MLA_90006"
    assert rec.metadata["status"] == "ALLOW"
    assert rec.metadata["action"] == "PUBLISH_LISTING"

    # Verificar ausencia de secretos en el dump de auditoría
    audit_dump = json.dumps(dict(rec.metadata))
    assert TEST_SECRET_TOKEN not in audit_dump
    assert "SUPER_SECRET_PASSWORD" not in audit_dump
    assert "SENSITIVE_API_KEY" not in audit_dump

    # 2. Verificar trazas de agente
    traces = trace_repo.list_records(component_name="AuthorizationService")
    assert len(traces) >= 1
    trace_dump = json.dumps([t.__dict__ for t in traces], default=str)
    assert TEST_SECRET_TOKEN not in trace_dump
    assert "SUPER_SECRET_PASSWORD" not in trace_dump


# ---------------------------------------------------------------------------
# Escenario G: Policy version change -> decision reevaluated
# ---------------------------------------------------------------------------
def test_scenario_g_policy_version_change_reevaluates_decision(
    auth_service: AuthenticationService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_90007",
        access_token=TEST_SECRET_TOKEN,
        refresh_token=TEST_SECRET_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=3),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn)
    principal_ctx = PrincipalContext(principal=auth_res.principal, auth_result=auth_res)

    req_v1 = AuthorizationRequest(
        action="UPDATE_INVENTORY",
        principal_context=principal_ctx,
        policy_version="1.0.0",
        commercial_context={"allowed_actions": ["UPDATE_INVENTORY"]},
    )
    dec_v1 = authz_service.authorize(req_v1)

    req_v2 = AuthorizationRequest(
        action="UPDATE_INVENTORY",
        principal_context=principal_ctx,
        policy_version="2.0.0-draft",
        commercial_context={"allowed_actions": ["UPDATE_INVENTORY"]},
    )
    dec_v2 = authz_service.authorize(req_v2)

    assert dec_v1.policy_version == "1.0.0"
    assert dec_v2.policy_version == "2.0.0-draft"
    # Checksums deben diferir debido a la versión de política
    assert dec_v1.checksum != dec_v2.checksum


# ---------------------------------------------------------------------------
# Escenario H: Commercial external action cannot bypass N.3
# ---------------------------------------------------------------------------
def test_scenario_h_commercial_external_action_cannot_bypass_n3(
    auth_service: AuthenticationService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_90008",
        access_token=TEST_SECRET_TOKEN,
        refresh_token=TEST_SECRET_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=1),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn)
    principal_ctx = PrincipalContext(principal=auth_res.principal, auth_result=auth_res)

    mock_delegate = MagicMock(spec=ActionExecutor)
    guarded_executor = AuthorizationGuardedActionExecutor(
        authorization_service=authz_service,
        delegate_executor=mock_delegate,
        principal_context=principal_ctx,
        default_allowed_actions=["READ"],  # Solo READ permitido
    )

    # Intentar ejecutar acción externa de alto impacto: UPDATE_PRICE
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Trigger external price update",
        target="listing:MLA_EXT_999",
        parameters={"action_type": "UPDATE_PRICE", "new_price": 4999.0},
    )
    state = LoopState(
        mission_id="mission-scen-h",
        iteration=1,
        goal="Update external price safely",
    )

    result = guarded_executor.execute(decision, state)

    # Confirmar bloqueo estricto antes del boundary externo
    assert result["is_allowed"] is False
    assert result["authorization_status"] == "DENY"
    assert mock_delegate.execute.call_count == 0


# ---------------------------------------------------------------------------
# Escenario I (E2E N.3): Actor -> N.1 -> N.2 -> N.3 -> ActionExecutor boundary
# ---------------------------------------------------------------------------
def test_scenario_i_e2e_actor_to_action_boundary(
    auth_service: AuthenticationService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    # 1. Actor externo con OAuth Connection
    oauth_conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_SELLER_E2E",
        access_token="LIVE_OAUTH_TOKEN_E2E",
        refresh_token="LIVE_REFRESH_TOKEN_E2E",
        expires_at=virtual_clock.now() + timedelta(hours=8),
    )

    # 2. N.1 & N.2: Autenticación
    auth_res = auth_service.authenticate_oauth_connection(oauth_conn, correlation_id="corr-e2e-n3")
    assert auth_res.status == AuthenticationStatus.AUTHENTICATED
    principal = auth_res.principal
    assert principal.identity_id == "usr_mercadolibre_MLA_SELLER_E2E"

    principal_ctx = PrincipalContext(principal=principal, auth_result=auth_res)

    # 3. N.3 Guardián y ActionExecutor
    mock_delegate = MagicMock(spec=ActionExecutor)
    mock_delegate.execute.return_value = {
        "status": "PUBLISHED",
        "listing_id": "MLA_NEW_12345",
    }

    guarded_executor = AuthorizationGuardedActionExecutor(
        authorization_service=authz_service,
        delegate_executor=mock_delegate,
        principal_context=principal_ctx,
        default_allowed_actions=["PUBLISH_LISTING", "READ"],
    )

    # Caso I.1: Acción autorizada (PUBLISH_LISTING) -> Se ejecuta 1 vez
    dec_allow = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publish verified opportunity",
        target="listing:MLA_NEW_12345",
        parameters={"action_type": "PUBLISH_LISTING"},
    )
    state_1 = LoopState(
        mission_id="mission-e2e-1",
        iteration=1,
        goal="Publish listing to marketplace",
    )
    res_allow = guarded_executor.execute(dec_allow, state_1)
    assert res_allow["status"] == "PUBLISHED"
    assert mock_delegate.execute.call_count == 1

    # Caso I.2: Acción denegada (DELETE_LISTING) -> 0 llamadas adicionales
    dec_deny = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Unauthorized deletion attempt",
        target="listing:MLA_NEW_12345",
        parameters={"action_type": "DELETE_LISTING"},
    )
    state_2 = LoopState(
        mission_id="mission-e2e-2",
        iteration=1,
        goal="Unauthorized deletion mission",
    )
    res_deny = guarded_executor.execute(dec_deny, state_2)
    assert res_deny["is_allowed"] is False
    assert mock_delegate.execute.call_count == 1  # Sigue en 1, no fue llamado
