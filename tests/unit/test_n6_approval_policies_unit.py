"""
Unit Tests for N.6 — Approval Policies (Transversal N — Security, Governance & Safety).

Mínimo 16 requerimientos canónicos:
1. action not requiring approval -> NOT_REQUIRED
2. action requiring approval -> APPROVAL_REQUIRED
3. valid approval -> APPROVED
4. explicit rejection -> REJECTED
5. expired approval -> no approval (EXPIRED)
6. UNKNOWN policy -> no execution (UNKNOWN)
7. approval bound to action
8. approval bound to resource
9. wrong requester/approver binding rejected
10. self-approval blocked when separation required
11. deterministic decision
12. policy versioning
13. corrupt evidence rejected
14. approval cannot override N.3 DENY
15. sanitized metadata
16. no N.7 financial thresholds
"""

import pytest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from src.domain.approval.models import (
    ApprovalStatus,
    ApprovalReasonCode,
    ApprovalPolicy,
    ApprovalEvidence,
    ApprovalRequest,
    ApprovalDecision,
    compute_approval_checksum,
)
from src.domain.approval.ports import ApprovalPolicyRepositoryPort, ApprovalEvidenceRepositoryPort
from src.infrastructure.persistence.data.json.approval_evidence_repository import (
    JsonApprovalEvidenceRepository,
    ApprovalEvidenceConflictError,
    CorruptedApprovalEvidenceRecordError,
)
from src.application.approval.approval_policy_service import (
    ApprovalPolicyService,
    InMemoryApprovalPolicyRepository,
)
from src.infrastructure.reliability.reliability_infrastructure import SystemClock
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.authentication.models import (
    PrincipalContext,
    AuthenticationStatus,
    AuthenticationResult,
    AuthenticationMethod,
)
from src.domain.authorization.models import (
    AuthorizationRequest,
    AuthorizationDecision,
    AuthorizationStatus,
    AuthorizationReasonCode,
)
from src.application.authorization.authorization_service import AuthorizationService
from src.application.authorization.authorization_guarded_action_executor import AuthorizationGuardedActionExecutor
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor


class DummyMockExecutor(ActionExecutor):
    def __init__(self):
        self.call_count = 0
        self.last_decision = None

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        self.call_count += 1
        self.last_decision = decision
        return {"status": "SUCCESS", "executed": True}


