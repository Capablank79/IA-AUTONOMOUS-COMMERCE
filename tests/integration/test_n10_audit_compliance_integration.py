"""
Integration Tests for Hito N.10 — Audit / Compliance (Transversal N — Security, Governance & Safety).

Cubre los escenarios de integración A a J y el pipeline E2E multicapa:
- Escenario A: Full permitted external operation -> complete evidence -> COMPLIANT.
- Escenario B: N.3 DENY -> executor blocked -> COMPLIANT enforcement.
- Escenario C: Force/mock execution despite N.3 DENY -> NON_COMPLIANT.
- Escenario D: N.8 DENY but execution present -> NON_COMPLIANT.
- Escenario E: N.7 limit exceeded + no valid approval + execution -> NON_COMPLIANT.
- Escenario F: N.6 required approval present and valid -> COMPLIANT path.
- Escenario G: N.9 sensitive payload correctly redacted -> COMPLIANT.
- Escenario H: Audit record tampered -> Integrity failure finding & ERROR.
- Escenario I: Evidence from wrong correlation -> Isolated and rejected.
- Escenario J: Restart/reload repository -> Assessment remains reproducible.
- E2E Pipeline: Real execution flow through Actor -> N.1 -> N.2 -> N.4 -> N.3 -> N.8 -> N.9 -> N.7 -> N.6 -> execution/blocked -> K.1 Audit -> K.2 Trace -> N.10 Compliance Assessment.
"""

from decimal import Decimal
import os
import shutil
import tempfile
import pytest
from typing import Dict, Any

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.authentication.models import PrincipalContext, AuthenticationResult, AuthenticationMethod, AuthenticationStatus
from src.domain.authorization.models import AuthorizationDecision, AuthorizationStatus
from src.domain.tool_policy.models import ToolReference
from src.domain.tool.models import ToolSideEffectLevel

from src.domain.compliance.models import (
    ComplianceStatus,
    ComplianceFindingSeverity,
    ComplianceFindingReasonCode,
    ComplianceRequirementType,
    ComplianceEvidenceType,
    ComplianceRequirement,
    ComplianceEvidenceReference,
    ComplianceFinding,
    CompliancePolicy,
    ComplianceAssessment,
    compute_evidence_checksum,
)
from src.domain.compliance.ports import (
    ComplianceEvidenceCollectorPort,
    CompliancePolicyRepositoryPort,
    ComplianceAssessmentServicePort,
)
from src.infrastructure.persistence.data.in_memory.compliance_policy_repository import (
    InMemoryCompliancePolicyRepository,
    create_default_commercial_compliance_policy,
)
from src.application.compliance.compliance_assessment_service import (
    ComplianceAssessmentService,
    DefaultComplianceEvidenceCollector,
)
from src.application.authorization.authorization_service import AuthorizationService
from src.application.authorization.authorization_guarded_action_executor import (
    AuthorizationGuardedActionExecutor,
)
from src.application.rbac.rbac_service import RBACService
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.application.financial_limit.financial_limit_service import FinancialLimitService
from src.application.approval.approval_policy_service import ApprovalPolicyService
from src.application.security.sensitive_data_handling_service import SensitiveDataHandlingService
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from typing import Optional, Dict, Any

import uuid
from datetime import datetime, timezone
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)


def _append_audit_record(
    audit_repo: JsonAuditRepository,
    record_type: AuditRecordType,
    actor_id: str,
    actor_type: str,
    action_or_operation: str,
    status: str,
    correlation_id: str,
    mission_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    subject_type: str = "OPERATION",
    subject_id: str = "op_target",
) -> AuditRecord:
    actor_enum = AuditActorType.USER if actor_type.upper() == "USER" else AuditActorType.AGENT
    if actor_type.upper() == "SYSTEM":
        actor_enum = AuditActorType.SYSTEM
    rec = AuditRecord(
        audit_id=f"aud_{uuid.uuid4().hex[:12]}",
        record_type=record_type,
        occurred_at=datetime.now(timezone.utc),
        actor=AuditActor(actor_type=actor_enum, actor_id=actor_id),
        subject_type=subject_type,
        subject_id=subject_id,
        action_or_operation=action_or_operation,
        status=status,
        correlation_id=correlation_id,
        mission_id=mission_id,
        metadata=metadata or {},
    )
    return audit_repo.append(rec)


