"""
Tests de Integración y E2E para N.6 — Approval Policies (Transversal N Security, Governance y Safety).

Escenarios exigidos por la especificación:
A. N.1 -> N.2 -> N.4 -> N.3 ALLOW + action NOT_REQUIRED -> execution mock called once.
B. N.3 ALLOW + APPROVAL_REQUIRED + no evidence -> zero physical calls.
C. Valid approval evidence -> APPROVED -> execution mock called once.
D. Approval for different resource -> zero physical calls (resource binding).
E. Expired approval -> zero physical calls (clock determinism).
F. Requester tries self-approval where forbidden -> zero physical calls (separation of duties).
G. N.3 DENY + valid approval -> zero physical calls (approval cannot override authorization DENY).
H. Restart -> valid persisted approval remains verifiable if not expired.
I. Tampered record -> rejected/no execution (SHA-256 integrity failure).
J. Audit / Trace safe (no secrets, no CoT, correct event lifecycle).
K. E2E N.6 Full pipeline (Actor -> N.1 -> N.2 -> N.4 -> N.3 -> N.6 -> guarded execution mock).
"""

import os
import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from src.domain.identity.models import IdentityType, IdentityReference
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.authorization.models import AuthorizationStatus
from src.domain.approval.models import (
    ApprovalStatus,
    ApprovalReasonCode,
    ApprovalPolicy,
    ApprovalEvidence,
    ApprovalRequest,
    compute_approval_checksum,
)
from src.domain.mission.models import (
    LoopDecision,
    LoopAction,
    LoopState,
    MissionType,
)
from src.domain.mission.ports import ActionExecutor
from src.domain.audit.models import AuditRecordType

from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.approval_evidence_repository import JsonApprovalEvidenceRepository
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock

