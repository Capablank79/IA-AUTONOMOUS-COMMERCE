"""
Unit tests for N.10 — Audit / Compliance (Transversal N — Security, Governance & Safety).

Validates:
1. Complete compliant chain
2. Missing authentication evidence -> not compliant / incomplete
3. Missing authorization evidence -> not compliant
4. Executed after DENY -> non-compliant / critical finding
5. Tool deny but executed -> non-compliant
6. Over financial limit improperly executed -> non-compliant
7. Approval required but missing -> non-compliant
8. Correct blocked action -> compliant (fail-secure / compliant blocked action)
9. Corrupt audit evidence -> audit integrity failure finding / ERROR
10. Wrong correlation evidence rejected -> cross correlation finding / isolated
11. UNKNOWN != compliant -> fail-secure state evaluation
12. Deterministic assessment reproducibility & checksums
13. Policy version preserved & version mismatch detection
14. Sensitive data and secrets not exposed in findings/assessments (N.9/N.5 integration)
15. Structured findings format, severity mapping, and precedence
16. Strict boundary verification: no N.11 emergency stop / kill switch implementation
"""

from datetime import datetime, timezone
import hashlib
import json
import pytest
from typing import Dict, Any, Tuple

from src.domain.compliance.models import (
    ComplianceStatus,
    ComplianceFindingSeverity,
    ComplianceFindingReasonCode,
    ComplianceRequirementType,
    ComplianceEvidenceType,
    ComplianceRequirement,
    ComplianceEvidenceReference,
    ComplianceFinding,
    ComplianceCheck,
    CompliancePolicy,
    ComplianceAssessment,
    ComplianceReport,
    compute_evidence_checksum,
    compute_finding_checksum,
    compute_assessment_checksum,
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


def _build_sample_evidence_reference(
    evidence_id: str,
    evidence_type: ComplianceEvidenceType,
    source_component: str,
    correlation_id: str,
    mission_id: str = "msn_test_100",
    observed_status: str = "AUTHENTICATED",
    integrity_verified: bool = True,
    metadata: Dict[str, Any] = None,
) -> ComplianceEvidenceReference:
    meta = metadata or {}
    chk = compute_evidence_checksum(
        evidence_id=evidence_id,
        evidence_type=evidence_type.value,
        source_component=source_component,
        correlation_id=correlation_id,
        original_checksum="sha256_mock_valid",
    )
    return ComplianceEvidenceReference(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        source_component=source_component,
        correlation_id=correlation_id,
        mission_id=mission_id,
        original_checksum="sha256_mock_valid",
        integrity_verified=integrity_verified,
        observed_status=observed_status,
        summary=f"{evidence_type.value} observed as {observed_status}",
        metadata=meta,
        checksum=chk,
    )


def test_01_complete_compliant_chain():
    """1. Complete compliant chain: All controls satisfied and action executed properly -> COMPLIANT."""
    corr_id = "corr_compl_01"
    msn_id = "msn_compl_01"

    evidences = (
        _build_sample_evidence_reference("ev_auth_01", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED", metadata={"actor_id": "usr_alice", "actor_type": "USER"}),
        _build_sample_evidence_reference("ev_rbac_01", ComplianceEvidenceType.RBAC_EVALUATION, "N4_RBAC", corr_id, msn_id, "VALID", metadata={"actions": ["CREATE_ORDER"]}),
        _build_sample_evidence_reference("ev_authz_01", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_tool_01", ComplianceEvidenceType.TOOL_ACCESS_DECISION, "N8_TOOL", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_fin_01", ComplianceEvidenceType.FINANCIAL_LIMIT_DECISION, "N7_FIN", corr_id, msn_id, "WITHIN_LIMIT"),
        _build_sample_evidence_reference("ev_appr_01", ComplianceEvidenceType.APPROVAL_DECISION, "N6_APPR", corr_id, msn_id, "NOT_REQUIRED"),
        _build_sample_evidence_reference("ev_sens_01", ComplianceEvidenceType.SENSITIVE_DATA_DECISION, "N9_SENS", corr_id, msn_id, "SANITIZED"),
        _build_sample_evidence_reference("ev_exec_01", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "CREATE_ORDER"}),
        _build_sample_evidence_reference("ev_audit_01", ComplianceEvidenceType.AUDIT_RECORD, "K1_AUDIT_TRAIL", corr_id, msn_id, "LOGGED", metadata={"action_or_operation": "CREATE_ORDER", "record_type": "ACTION_EXECUTED"}),
        _build_sample_evidence_reference("ev_trace_01", ComplianceEvidenceType.TRACE_RECORD, "K2_AGENT_TRACE", corr_id, msn_id, "COMPLETED", metadata={"operation": "CREATE_ORDER"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )

    assert assessment.overall_status == ComplianceStatus.COMPLIANT
    assert len(assessment.findings) > 0
    assert all(f.status == ComplianceStatus.COMPLIANT for f in assessment.findings)
    assert assessment.correlation_id == corr_id
    assert assessment.mission_id == msn_id
    assert assessment.checksum is not None


def test_02_missing_auth_evidence():
    """2. Missing authentication evidence -> NON_COMPLIANT / INCOMPLETE (fail-secure)."""
    corr_id = "corr_miss_auth_02"
    msn_id = "msn_02"

    # Evidences without authentication
    evidences = (
        _build_sample_evidence_reference("ev_authz_02", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_tool_02", ComplianceEvidenceType.TOOL_ACCESS_DECISION, "N8_TOOL", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_exec_02", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "CREATE_ORDER"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    auth_findings = [f for f in assessment.findings if f.requirement_id == "REQ_AUTHENTICATION"]
    assert len(auth_findings) == 1
    assert auth_findings[0].status == ComplianceStatus.NON_COMPLIANT
    assert auth_findings[0].reason_code == ComplianceFindingReasonCode.MISSING_AUTHENTICATION_EVIDENCE


def test_03_missing_authorization_evidence():
    """3. Missing authorization evidence -> NON_COMPLIANT."""
    corr_id = "corr_miss_authz_03"
    msn_id = "msn_03"

    evidences = (
        _build_sample_evidence_reference("ev_auth_03", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_tool_03", ComplianceEvidenceType.TOOL_ACCESS_DECISION, "N8_TOOL", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_exec_03", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "CREATE_ORDER"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    authz_findings = [f for f in assessment.findings if f.requirement_id == "REQ_AUTHORIZATION"]
    assert len(authz_findings) == 1
    assert authz_findings[0].reason_code == ComplianceFindingReasonCode.MISSING_AUTHORIZATION_EVIDENCE


def test_04_executed_after_deny():
    """4. Executed after DENY -> NON_COMPLIANT with CRITICAL finding."""
    corr_id = "corr_deny_exec_04"
    msn_id = "msn_04"

    evidences = (
        _build_sample_evidence_reference("ev_auth_04", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_authz_04", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "DENY"),
        _build_sample_evidence_reference("ev_exec_04", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "TRANSFER_FUNDS"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="TRANSFER_FUNDS",
        injected_evidences=evidences,
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    authz_findings = [f for f in assessment.findings if f.requirement_id == "REQ_AUTHORIZATION"]
    assert len(authz_findings) == 1
    assert authz_findings[0].status == ComplianceStatus.NON_COMPLIANT
    assert authz_findings[0].severity == ComplianceFindingSeverity.CRITICAL
    assert authz_findings[0].reason_code == ComplianceFindingReasonCode.AUTHORIZATION_DENIED_BUT_EXECUTED


def test_05_tool_deny_but_executed():
    """5. Tool deny but executed -> NON_COMPLIANT with TOOL_POLICY_DENIED_BUT_EXECUTED."""
    corr_id = "corr_tool_deny_05"
    msn_id = "msn_05"

    evidences = (
        _build_sample_evidence_reference("ev_auth_05", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_authz_05", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_tool_05", ComplianceEvidenceType.TOOL_ACCESS_DECISION, "N8_TOOL", corr_id, msn_id, "DENY"),
        _build_sample_evidence_reference("ev_exec_05", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "EXECUTE_TOOL"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="EXECUTE_TOOL",
        injected_evidences=evidences,
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    tool_findings = [f for f in assessment.findings if f.requirement_id == "REQ_TOOL_POLICY"]
    assert len(tool_findings) == 1
    assert tool_findings[0].reason_code == ComplianceFindingReasonCode.TOOL_POLICY_DENIED_BUT_EXECUTED
    assert tool_findings[0].severity == ComplianceFindingSeverity.CRITICAL


def test_06_over_financial_limit_improperly_executed():
    """6. Over financial limit without approval and executed -> NON_COMPLIANT."""
    corr_id = "corr_fin_exceeded_06"
    msn_id = "msn_06"

    evidences = (
        _build_sample_evidence_reference("ev_auth_06", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_authz_06", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_tool_06", ComplianceEvidenceType.TOOL_ACCESS_DECISION, "N8_TOOL", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_fin_06", ComplianceEvidenceType.FINANCIAL_LIMIT_DECISION, "N7_FIN", corr_id, msn_id, "LIMIT_EXCEEDED"),
        _build_sample_evidence_reference("ev_exec_06", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "PAY_INVOICE"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="PAY_INVOICE",
        injected_evidences=evidences,
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    fin_findings = [f for f in assessment.findings if f.requirement_id == "REQ_FINANCIAL_LIMIT"]
    assert len(fin_findings) == 1
    assert fin_findings[0].reason_code == ComplianceFindingReasonCode.FINANCIAL_LIMIT_EXCEEDED_WITHOUT_APPROVAL


def test_07_approval_required_but_missing():
    """7. Approval required but missing / rejected and executed -> NON_COMPLIANT."""
    corr_id = "corr_appr_miss_07"
    msn_id = "msn_07"

    evidences = (
        _build_sample_evidence_reference("ev_auth_07", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_authz_07", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_tool_07", ComplianceEvidenceType.TOOL_ACCESS_DECISION, "N8_TOOL", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_appr_07", ComplianceEvidenceType.APPROVAL_DECISION, "N6_APPR", corr_id, msn_id, "REJECTED"),
        _build_sample_evidence_reference("ev_exec_07", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "CRITICAL_PAYMENT"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CRITICAL_PAYMENT",
        injected_evidences=evidences,
    )

    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    appr_findings = [f for f in assessment.findings if f.requirement_id == "REQ_APPROVAL"]
    assert len(appr_findings) == 1
    assert appr_findings[0].reason_code == ComplianceFindingReasonCode.APPROVAL_REJECTED_BUT_EXECUTED


def test_08_correct_blocked_action_is_compliant():
    """8. Correct blocked action -> COMPLIANT (Fail-secure enforcement successful)."""
    corr_id = "corr_blocked_ok_08"
    msn_id = "msn_08"

    # Action blocked by N.3 DENY; never reached execution
    evidences = (
        _build_sample_evidence_reference("ev_auth_08", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_authz_08", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "DENY"),
        _build_sample_evidence_reference("ev_audit_08", ComplianceEvidenceType.AUDIT_RECORD, "K1_AUDIT_TRAIL", corr_id, msn_id, "BLOCKED", metadata={"record_type": "AUTHORIZATION_DENIED"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="DELETE_DATABASE",
        injected_evidences=evidences,
    )

    # Compliance means governance policy was properly enforced, so blocking unauthorized action is COMPLIANT
    assert assessment.overall_status == ComplianceStatus.COMPLIANT
    authz_findings = [f for f in assessment.findings if f.requirement_id == "REQ_AUTHORIZATION"]
    assert len(authz_findings) == 1
    assert authz_findings[0].reason_code == ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION
    assert authz_findings[0].status == ComplianceStatus.COMPLIANT


def test_09_corrupt_audit_evidence():
    """9. Corrupt audit evidence -> Cryptographic integrity failure finding & overall ERROR."""
    corr_id = "corr_corrupt_09"
    msn_id = "msn_09"

    # Invalidate integrity_verified flag to simulate checksum mismatch / tampering
    evidences = (
        _build_sample_evidence_reference("ev_auth_09", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_authz_09", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_audit_corrupt_09", ComplianceEvidenceType.AUDIT_RECORD, "K1_AUDIT_TRAIL", corr_id, msn_id, "LOGGED", integrity_verified=False),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )

    assert assessment.overall_status == ComplianceStatus.ERROR
    integrity_findings = [f for f in assessment.findings if f.requirement_id == "AUDIT_INTEGRITY"]
    assert len(integrity_findings) == 1
    assert integrity_findings[0].reason_code == ComplianceFindingReasonCode.AUDIT_INTEGRITY_FAILURE
    assert integrity_findings[0].severity == ComplianceFindingSeverity.CRITICAL


def test_10_wrong_correlation_evidence_rejected():
    """10. Wrong correlation evidence rejected -> Mismatched records isolated, finding generated."""
    corr_id = "corr_target_10"
    msn_id = "msn_10"

    evidences = (
        _build_sample_evidence_reference("ev_auth_10", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_authz_10", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        # Foreign correlation
        _build_sample_evidence_reference("ev_foreign_10", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", "corr_FOREIGN_999", msn_id, "SUCCESS"),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )

    # Cross-correlation was rejected, and because required execution/controls from target correlation were missing, status is not COMPLIANT
    assert assessment.overall_status in (ComplianceStatus.NON_COMPLIANT, ComplianceStatus.INCOMPLETE)
    cross_findings = [f for f in assessment.findings if f.requirement_id == "CORRELATION_ISOLATION"]
    assert len(cross_findings) == 1
    assert cross_findings[0].reason_code == ComplianceFindingReasonCode.CROSS_CORRELATION_EVIDENCE_REJECTED


def test_11_unknown_status_is_not_compliant():
    """11. UNKNOWN evidence status -> Evaluates to UNKNOWN / NON_COMPLIANT (Fail-secure)."""
    corr_id = "corr_unknown_11"
    msn_id = "msn_11"

    evidences = (
        _build_sample_evidence_reference("ev_auth_11", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "UNKNOWN"),
        _build_sample_evidence_reference("ev_authz_11", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )

    assert assessment.overall_status in (ComplianceStatus.UNKNOWN, ComplianceStatus.NON_COMPLIANT)
    assert assessment.overall_status != ComplianceStatus.COMPLIANT


def test_12_deterministic_assessment_and_checksum():
    """12. Deterministic assessment reproducibility & SHA-256 integrity."""
    corr_id = "corr_det_12"
    msn_id = "msn_12"

    evidences = (
        _build_sample_evidence_reference("ev_auth_12", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, msn_id, "AUTHENTICATED"),
        _build_sample_evidence_reference("ev_authz_12", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_tool_12", ComplianceEvidenceType.TOOL_ACCESS_DECISION, "N8_TOOL", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_fin_12", ComplianceEvidenceType.FINANCIAL_LIMIT_DECISION, "N7_FIN", corr_id, msn_id, "WITHIN_LIMIT"),
        _build_sample_evidence_reference("ev_appr_12", ComplianceEvidenceType.APPROVAL_DECISION, "N6_APPR", corr_id, msn_id, "NOT_REQUIRED"),
        _build_sample_evidence_reference("ev_sens_12", ComplianceEvidenceType.SENSITIVE_DATA_DECISION, "N9_SENS", corr_id, msn_id, "SANITIZED"),
        _build_sample_evidence_reference("ev_exec_12", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "CREATE_ORDER"}),
        _build_sample_evidence_reference("ev_audit_12", ComplianceEvidenceType.AUDIT_RECORD, "K1_AUDIT_TRAIL", corr_id, msn_id, "LOGGED", metadata={"action_or_operation": "CREATE_ORDER", "record_type": "ACTION_EXECUTED"}),
        _build_sample_evidence_reference("ev_trace_12", ComplianceEvidenceType.TRACE_RECORD, "K2_AGENT_TRACE", corr_id, msn_id, "COMPLETED", metadata={"operation": "CREATE_ORDER"}),
    )

    service = ComplianceAssessmentService()
    assessment1 = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )
    assessment2 = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )

    assert assessment1.overall_status == assessment2.overall_status
    assert len(assessment1.findings) == len(assessment2.findings)
    assert assessment1.policy_name == assessment2.policy_name
    assert assessment1.policy_version == assessment2.policy_version
    # Finding checksums and reason codes must match exactly
    for f1, f2 in zip(assessment1.findings, assessment2.findings):
        assert f1.reason_code == f2.reason_code
        assert f1.status == f2.status
        assert f1.checksum == f2.checksum


def test_13_policy_version_preserved_and_resolved():
    """13. Policy version preserved & version mismatch detection."""
    repo = InMemoryCompliancePolicyRepository()
    v1_policy = create_default_commercial_compliance_policy("v1_policy", "1.0.0")
    v2_policy = create_default_commercial_compliance_policy("v2_policy", "2.0.0")
    repo.save(v1_policy)
    repo.save(v2_policy)

    service = ComplianceAssessmentService(policy_repository=repo, default_policy_name="v1_policy")

    corr_id = "corr_ver_13"
    evidences = (
        _build_sample_evidence_reference("ev_auth_13", ComplianceEvidenceType.AUTHENTICATION_RESULT, "N2_AUTH", corr_id, "msn_13", "AUTHENTICATED"),
    )

    # Assess against specific version
    ass_v2 = service.assess_operation(
        correlation_id=corr_id,
        policy_name="v2_policy",
        policy_version="2.0.0",
        injected_evidences=evidences,
    )
    assert ass_v2.policy_name == "v2_policy"
    assert ass_v2.policy_version == "2.0.0"

    # Assess against non-existent policy returns ERROR
    ass_err = service.assess_operation(
        correlation_id=corr_id,
        policy_name="non_existent_policy",
        policy_version="9.9.9",
        injected_evidences=evidences,
    )
    assert ass_err.overall_status == ComplianceStatus.ERROR
    assert ass_err.findings[0].reason_code == ComplianceFindingReasonCode.EVALUATION_ERROR


def test_14_sensitive_data_not_exposed_in_reports():
    """14. Sensitive data, PII, and secrets not exposed in reports/findings."""
    corr_id = "corr_priv_14"
    msn_id = "msn_14"

    # Evidence contains raw PII/secrets in payload metadata that should be sanitized
    evidences = (
        _build_sample_evidence_reference(
            "ev_auth_14",
            ComplianceEvidenceType.AUTHENTICATION_RESULT,
            "N2_AUTH",
            corr_id,
            msn_id,
            "AUTHENTICATED",
            metadata={"secret_key": "sk_live_verysecret123", "password": "PlainTextPassword", "actor_id": "usr_bob"},
        ),
        _build_sample_evidence_reference("ev_authz_14", ComplianceEvidenceType.AUTHORIZATION_DECISION, "N3_AUTHZ", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_tool_14", ComplianceEvidenceType.TOOL_ACCESS_DECISION, "N8_TOOL", corr_id, msn_id, "ALLOW"),
        _build_sample_evidence_reference("ev_fin_14", ComplianceEvidenceType.FINANCIAL_LIMIT_DECISION, "N7_FIN", corr_id, msn_id, "WITHIN_LIMIT"),
        _build_sample_evidence_reference("ev_appr_14", ComplianceEvidenceType.APPROVAL_DECISION, "N6_APPR", corr_id, msn_id, "NOT_REQUIRED"),
        _build_sample_evidence_reference("ev_sens_14", ComplianceEvidenceType.SENSITIVE_DATA_DECISION, "N9_SENS", corr_id, msn_id, "SANITIZED"),
        _build_sample_evidence_reference("ev_exec_14", ComplianceEvidenceType.ACTION_RESULT, "ACTION_EXECUTOR", corr_id, msn_id, "SUCCESS", metadata={"action_or_operation": "CREATE_ORDER"}),
        _build_sample_evidence_reference("ev_audit_14", ComplianceEvidenceType.AUDIT_RECORD, "K1_AUDIT_TRAIL", corr_id, msn_id, "LOGGED", metadata={"action_or_operation": "CREATE_ORDER", "record_type": "ACTION_EXECUTED"}),
        _build_sample_evidence_reference("ev_trace_14", ComplianceEvidenceType.TRACE_RECORD, "K2_AGENT_TRACE", corr_id, msn_id, "COMPLETED", metadata={"operation": "CREATE_ORDER"}),
    )

    service = ComplianceAssessmentService()
    assessment = service.assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="CREATE_ORDER",
        injected_evidences=evidences,
    )

    report = service.generate_report(assessment)

    # String representations and dicts of assessment & report must not leak raw secrets
    report_str = json.dumps(report.to_dict(), default=str)
    assert "sk_live_verysecret123" not in report_str
    assert "PlainTextPassword" not in report_str
    assert "[REDACTED" in report_str


def test_15_structured_findings_and_severity_precedence():
    """15. Structured findings format, severity mapping, and status precedence (ERROR > NON_COMPLIANT > INCOMPLETE > UNKNOWN > COMPLIANT)."""
    # Create findings with multiple statuses
    f1 = ComplianceFinding(
        finding_id="f1",
        requirement_id="REQ_1",
        status=ComplianceStatus.COMPLIANT,
        severity=ComplianceFindingSeverity.INFO,
        reason_code=ComplianceFindingReasonCode.COMPLIANT_EXECUTION,
        description="Requirement 1 compliant",
        correlation_id="c1",
    )
    f2 = ComplianceFinding(
        finding_id="f2",
        requirement_id="REQ_2",
        status=ComplianceStatus.INCOMPLETE,
        severity=ComplianceFindingSeverity.LOW,
        reason_code=ComplianceFindingReasonCode.MISSING_AUDIT_TRAIL,
        description="Missing audit trail",
        correlation_id="c1",
    )
    f3 = ComplianceFinding(
        finding_id="f3",
        requirement_id="REQ_3",
        status=ComplianceStatus.NON_COMPLIANT,
        severity=ComplianceFindingSeverity.CRITICAL,
        reason_code=ComplianceFindingReasonCode.UNAUTHORIZED_EXECUTION,
        description="Unauthorized execution",
        correlation_id="c1",
    )

    service = ComplianceAssessmentService()
    assert service._derive_overall_status([f1]) == ComplianceStatus.COMPLIANT
    assert service._derive_overall_status([f1, f2]) == ComplianceStatus.INCOMPLETE
    assert service._derive_overall_status([f1, f2, f3]) == ComplianceStatus.NON_COMPLIANT

    f4 = ComplianceFinding(
        finding_id="f4",
        requirement_id="REQ_4",
        status=ComplianceStatus.ERROR,
        severity=ComplianceFindingSeverity.CRITICAL,
        reason_code=ComplianceFindingReasonCode.AUDIT_INTEGRITY_FAILURE,
        description="Integrity failed",
        correlation_id="c1",
    )
    assert service._derive_overall_status([f1, f2, f3, f4]) == ComplianceStatus.ERROR


def test_16_strict_boundary_no_n11_implementation():
    """16. Strict boundary verification: N.10 does NOT implement emergency stop, kill switch, or shutdown."""
    import src.domain.compliance.models as comp_models
    import src.application.compliance.compliance_assessment_service as comp_service

    forbidden_keywords = ["emergency_stop", "kill_switch", "global_pause", "shutdown_system", "circuit_breaker"]

    for kw in forbidden_keywords:
        assert not hasattr(comp_models, kw), f"N.10 domain model must not implement '{kw}'"
        assert not hasattr(comp_service, kw), f"N.10 application service must not implement '{kw}'"
        assert not hasattr(ComplianceAssessmentService, kw), f"ComplianceAssessmentService must not implement '{kw}'"