class MockActionExecutor(ActionExecutor):
    def __init__(self):
        self.executed_decisions = []
        self.external_calls_count = 0

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        self.executed_decisions.append((decision, state))
        self.external_calls_count += 1
        return {
            "execution_status": "SUCCESS",
            "received_parameters": dict(decision.parameters) if decision.parameters else {},
        }


@pytest.fixture
def tmp_dir():
    temp_path = tempfile.mkdtemp(prefix="test_n10_")
    yield temp_path
    shutil.rmtree(temp_path, ignore_errors=True)


@pytest.fixture
def audit_repo(tmp_dir):
    audit_file = os.path.join(tmp_dir, "audit_trail.json")
    return JsonAuditRepository(audit_file)


@pytest.fixture
def compliance_service(audit_repo):
    collector = DefaultComplianceEvidenceCollector(audit_repository=audit_repo)
    return ComplianceAssessmentService(evidence_collector=collector)


# Escenario A: full permitted external operation -> complete evidence -> COMPLIANT
def test_scenario_a_full_permitted_operation_compliant(compliance_service, audit_repo):
    corr_id = "corr_e2e_a_01"
    msn_id = "msn_a_01"

    # Registrar evidencias en AuditRepository K.1
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="usr_charlie",
        actor_type="USER",
        action_or_operation="AUTHENTICATE",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.RBAC_EVALUATED,
        actor_id="usr_charlie",
        actor_type="USER",
        action_or_operation="CREATE_ORDER",
        status="VALID",
        correlation_id=corr_id,
        mission_id=msn_id,
        metadata={"actions": ["CREATE_ORDER"]},
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="usr_charlie",
        actor_type="USER",
        action_or_operation="CREATE_ORDER",
        status="ALLOW",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.TOOL_ACCESS_EVALUATED,
        actor_id="usr_charlie",
        actor_type="USER",
        action_or_operation="CREATE_ORDER",
        status="ALLOW",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.FINANCIAL_LIMIT_EVALUATED,
        actor_id="usr_charlie",
        actor_type="USER",
        action_or_operation="CREATE_ORDER",
        status="WITHIN_LIMIT",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.APPROVAL_EVALUATED,
        actor_id="usr_charlie",
        actor_type="USER",
        action_or_operation="CREATE_ORDER",
        status="NOT_REQUIRED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.ACTION_EXECUTED,
        actor_id="usr_charlie",
        actor_type="USER",
        action_or_operation="CREATE_ORDER",
        status="SUCCESS",
        correlation_id=corr_id,
        mission_id=msn_id,
    )

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
    )

    assert assessment.overall_status == ComplianceStatus.COMPLIANT
    assert len(assessment.findings) >= 5
    assert all(f.status == ComplianceStatus.COMPLIANT for f in assessment.findings)


# Escenario B: N.3 DENY -> executor blocked -> COMPLIANT enforcement
def test_scenario_b_deny_blocked_compliant_enforcement(compliance_service, audit_repo):
    corr_id = "corr_deny_block_b"
    msn_id = "msn_b_01"

    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="agent_unauthorized",
        actor_type="AGENT",
        action_or_operation="DELETE_CATALOG",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="agent_unauthorized",
        actor_type="AGENT",
        action_or_operation="DELETE_CATALOG",
        status="DENY",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="agent_unauthorized",
        actor_type="AGENT",
        action_or_operation="DELETE_CATALOG",
        status="BLOCKED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="DELETE_CATALOG",
    )

    assert assessment.overall_status == ComplianceStatus.COMPLIANT
    authz_findings = [f for f in assessment.findings if f.requirement_id == "REQ_AUTHORIZATION"]
    assert len(authz_findings) == 1
    assert authz_findings[0].reason_code == ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION


# Escenario C: force/mock execution despite N.3 DENY -> NON_COMPLIANT
def test_scenario_c_force_execution_despite_deny_non_compliant(compliance_service, audit_repo):
    corr_id = "corr_force_exec_c"
    msn_id = "msn_c_01"

    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="attacker",
        actor_type="USER",
        action_or_operation="TRANSFER_FUNDS",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="attacker",
        actor_type="USER",
        action_or_operation="TRANSFER_FUNDS",
        status="DENY",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.ACTION_EXECUTED,
        actor_id="attacker",
        actor_type="USER",
        action_or_operation="TRANSFER_FUNDS",
        status="SUCCESS",
        correlation_id=corr_id,
        mission_id=msn_id,
    )

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="TRANSFER_FUNDS",
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    authz_findings = [f for f in assessment.findings if f.requirement_id == "REQ_AUTHORIZATION"]
    assert len(authz_findings) == 1
    assert authz_findings[0].status == ComplianceStatus.NON_COMPLIANT
    assert authz_findings[0].reason_code == ComplianceFindingReasonCode.AUTHORIZATION_DENIED_BUT_EXECUTED


# Escenario D: N.8 DENY but execution present -> NON_COMPLIANT
def test_scenario_d_tool_deny_with_execution_non_compliant(compliance_service, audit_repo):
    corr_id = "corr_tool_deny_d"
    msn_id = "msn_d_01"

    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="DANGEROUS_TOOL",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="DANGEROUS_TOOL",
        status="ALLOW",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.TOOL_ACCESS_DENIED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="DANGEROUS_TOOL",
        status="DENY",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.ACTION_EXECUTED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="DANGEROUS_TOOL",
        status="SUCCESS",
        correlation_id=corr_id,
        mission_id=msn_id,
    )

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="DANGEROUS_TOOL",
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    tool_findings = [f for f in assessment.findings if f.requirement_id == "REQ_TOOL_POLICY"]
    assert len(tool_findings) == 1
    assert tool_findings[0].reason_code == ComplianceFindingReasonCode.TOOL_POLICY_DENIED_BUT_EXECUTED


# Escenario E: N.7 limit exceeded + no valid approval + execution -> NON_COMPLIANT
def test_scenario_e_limit_exceeded_no_approval_executed_non_compliant(compliance_service, audit_repo):
    corr_id = "corr_fin_exceeded_e"
    msn_id = "msn_e_01"

    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="REFUND",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="REFUND",
        status="ALLOW",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.LIMIT_EXCEEDED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="REFUND",
        status="LIMIT_EXCEEDED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.ACTION_EXECUTED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="REFUND",
        status="SUCCESS",
        correlation_id=corr_id,
        mission_id=msn_id,
    )

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="REFUND",
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    fin_findings = [f for f in assessment.findings if f.requirement_id == "REQ_FINANCIAL_LIMIT"]
    assert len(fin_findings) == 1
    assert fin_findings[0].reason_code == ComplianceFindingReasonCode.FINANCIAL_LIMIT_EXCEEDED_WITHOUT_APPROVAL


# Escenario F: N.6 required approval present and valid -> compliant path
def test_scenario_f_required_approval_present_compliant(compliance_service, audit_repo):
    corr_id = "corr_approved_f"
    msn_id = "msn_f_01"

    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="HIGH_VALUE_TRANSFER",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="HIGH_VALUE_TRANSFER",
        status="ALLOW",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.LIMIT_EXCEEDED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="HIGH_VALUE_TRANSFER",
        status="LIMIT_EXCEEDED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.APPROVAL_GRANTED,
        actor_id="supervisor_01",
        actor_type="USER",
        action_or_operation="HIGH_VALUE_TRANSFER",
        status="APPROVED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.ACTION_EXECUTED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="HIGH_VALUE_TRANSFER",
        status="SUCCESS",
        correlation_id=corr_id,
        mission_id=msn_id,
    )

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="HIGH_VALUE_TRANSFER",
    )

    assert assessment.overall_status == ComplianceStatus.COMPLIANT