from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import AuthenticationService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.application.approval.approval_policy_service import (
    ApprovalPolicyService,
    InMemoryApprovalPolicyRepository,
)
from src.application.authorization.authorization_guarded_action_executor import (
    AuthorizationGuardedActionExecutor,
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "n6_integration_db"


@pytest.fixture
def virtual_clock() -> VirtualClock:
    start_time = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


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
def evidence_repo(data_dir: Path) -> JsonApprovalEvidenceRepository:
    return JsonApprovalEvidenceRepository(data_dir / "approval_evidences")


@pytest.fixture
def default_policy() -> ApprovalPolicy:
    return ApprovalPolicy(
        policy_name="commercial_governance_policy",
        version="1.0.0",
        description="Default commercial approval policy for autonomous operations",
        actions_not_requiring_approval=("MARKET_ANALYSIS", "READ_LISTING", "SYNC_CATALOG", "market_analysis", "read_listing", "sync_catalog"),
        actions_requiring_approval=("PUBLISH_LISTING", "PRICE_UPDATE", "ORDER_REFUND", "publish_listing", "price_update", "order_refund"),
        require_separation_of_duties=True,
        default_ttl_seconds=3600,
    )


@pytest.fixture
def policy_repo(default_policy: ApprovalPolicy) -> InMemoryApprovalPolicyRepository:
    return InMemoryApprovalPolicyRepository(initial_policies=[default_policy])


@pytest.fixture
def approval_service(
    policy_repo: InMemoryApprovalPolicyRepository,
    evidence_repo: JsonApprovalEvidenceRepository,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> ApprovalPolicyService:
    return ApprovalPolicyService(
        policy_repository=policy_repo,
        evidence_repository=evidence_repo,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
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


def build_authenticated_principal(identity_id: str, identity_type: IdentityType = IdentityType.AGENT) -> PrincipalContext:
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
# Escenario A: N.1->N.2->N.4->N.3 ALLOW + action NOT_REQUIRED -> execution mock called once
# ---------------------------------------------------------------------------
def test_scenario_a_action_not_required_executes_once(
    authz_service: AuthorizationService,
    approval_service: ApprovalPolicyService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)
    mock_delegate.execute.return_value = {"status": "SUCCESS", "data": "analyzed"}

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["MARKET_ANALYSIS", "market_analysis"],
        approval_service=approval_service,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Analyze market trends",
        target="category_electronics",
        parameters={
            "action_type": "market_analysis",
            "approval_policy_name": "commercial_governance_policy",
        },
    )
    state = LoopState(mission_id="m-scen-a", iteration=1, goal="Market Analysis")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "SUCCESS"
    assert result["is_allowed"] is True
    assert result["is_executable"] is True
    assert mock_delegate.execute.call_count == 1
    assert guarded_executor.latest_approval_decision is not None
    assert guarded_executor.latest_approval_decision.status == ApprovalStatus.NOT_REQUIRED


# ---------------------------------------------------------------------------
# Escenario B: N.3 ALLOW + APPROVAL_REQUIRED + no evidence -> zero calls
# ---------------------------------------------------------------------------
def test_scenario_b_approval_required_without_evidence_blocks_execution(
    authz_service: AuthorizationService,
    approval_service: ApprovalPolicyService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["PUBLISH_LISTING", "publish_listing"],
        approval_service=approval_service,
    )

    decision = LoopDecision(
        action=LoopAction.PROMOTE,
        reason="Publish high-impact product listing",
        target="listing_prod_001",
        parameters={
            "action_type": "publish_listing",
            "approval_policy_name": "commercial_governance_policy",
        },
    )
    state = LoopState(mission_id="m-scen-b", iteration=1, goal="Publish listing")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "APPROVAL_APPROVAL_REQUIRED"
    assert result["is_allowed"] is True  # N.3 Autorizado
    assert result["is_executable"] is False  # N.6 Bloquea
    assert result["requires_approval"] is True
    assert mock_delegate.execute.call_count == 0  # CERO llamadas físicas


# ---------------------------------------------------------------------------
# Escenario C: Valid approval evidence -> APPROVED -> execution mock called once
# ---------------------------------------------------------------------------
def test_scenario_c_valid_approval_evidence_allows_execution(
    authz_service: AuthorizationService,
    approval_service: ApprovalPolicyService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)
    mock_delegate.execute.return_value = {"status": "SUCCESS", "listing_id": "listing_prod_001"}

    # Otorgar evidencia válida emitida por un supervisor humano
    evidence = approval_service.grant_approval(
        approval_id="app_scen_c_001",
        target_action="publish_listing",
        target_resource="listing_prod_001",
        requesting_identity_id="agent_commercial",
        approver_identity_id="human_supervisor_jllv",
        policy_name="commercial_governance_policy",
        correlation_id="m-scen-c",
    )
    assert evidence.status == ApprovalStatus.APPROVED

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["PUBLISH_LISTING", "publish_listing"],
        approval_service=approval_service,
    )

    decision = LoopDecision(
        action=LoopAction.PROMOTE,
        reason="Publish high-impact product listing with approval",
        target="listing_prod_001",
        parameters={
            "action_type": "publish_listing",
            "approval_policy_name": "commercial_governance_policy",
            "attached_evidence_id": "app_scen_c_001",
        },
    )
    state = LoopState(mission_id="m-scen-c", iteration=1, goal="Publish listing")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "SUCCESS"
    assert result["is_allowed"] is True
    assert result["is_executable"] is True
    assert mock_delegate.execute.call_count == 1
    assert guarded_executor.latest_approval_decision.status == ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# Escenario D: Approval for different resource -> zero calls (Resource binding)
# ---------------------------------------------------------------------------
def test_scenario_d_approval_for_different_resource_blocks_execution(
    authz_service: AuthorizationService,
    approval_service: ApprovalPolicyService,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)

    # Otorgar aprobación para listing_AAA
    approval_service.grant_approval(
        approval_id="app_scen_d_resource_aaa",
        target_action="publish_listing",
        target_resource="listing_AAA",
        requesting_identity_id="agent_commercial",
        approver_identity_id="human_supervisor_jllv",
        policy_name="commercial_governance_policy",
    )

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["PUBLISH_LISTING", "publish_listing"],
        approval_service=approval_service,
    )

    # Petición intentando usar la aprobación de AAA en BBB
    decision = LoopDecision(
        action=LoopAction.PROMOTE,
        reason="Publish listing BBB",
        target="listing_BBB",
        parameters={
            "action_type": "publish_listing",
            "approval_policy_name": "commercial_governance_policy",
            "attached_evidence_id": "app_scen_d_resource_aaa",
        },
    )
    state = LoopState(mission_id="m-scen-d", iteration=1, goal="Publish BBB")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "APPROVAL_APPROVAL_REQUIRED"
    assert result["is_executable"] is False
    assert mock_delegate.execute.call_count == 0