class FreezeClock:
    def __init__(self, current_time: datetime):
        self._now = current_time

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int):
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def base_clock():
    return FreezeClock(datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def evidence_repo(tmp_path):
    return JsonApprovalEvidenceRepository(base_dir=tmp_path / "approval_storage")


@pytest.fixture
def approval_service(evidence_repo, base_clock):
    policy = ApprovalPolicy(
        policy_name="governance_policy",
        version="1.0.0",
        actions_not_requiring_approval=("market.query", "catalog.view"),
        actions_requiring_approval=("listing.publish", "price.update"),
        prohibited_actions=("system.shutdown",),
        require_separation_of_duties=True,
        default_ttl_seconds=3600,
    )
    repo = InMemoryApprovalPolicyRepository([policy])
    return ApprovalPolicyService(
        evidence_repository=evidence_repo,
        policy_repository=repo,
        clock=base_clock,
    )


# 1. Action not requiring approval -> NOT_REQUIRED
def test_1_action_not_requiring_approval_returns_not_required(approval_service):
    req = ApprovalRequest(
        action="market.query",
        resource="product_123",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.NOT_REQUIRED
    assert decision.reason_code == ApprovalReasonCode.APPROVAL_NOT_REQUIRED
    assert decision.is_executable is True


# 2. Action requiring approval -> APPROVAL_REQUIRED
def test_2_action_requiring_approval_returns_approval_required(approval_service):
    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.APPROVAL_REQUIRED
    assert decision.reason_code == ApprovalReasonCode.APPROVAL_REQUIRED_BY_POLICY
    assert decision.is_executable is False


# 3. Valid approval -> APPROVED
def test_3_valid_approval_returns_approved(approval_service):
    # Conceder aprobación
    approval_service.grant_approval(
        approval_id="app_001",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_worker",
        approver_identity_id="human_supervisor",
        policy_name="governance_policy",
        policy_version="1.0.0",
    )

    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
        attached_evidence_id="app_001",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.APPROVED
    assert decision.reason_code == ApprovalReasonCode.VALID_APPROVAL_ATTACHED
    assert decision.is_executable is True
    assert decision.approver_identity_id == "human_supervisor"


# 4. Explicit rejection -> REJECTED
def test_4_explicit_rejection_returns_rejected(approval_service):
    approval_service.reject_approval(
        approval_id="app_002",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_worker",
        approver_identity_id="human_supervisor",
        rejection_reason="Price error detected in draft",
        policy_name="governance_policy",
    )

    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
        attached_evidence_id="app_002",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.REJECTED
    assert decision.reason_code == ApprovalReasonCode.APPROVAL_REJECTED_BY_OPERATOR
    assert decision.is_executable is False


# 5. Expired approval -> no approval (EXPIRED)
def test_5_expired_approval_returns_expired(approval_service, base_clock):
    approval_service.grant_approval(
        approval_id="app_003",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_worker",
        approver_identity_id="human_supervisor",
        policy_name="governance_policy",
        ttl_seconds=300,  # 5 minutos
    )

    # Avanzar reloj 10 minutos
    base_clock.advance(600)

    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
        attached_evidence_id="app_003",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.EXPIRED
    assert decision.reason_code == ApprovalReasonCode.APPROVAL_EVIDENCE_EXPIRED
    assert decision.is_executable is False


# 6. UNKNOWN policy -> no execution (UNKNOWN)
def test_6_unknown_policy_returns_unknown(approval_service):
    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="non_existent_policy",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.UNKNOWN
    assert decision.reason_code == ApprovalReasonCode.UNKNOWN_APPROVAL_POLICY
    assert decision.is_executable is False


# 7. Approval bound to action
def test_7_approval_bound_to_action(approval_service):
    approval_service.grant_approval(
        approval_id="app_004",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_worker",
        approver_identity_id="human_supervisor",
        policy_name="governance_policy",
    )

    req = ApprovalRequest(
        action="price.update",  # Acción diferente
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
        attached_evidence_id="app_004",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.APPROVAL_REQUIRED
    assert decision.reason_code == ApprovalReasonCode.ACTION_MISMATCH
    assert decision.is_executable is False


# 8. Approval bound to resource
def test_8_approval_bound_to_resource(approval_service):
    approval_service.grant_approval(
        approval_id="app_005",
        target_action="listing.publish",
        target_resource="listing_A",
        requesting_identity_id="agent_worker",
        approver_identity_id="human_supervisor",
        policy_name="governance_policy",
    )

    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_B",  # Recurso diferente
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
        attached_evidence_id="app_005",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.APPROVAL_REQUIRED
    assert decision.reason_code == ApprovalReasonCode.RESOURCE_MISMATCH
    assert decision.is_executable is False


# 9. Wrong requester/approver binding rejected
def test_9_wrong_requester_binding_rejected(approval_service):
    approval_service.grant_approval(
        approval_id="app_006",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_worker_1",
        approver_identity_id="human_supervisor",
        policy_name="governance_policy",
    )

    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker_2",  # Solicitante diferente
        policy_name="governance_policy",
        attached_evidence_id="app_006",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.APPROVAL_REQUIRED
    assert decision.reason_code == ApprovalReasonCode.REQUESTER_MISMATCH
    assert decision.is_executable is False


# 10. Self-approval blocked when separation required
def test_10_self_approval_blocked_when_separation_required(approval_service):
    approval_service.grant_approval(
        approval_id="app_007",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_worker",
        approver_identity_id="agent_worker",  # Mismo agente que solicita
        policy_name="governance_policy",
    )

    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
        attached_evidence_id="app_007",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.REJECTED
    assert decision.reason_code == ApprovalReasonCode.SELF_APPROVAL_FORBIDDEN
    assert decision.is_executable is False


# 11. Deterministic decision
def test_11_deterministic_decision(approval_service):
    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
    )
    d1 = approval_service.evaluate_request(req)
    d2 = approval_service.evaluate_request(req)
    assert d1.status == d2.status
    assert d1.reason_code == d2.reason_code
    assert d1.policy_name == d2.policy_name


# 12. Policy versioning
def test_12_policy_versioning_mismatch_rejected(approval_service):
    approval_service.grant_approval(
        approval_id="app_008",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_worker",
        approver_identity_id="human_supervisor",
        policy_name="governance_policy",
        policy_version="0.9.0",  # Versión anterior
    )

    req = ApprovalRequest(
        action="listing.publish",
        resource="listing_999",
        requesting_identity_id="agent_worker",
        policy_name="governance_policy",
        attached_evidence_id="app_008",
    )
    decision = approval_service.evaluate_request(req)
    assert decision.status == ApprovalStatus.APPROVAL_REQUIRED
    assert decision.reason_code == ApprovalReasonCode.POLICY_VERSION_MISMATCH
    assert decision.is_executable is False


# 13. Corrupt evidence rejected
def test_13_corrupt_evidence_checksum_rejected(tmp_path, base_clock):
    repo = JsonApprovalEvidenceRepository(base_dir=tmp_path / "corrupt_storage")
    ev = ApprovalEvidence(
        approval_id="app_tamper",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_worker",
        approver_identity_id="human_supervisor",
        policy_name="governance_policy",
        policy_version="1.0.0",
        status=ApprovalStatus.APPROVED,
        approved_at=base_clock.now(),
    )
    repo.save_evidence(ev)

    # Manipular el archivo JSON directamente en disco
    target_file = tmp_path / "corrupt_storage" / "approval_evidence" / "app_tamper.json"
    content = target_file.read_text(encoding="utf-8")
    tampered_content = content.replace("listing.publish", "admin.wipe_all")
    target_file.write_text(tampered_content, encoding="utf-8")

    with pytest.raises(CorruptedApprovalEvidenceRecordError):
        repo.get_by_id("app_tamper")


# 14. Approval cannot override N.3 DENY
def test_14_approval_cannot_override_n3_deny(approval_service, tmp_path):
    authz_service = AuthorizationService(
        policy_engine=None,
        audit_repository=None,
    )
    mock_executor = DummyMockExecutor()
    identity_ref = IdentityReference(
        identity_id="agent_restricted",
        canonical_identifier="canonical_agent_restricted",
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

    # Guardián configurado con N.3 y N.6
    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=mock_executor,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("read_only",),
        default_prohibited_actions=("listing.publish",),  # DENY en N.3
        approval_service=approval_service,
    )

    # Otorgar aprobación válida en N.6
    approval_service.grant_approval(
        approval_id="app_valid_but_denied",
        target_action="listing.publish",
        target_resource="listing_999",
        requesting_identity_id="agent_restricted",
        approver_identity_id="human_supervisor",
        policy_name="governance_policy",
    )

    loop_dec = LoopDecision(
        action=LoopAction.PROMOTE,
        reason="Publishing listing 999",
        target="listing_999",
        parameters={
            "action_type": "listing.publish",
            "approval_policy_name": "governance_policy",
            "attached_evidence_id": "app_valid_but_denied",
        },
    )
    loop_state = LoopState(
        mission_id="m-123",
        iteration=1,
        goal="publish listing",
    )

    result = guarded_executor.execute(loop_dec, loop_state)
    assert result["is_allowed"] is False
    assert result["status"] == "AUTHORIZATION_DENY"
    assert mock_executor.call_count == 0  # Cero llamadas físicas


# 15. Sanitized metadata
def test_15_sanitized_metadata():
    policy = ApprovalPolicy(
        policy_name="test_sanitized_policy",
        metadata={"client_secret": "sensitive_password_123", "safe_tag": "ok"},
    )
    assert policy.metadata["client_secret"] == "[REDACTED]"
    assert policy.metadata["safe_tag"] == "ok"

    ev = ApprovalEvidence(
        approval_id="app_meta",
        target_action="test.act",
        target_resource="res_1",
        requesting_identity_id="req_1",
        approver_identity_id="appr_1",
        policy_name="test_sanitized_policy",
        policy_version="1.0.0",
        metadata={"api_key": "secret_api_key_456", "note": "approved manually"},
    )
    assert ev.metadata["api_key"] == "[REDACTED]"
    assert ev.metadata["note"] == "approved manually"


# 16. No N.7 financial thresholds in N.6
def test_16_no_n7_financial_thresholds_in_n6():
    # Comprobar que los modelos de N.6 no definen campos de límites monetarios
    for cls in (ApprovalPolicy, ApprovalEvidence, ApprovalRequest, ApprovalDecision):
        field_names = [f.name for f in cls.__dataclass_fields__.values()]
        assert "max_amount" not in field_names
        assert "daily_limit" not in field_names
        assert "transaction_limit" not in field_names
        assert "spend_quota" not in field_names
        assert "currency_ceiling" not in field_names