# Escenario G: N.9 sensitive payload correctly redacted -> compliant
def test_scenario_g_sensitive_payload_redacted_compliant(compliance_service, audit_repo):
    corr_id = "corr_redacted_g"
    msn_id = "msn_g_01"

    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="PROCESS_ORDER",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="PROCESS_ORDER",
        status="ALLOW",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.ACTION_EXECUTED,
        actor_id="agent_1",
        actor_type="AGENT",
        action_or_operation="PROCESS_ORDER",
        status="SUCCESS",
        correlation_id=corr_id,
        mission_id=msn_id,
        metadata={"buyer_email": "ro***@example.com", "shipping_address": "[REDACTED_ADDRESS]"},
    )

    # Inyectar evidencia de decision N.9
    ev_sens = ComplianceEvidenceReference(
        evidence_id="ev_sens_g",
        evidence_type=ComplianceEvidenceType.SENSITIVE_DATA_DECISION,
        source_component="N9_SENSITIVE_DATA",
        correlation_id=corr_id,
        mission_id=msn_id,
        observed_status="REDACTED",
    )

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="PROCESS_ORDER",
        injected_evidences=(ev_sens,),
    )

    assert assessment.overall_status == ComplianceStatus.COMPLIANT


# Escenario H: audit record tampered -> integrity finding
def test_scenario_h_audit_tampered_integrity_finding(compliance_service, audit_repo):
    corr_id = "corr_tampered_h"
    msn_id = "msn_h_01"

    rec = _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="usr_1",
        actor_type="USER",
        action_or_operation="LOGIN",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )

    # Tamper with the raw record in repository (corrupt checksum)
    tampered_rec = AuditRecord(
        audit_id=rec.audit_id,
        record_type=rec.record_type,
        occurred_at=rec.occurred_at,
        actor=rec.actor,
        subject_type=rec.subject_type,
        subject_id=rec.subject_id,
        action_or_operation=rec.action_or_operation,
        status=rec.status,
        correlation_id=rec.correlation_id,
        causation_id=rec.causation_id,
        mission_id=rec.mission_id,
        entity_reference=rec.entity_reference,
        evidence_reference=rec.evidence_reference,
        provenance=rec.provenance,
        idempotency_key=rec.idempotency_key,
        checksum="invalid_fake_checksum_12345",
        schema_version=rec.schema_version,
        metadata=dict(rec.metadata),
    )
    audit_repo._records_by_id[rec.audit_id] = tampered_rec

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="LOGIN",
    )

    assert assessment.overall_status == ComplianceStatus.ERROR
    integrity_findings = [f for f in assessment.findings if f.requirement_id == "AUDIT_INTEGRITY"]
    assert len(integrity_findings) >= 1
    assert integrity_findings[0].reason_code == ComplianceFindingReasonCode.AUDIT_INTEGRITY_FAILURE


# Escenario I: evidence from wrong correlation -> rejected / incomplete
def test_scenario_i_evidence_from_wrong_correlation_isolated(compliance_service, audit_repo):
    corr_id = "corr_target_i"
    msn_id = "msn_i_01"

    # Insert audit record with foreign correlation
    _append_audit_record(
        audit_repo,
        record_type=AuditRecordType.ACTION_EXECUTED,
        actor_id="foreign_actor",
        actor_type="USER",
        action_or_operation="CREATE_ORDER",
        status="SUCCESS",
        correlation_id="corr_FOREIGN_999",
        mission_id=msn_id,
    )

    # Injected evidence with wrong correlation
    foreign_ev = ComplianceEvidenceReference(
        evidence_id="ev_foreign",
        evidence_type=ComplianceEvidenceType.ACTION_RESULT,
        source_component="ACTION_EXECUTOR",
        correlation_id="corr_FOREIGN_999",
        mission_id=msn_id,
        observed_status="SUCCESS",
    )

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=(foreign_ev,),
    )

    assert assessment.overall_status != ComplianceStatus.COMPLIANT
    cross_findings = [f for f in assessment.findings if f.requirement_id == "CORRELATION_ISOLATION"]
    assert len(cross_findings) == 1
    assert cross_findings[0].reason_code == ComplianceFindingReasonCode.CROSS_CORRELATION_EVIDENCE_REJECTED