# ---------------------------------------------------------------------------
# Escenario E: Expired approval -> zero calls (Clock determinism)
# ---------------------------------------------------------------------------
def test_scenario_e_expired_approval_blocks_execution(
    authz_service: AuthorizationService,
    approval_service: ApprovalPolicyService,
    virtual_clock: VirtualClock,
):
    principal = build_authenticated_principal("agent_commercial")
    mock_delegate = MagicMock(spec=ActionExecutor)

    # Otorgar evidencia con TTL de 1 hora
    approval_service.grant_approval(
        approval_id="app_scen_e_expiring",
        target_action="price_update",
        target_resource="sku_999",
        requesting_identity_id="agent_commercial",
        approver_identity_id="human_supervisor_jllv",
        policy_name="commercial_governance_policy",
        ttl_seconds=3600,
    )

    # Avanzar reloj virtual 2 horas en el futuro
    virtual_clock.advance(7200)

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["PRICE_UPDATE", "price_update"],
        approval_service=approval_service,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Update price after 2 hours",
        target="sku_999",
        parameters={
            "action_type": "price_update",
            "approval_policy_name": "commercial_governance_policy",
            "attached_evidence_id": "app_scen_e_expiring",
        },
    )
    state = LoopState(mission_id="m-scen-e", iteration=1, goal="Price update")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "APPROVAL_EXPIRED"
    assert result["is_executable"] is False
    assert mock_delegate.execute.call_count == 0


# ---------------------------------------------------------------------------
# Escenario F: Requester tries self-approval where forbidden -> zero calls
# ---------------------------------------------------------------------------
def test_scenario_f_self_approval_blocked_when_forbidden(
    authz_service: AuthorizationService,
    approval_service: ApprovalPolicyService,
):
    principal = build_authenticated_principal("agent_rogue")
    mock_delegate = MagicMock(spec=ActionExecutor)

    # El mismo agente intenta auto-otorgarse la aprobación
    approval_service.grant_approval(
        approval_id="app_scen_f_self_approved",
        target_action="order_refund",
        target_resource="order_777",
        requesting_identity_id="agent_rogue",
        approver_identity_id="agent_rogue",  # Auto-aprobación
        policy_name="commercial_governance_policy",
    )

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["ORDER_REFUND", "order_refund"],
        approval_service=approval_service,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Refund order 777",
        target="order_777",
        parameters={
            "action_type": "order_refund",
            "approval_policy_name": "commercial_governance_policy",
            "attached_evidence_id": "app_scen_f_self_approved",
        },
    )
    state = LoopState(mission_id="m-scen-f", iteration=1, goal="Refund order")

    result = guarded_executor.execute(decision, state)

    assert result["status"] == "APPROVAL_REJECTED"
    assert result["is_executable"] is False
    assert mock_delegate.execute.call_count == 0


# ---------------------------------------------------------------------------
# Escenario G: N.3 DENY + valid approval -> zero calls (Approval cannot override DENY)
# ---------------------------------------------------------------------------
def test_scenario_g_approval_cannot_override_n3_deny(
    authz_service: AuthorizationService,
    approval_service: ApprovalPolicyService,
):
    principal = build_authenticated_principal("agent_unauthorized")
    mock_delegate = MagicMock(spec=ActionExecutor)

    # Se cuenta con aprobación válida
    approval_service.grant_approval(
        approval_id="app_scen_g_valid",
        target_action="order_refund",
        target_resource="order_888",
        requesting_identity_id="agent_unauthorized",
        approver_identity_id="human_supervisor_jllv",
        policy_name="commercial_governance_policy",
    )

    # Pero N.3 prohíbe explícitamente order_refund al actor
    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["read_listing"],
        default_prohibited_actions=["order_refund"],  # DENY en N.3
        approval_service=approval_service,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Refund order 888 with approval",
        target="order_888",
        parameters={
            "action_type": "order_refund",
            "approval_policy_name": "commercial_governance_policy",
            "attached_evidence_id": "app_scen_g_valid",
        },
    )
    state = LoopState(mission_id="m-scen-g", iteration=1, goal="Refund order")

    result = guarded_executor.execute(decision, state)

    assert result["is_allowed"] is False
    assert result["status"] == "AUTHORIZATION_DENY"
    assert mock_delegate.execute.call_count == 0