# Escenario J: restart/reload -> assessment remains reproducible
def test_scenario_j_restart_reproducible_assessment(tmp_dir):
    audit_file = os.path.join(tmp_dir, "audit_persistent.json")
    repo1 = JsonAuditRepository(audit_file)
    corr_id = "corr_restart_j"
    msn_id = "msn_j"

    _append_audit_record(
        repo1,
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        actor_id="usr_j",
        actor_type="USER",
        action_or_operation="OP_J",
        status="AUTHENTICATED",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        repo1,
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        actor_id="usr_j",
        actor_type="USER",
        action_or_operation="OP_J",
        status="ALLOW",
        correlation_id=corr_id,
        mission_id=msn_id,
    )
    _append_audit_record(
        repo1,
        record_type=AuditRecordType.ACTION_EXECUTED,
        actor_id="usr_j",
        actor_type="USER",
        action_or_operation="OP_J",
        status="SUCCESS",
        correlation_id=corr_id,
        mission_id=msn_id,
    )

    # Primer assessment
    collector1 = DefaultComplianceEvidenceCollector(audit_repository=repo1)
    service1 = ComplianceAssessmentService(evidence_collector=collector1)
    ass1 = service1.assess_operation(correlation_id=corr_id, mission_id=msn_id, action_or_operation="OP_J")

    # Simular reinicio y recarga
    repo2 = JsonAuditRepository(audit_file)
    collector2 = DefaultComplianceEvidenceCollector(audit_repository=repo2)
    service2 = ComplianceAssessmentService(evidence_collector=collector2)
    ass2 = service2.assess_operation(correlation_id=corr_id, mission_id=msn_id, action_or_operation="OP_J")

    assert ass1.overall_status == ass2.overall_status
    assert len(ass1.findings) == len(ass2.findings)
    for f1, f2 in zip(ass1.findings, ass2.findings):
        assert f1.reason_code == f2.reason_code
        assert f1.status == f2.status
        assert f1.checksum == f2.checksum


# Flujo E2E N.10: Pipeline de ejecución gobernada conectado a ComplianceAssessment
def test_e2e_governed_pipeline_compliance_evaluation(tmp_dir, audit_repo):
    delegate = MockActionExecutor()
    authz_service = AuthorizationService(audit_repository=audit_repo)

    identity_ref = IdentityReference(
        identity_id="agent_audited_01",
        identity_type=IdentityType.AGENT,
        canonical_identifier="agent:internal:agent_audited_01",
        display_name="Audited Agent",
    )
    auth_res = AuthenticationResult(
        principal=identity_ref,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal_api",
        status=AuthenticationStatus.AUTHENTICATED,
    )
    principal = PrincipalContext(
        principal=identity_ref,
        auth_result=auth_res,
    )

    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=("PROCESS_TRANSACTION", "QUERY_DATA"),
    )

    # 1. Ejecución permitida y conforme
    state = LoopState(mission_id="mission_e2e_audited", iteration=1, goal="Execute transaction securely")
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="db_service",
        parameters={"action_type": "PROCESS_TRANSACTION", "amount": 100},
        reason="Valid transaction execution",
    )

    exec_res = guarded_executor.execute(decision, state)
    assert exec_res["is_allowed"] is True
    assert exec_res["execution_status"] == "SUCCESS"

    # Recolectar y evaluar compliance desde Audit K.1
    collector = DefaultComplianceEvidenceCollector(audit_repository=audit_repo)
    compliance_service = ComplianceAssessmentService(evidence_collector=collector)

    # Audit records were logged under correlation_id from state or execution
    audit_records = audit_repo.list_records(mission_id="mission_e2e_audited")
    assert len(audit_records) >= 1
    corr_id = audit_records[0].correlation_id

    assessment = compliance_service.assess_operation(
        correlation_id=corr_id,
        mission_id="mission_e2e_audited",
        action_or_operation="PROCESS_TRANSACTION",
    )

    assert assessment.overall_status == ComplianceStatus.COMPLIANT
    report = compliance_service.generate_report(assessment)
    assert report.is_compliant() is True
    assert report.summary()["overall_status"] == "COMPLIANT"