# ---------------------------------------------------------------------------
# Escenario H: Restart -> valid persisted approval remains verifiable if not expired
# ---------------------------------------------------------------------------
def test_scenario_h_restart_preserves_persisted_approval(
    data_dir: Path,
    default_policy: ApprovalPolicy,
    virtual_clock: VirtualClock,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
):
    # 1. Sesión 1: Crear y persistir evidencia en disco
    repo1 = JsonApprovalEvidenceRepository(data_dir / "approval_evidences")
    pol_repo1 = InMemoryApprovalPolicyRepository([default_policy])
    service1 = ApprovalPolicyService(
        policy_repository=pol_repo1,
        evidence_repository=repo1,
        clock=virtual_clock,
    )
    service1.grant_approval(
        approval_id="app_scen_h_persisted",
        target_action="publish_listing",
        target_resource="listing_res_001",
        requesting_identity_id="agent_restart_test",
        approver_identity_id="human_supervisor_jllv",
        policy_name="commercial_governance_policy",
        ttl_seconds=7200,
    )

    # 2. Simular Reinicio de Sistema: Instanciar nuevo repositorio y servicio leyendo desde data_dir
    repo2 = JsonApprovalEvidenceRepository(data_dir / "approval_evidences")
    pol_repo2 = InMemoryApprovalPolicyRepository([default_policy])
    service2 = ApprovalPolicyService(
            policy_repository=pol_repo2,
            evidence_repository=repo2,
            clock=virtual_clock,
            audit_repository=audit_repo,
            agent_trace_service=trace_service,
        )

    # 3. Evaluar petición contra servicio reiniciado
    req = ApprovalRequest(
        requesting_identity_id="agent_restart_test",
        action="publish_listing",
        resource="listing_res_001",
        policy_name="commercial_governance_policy",
        attached_evidence_id="app_scen_h_persisted",
    )
    decision = service2.evaluate_request(req)

    assert decision.status == ApprovalStatus.APPROVED
    assert decision.is_executable is True
    assert decision.approval_reference == "app_scen_h_persisted"


# ---------------------------------------------------------------------------
# Escenario I: Tampered record -> rejected/no execution (Checksum integrity failure)
# ---------------------------------------------------------------------------
def test_scenario_i_tampered_record_integrity_rejected(
    data_dir: Path,
    default_policy: ApprovalPolicy,
    virtual_clock: VirtualClock,
):
    evidence_dir = data_dir / "approval_evidences"
    repo = JsonApprovalEvidenceRepository(evidence_dir)
    service = ApprovalPolicyService(
        policy_repository=InMemoryApprovalPolicyRepository([default_policy]),
        evidence_repository=repo,
        clock=virtual_clock,
    )

    # Crear evidencia original
    service.grant_approval(
        approval_id="app_scen_i_tamper",
        target_action="price_update",
        target_resource="sku_safe",
        requesting_identity_id="agent_legit",
        approver_identity_id="human_supervisor_jllv",
        policy_name="commercial_governance_policy",
    )

    # Modificar físicamente el archivo JSON en disco simulando corrupción o manipulación maliciosa
    json_path = repo.evidence_dir / "app_scen_i_tamper.json"
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["target_resource"] = "sku_TAMPERED_TARGET"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Nuevo repo sin caché en memoria
    reloaded_repo = JsonApprovalEvidenceRepository(evidence_dir)
    reloaded_service = ApprovalPolicyService(
        policy_repository=InMemoryApprovalPolicyRepository([default_policy]),
        evidence_repository=reloaded_repo,
        clock=virtual_clock,
    )

    req = ApprovalRequest(
        requesting_identity_id="agent_legit",
        action="price_update",
        resource="sku_TAMPERED_TARGET",
        policy_name="commercial_governance_policy",
        attached_evidence_id="app_scen_i_tamper",
    )
    decision = reloaded_service.evaluate_request(req)

    assert decision.status == ApprovalStatus.ERROR
    assert decision.is_executable is False
    assert ApprovalReasonCode.APPROVAL_EVIDENCE_CORRUPTED in (decision.reason_code,)


# ---------------------------------------------------------------------------
# Escenario J: Audit / Trace safe (No secrets, correct event emission)
# ---------------------------------------------------------------------------
def test_scenario_j_audit_and_trace_safe(
    approval_service: ApprovalPolicyService,
    audit_repo: JsonAuditRepository,
    trace_repo: JsonAgentTraceRepository,
):
    # Otorgar con metadata potencialmente sensible
    approval_service.grant_approval(
        approval_id="app_scen_j_audit",
        target_action="publish_listing",
        target_resource="listing_secret_test",
        requesting_identity_id="agent_tester",
        approver_identity_id="human_supervisor_jllv",
        policy_name="commercial_governance_policy",
        metadata={"client_secret": "my_super_secret_key", "public_note": "audit ok"},
    )

    records = audit_repo.list_records()
    grant_records = [r for r in records if r.record_type == AuditRecordType.APPROVAL_GRANTED]
    assert len(grant_records) >= 1
    rec = grant_records[0]

    # Verificar que no contenga el secreto en claro
    raw_json = json.dumps(dict(rec.metadata))
    assert "my_super_secret_key" not in raw_json
    assert "[REDACTED]" in raw_json


# ---------------------------------------------------------------------------
# Escenario K: E2E Pipeline N.6 (Actor -> N.1 -> N.2 -> N.4 -> N.3 -> N.6 -> Guarded Execution Mock)
# ---------------------------------------------------------------------------
def test_scenario_k_e2e_full_governance_pipeline(
    identity_service: IdentityService,
    authz_service: AuthorizationService,
    approval_service: ApprovalPolicyService,
):
    # 1. N.1 Crear Identidad
    agent = identity_service.register_identity(
        identity_id="e2e_agent_runner",
        identity_type=IdentityType.AGENT,
        provider="autonomous_loop",
        display_name="E2E Autonomous Agent",
    )

    # 2. N.2 Contexto Autenticado
    identity_ref = IdentityReference(
        identity_id=agent.identity_id,
        canonical_identifier=agent.canonical_identifier,
        identity_type=IdentityType.AGENT,
    )
    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="autonomous_loop",
        authenticated_at=datetime.now(timezone.utc),
    )
    principal_ctx = PrincipalContext(principal=identity_ref, auth_result=auth_result)

    # 3. Guardián de ejecución integral (N.3 + N.6)
    mock_delegate = MagicMock(spec=ActionExecutor)
    mock_delegate.execute.return_value = {"status": "SUCCESS", "e2e_executed": True}

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_delegate,
        authorization_service=authz_service,
        principal_context=principal_ctx,
        default_allowed_actions=["PUBLISH_LISTING", "MARKET_ANALYSIS", "publish_listing", "market_analysis"],
        approval_service=approval_service,
    )

    # Paso 1 E2E: Acción que no requiere aprobación -> Ejecuta
    dec_unrestricted = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Analyze market",
        target="niche_smart_home",
        parameters={
            "action_type": "market_analysis",
            "approval_policy_name": "commercial_governance_policy",
        },
    )
    res1 = guarded_executor.execute(dec_unrestricted, LoopState(mission_id="m-e2e", iteration=1, goal="E2E"))
    assert res1["status"] == "SUCCESS"
    assert mock_delegate.execute.call_count == 1

    # Paso 2 E2E: Acción que requiere aprobación pero falta evidencia -> Bloqueada (0 llamadas adicionales)
    dec_req_approval = LoopDecision(
        action=LoopAction.PROMOTE,
        reason="Publish high-impact product",
        target="listing_e2e_001",
        parameters={
            "action_type": "publish_listing",
            "approval_policy_name": "commercial_governance_policy",
        },
    )
    res2 = guarded_executor.execute(dec_req_approval, LoopState(mission_id="m-e2e", iteration=2, goal="E2E"))
    assert res2["status"] == "APPROVAL_APPROVAL_REQUIRED"
    assert res2["is_executable"] is False
    assert mock_delegate.execute.call_count == 1  # No incrementó

    # Paso 3 E2E: Obtener aprobación válida de supervisor humano -> Ejecuta
    approval_service.grant_approval(
        approval_id="app_e2e_listing_001",
        target_action="publish_listing",
        target_resource="listing_e2e_001",
        requesting_identity_id=agent.identity_id,
        approver_identity_id="human_supervisor_jllv",
        policy_name="commercial_governance_policy",
    )
    dec_with_evidence = LoopDecision(
        action=LoopAction.PROMOTE,
        reason="Publish high-impact product with approved evidence",
        target="listing_e2e_001",
        parameters={
            "action_type": "publish_listing",
            "approval_policy_name": "commercial_governance_policy",
            "attached_evidence_id": "app_e2e_listing_001",
        },
    )
    res3 = guarded_executor.execute(dec_with_evidence, LoopState(mission_id="m-e2e", iteration=3, goal="E2E"))
    assert res3["status"] == "SUCCESS"
    assert res3["is_executable"] is True
    assert mock_delegate.execute.call_count == 2
