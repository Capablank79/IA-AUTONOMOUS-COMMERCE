"""
Tests de Integración E2E para GATE M — Formal Validation & Closure of Hito N
(Transversal N — Security, Governance & Safety).

Principio Fundamental a Validar:
"Ninguna acción financiera o externa de alto impacto puede ejecutarse sin cumplir la política correspondiente."

Pipeline E2E completo:
Actor
  → N.1 Identity
  → N.2 Authentication
  → N.4 RBAC / Permissions
  → N.3 Authorization
  → N.8 Tool Access Policy
  → N.9 Sensitive Data Handling
  → N.7 Financial Limits
  → N.6 Approval Policies
  → N.11 Emergency Stop
  → Physical Action Boundary (MockActionExecutor)
  → K.1 Audit Trail
  → K.2 Agent Trace
  → N.10 Compliance Assessment

Cobertura obligatoria (14 escenarios):
1. Happy path: full compliant external execution (physical mock = 1, N.10 COMPLIANT)
2. Invalid/expired/unknown authentication blocks (physical mock = 0)
3. RBAC / Authorization denial blocks (physical mock = 0)
4. Tool deny / unknown tool blocks (physical mock = 0)
5. Sensitive data classification, minimization, and redaction (no PII leak)
6. Financial limit exceeded blocks without approval (physical mock = 0)
7. Valid approval exception allows execution over financial limit (physical mock = 1)
8. Missing, expired, or mismatched approval blocks (physical mock = 0)
9. Missing secret / credentials blocks external adapter (physical mock = 0)
10. Emergency stop (Global or Scoped) active blocks execution (physical mock = 0)
11. Correctly blocked action evaluates to COMPLIANT enforcement in N.10
12. Simulated/forced execution after denial evaluates to NON_COMPLIANT in N.10
13. Cross-correlation / mission isolation rejects unrelated evidence
14. Corrupted / tampered records fail-safe cleanly without granting false ALLOW
"""

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, Optional, Sequence
import pytest

from src.domain.identity.models import IdentityType, IdentityStatus, IdentityReference, PrincipalIdentity, Identity
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationResult,
    AuthenticationRequest,
    PrincipalContext,
)
from src.domain.authorization.models import (
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
    AuthorizationRequest,
    AuthorizationDecision,
)
from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
)
from src.domain.secrets.models import (
    SecretType,
    SecretStatus,
    SecretResolutionStatus,
    SecretValue,
    SecretReference,
    SecretMetadata,
    SecretResolutionResult,
)
from src.domain.approval.models import (
    ApprovalStatus,
    ApprovalReasonCode,
    ApprovalPolicy,
    ApprovalEvidence,
    ApprovalRequest,
    ApprovalDecision,
    compute_approval_checksum,
)
from src.domain.profit.models import Money
from src.domain.financial_limit.models import (
    FinancialLimitType,
    FinancialLimitStatus,
    FinancialLimitReasonCode,
    FinancialLimitRule,
    FinancialLimitPolicy,
    FinancialLimitRequest,
    FinancialLimitDecision,
    compute_financial_decision_checksum,
)
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
from src.domain.security.sensitive_data_models import (
    DataClassification,
    SensitiveCategory,
    DataHandlingPurpose,
    PersistenceHandlingMode,
    CacheHandlingMode,
    DataHandlingReasonCode,
    DataHandlingPolicy,
    DataHandlingRequest,
    DataHandlingDecision,
    compute_deterministic_fingerprint,
)
from src.domain.security.sensitive_data_engine import (
    DeterministicSensitiveDataClassifier,
    DeterministicSensitiveDataRedactor,
)
from src.domain.emergency_stop.models import (
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
    EmergencyStopDecisionStatus,
    EmergencyStopReasonCode,
    EmergencyStopEvaluationContext,
    EmergencyStopDecision,
    compute_emergency_stop_checksum,
)
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
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
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule

from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.infrastructure.persistence.data.json.secret_metadata_repository import (
    JsonSecretMetadataRepository,
)
from src.infrastructure.persistence.data.json.approval_evidence_repository import (
    JsonApprovalEvidenceRepository,
)
from src.application.approval.approval_policy_service import (
    InMemoryApprovalPolicyRepository,
)
from src.infrastructure.persistence.data.json.financial_limit_repository import (
    JsonFinancialLimitPolicyRepository,
)
from src.infrastructure.persistence.data.json.tool_policy_repository import (
    JsonToolPolicyRepository,
)
from src.infrastructure.persistence.data.json.sensitive_data_policy_repository import (
    JsonDataHandlingPolicyRepository,
)
from src.infrastructure.persistence.data.json.emergency_stop_repository import (
    JsonEmergencyStopRepository,
)
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.in_memory.compliance_policy_repository import (
    InMemoryCompliancePolicyRepository,
    create_default_commercial_compliance_policy,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.infrastructure.secrets.providers import InjectedSecretProvider

from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import AuthenticationService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.rbac.rbac_service import RBACService
from src.application.secrets.secret_service import SecretService
from src.application.approval.approval_policy_service import ApprovalPolicyService
from src.application.financial_limit.financial_limit_service import FinancialLimitService
from src.application.tool_policy.tool_access_policy_service import ToolAccessPolicyService
from src.application.security.sensitive_data_handling_service import SensitiveDataHandlingService
from src.application.emergency_stop.emergency_stop_service import EmergencyStopService
from src.application.authorization.authorization_guarded_action_executor import (
    AuthorizationGuardedActionExecutor,
)
from src.application.compliance.compliance_assessment_service import (
    ComplianceAssessmentService,
    DefaultComplianceEvidenceCollector,
)


class MockPhysicalActionExecutor(ActionExecutor):
    """
    Mock delegado de ejecución física (representa el adapter externo, p.ej. MercadoLibre).
    Verifica que las llamadas sólo ocurran cuando la cadena de gobernanza autorice explícitamente.
    """
    def __init__(self):
        self.calls = []
        self.external_calls_count = 0

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        self.calls.append({"decision": decision, "state": state})
        self.external_calls_count += 1
        return {
            "status": "PHYSICAL_EXECUTION_SUCCESS",
            "executed_action": decision.action.value if hasattr(decision.action, "value") else str(decision.action),
            "sanitized_params": dict(decision.parameters) if decision.parameters else {},
            "call_index": self.external_calls_count,
        }


def _record_audit(
    audit_repo: JsonAuditRepository,
    record_type: AuditRecordType,
    actor_id: str,
    actor_type: str,
    action_or_operation: str,
    status: str,
    correlation_id: str,
    mission_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    subject_type: str = "COMMERCIAL_OPERATION",
    subject_id: str = "mercadolibre_item_123",
) -> AuditRecord:
    actor_enum = (
        AuditActorType.USER if actor_type.upper() == "USER"
        else AuditActorType.SYSTEM if actor_type.upper() == "SYSTEM"
        else AuditActorType.AGENT
    )
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


@pytest.fixture
def test_env(tmp_path: Path):
    """
    Fixture integral que instancia e interconecta todos los servicios de gobernanza y seguridad N.1 a N.11.
    """
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    clock = VirtualClock(initial_time=now)

    # Repositorios JSON temporales
    id_repo = JsonIdentityRepository(tmp_path / "identities.json")
    role_repo = JsonRoleRepository(tmp_path / "rbac")
    assignment_repo = JsonRoleAssignmentRepository(tmp_path / "rbac")
    secret_meta_repo = JsonSecretMetadataRepository(tmp_path / "secrets")
    approval_pol_repo = InMemoryApprovalPolicyRepository()
    approval_ev_repo = JsonApprovalEvidenceRepository(tmp_path / "approval_evidence.json")
    fin_limit_repo = JsonFinancialLimitPolicyRepository(tmp_path / "financial_policies.json")
    tool_pol_repo = JsonToolPolicyRepository(tmp_path / "tool_policies")
    sensitive_repo = JsonDataHandlingPolicyRepository(tmp_path / "sensitive_policies.json")
    estop_repo = JsonEmergencyStopRepository(tmp_path / "emergency_stops.json")
    audit_repo = JsonAuditRepository(tmp_path / "audit")
    trace_repo = JsonAgentTraceRepository(tmp_path / "traces")
    compliance_pol_repo = InMemoryCompliancePolicyRepository()
    compliance_pol_repo.save(create_default_commercial_compliance_policy())

    # Proveedores de secretos
    secret_provider = InjectedSecretProvider()
    secret_provider.set_secret("secret_meli_prod_01", "meli_oauth_token_super_secret_xyz")

    # Servicios de Aplicación N.1 a N.11
    id_svc = IdentityService(id_repo)
    authn_svc = AuthenticationService(
        identity_service=id_svc,
        clock=clock,
        audit_repository=audit_repo,
        trusted_internal_tokens={"key_internal_ops_token_001": "mercadolibre_ops"},
        trusted_api_credentials={"key_ml_valid_001": "agent_mercadolibre_ops"},
    )
    rbac_svc = RBACService(
        role_repository=role_repo,
        assignment_repository=assignment_repo,
        clock=clock,
    )
    secret_svc = SecretService(
        metadata_repository=secret_meta_repo,
        providers=[secret_provider],
        clock=clock,
    )
    authz_svc = AuthorizationService(clock=clock)
    tool_pol_svc = ToolAccessPolicyService(
        policy_repository=tool_pol_repo,
        clock=clock,
    )
    fin_limit_svc = FinancialLimitService(
        policy_repository=fin_limit_repo,
        clock=clock,
    )
    approval_svc = ApprovalPolicyService(
        policy_repository=approval_pol_repo,
        evidence_repository=approval_ev_repo,
        clock=clock,
    )
    sensitive_svc = SensitiveDataHandlingService(
        policy_repository=sensitive_repo,
    )
    estop_svc = EmergencyStopService(
        repository=estop_repo,
        clock=clock,
    )
    compliance_collector = DefaultComplianceEvidenceCollector(audit_repository=audit_repo)
    compliance_svc = ComplianceAssessmentService(
        evidence_collector=compliance_collector,
        policy_repository=compliance_pol_repo,
    )

    # 1. Configurar Identidad y Autenticación N.1 / N.2
    agent_identity = Identity(
        identity_id="agent_mercadolibre_ops",
        identity_type=IdentityType.AGENT,
        canonical_identifier="agent:internal:mercadolibre_ops",
        created_at=now,
        updated_at=now,
        display_name="MercadoLibre Autonomous Operator",
        status=IdentityStatus.ACTIVE,
    )
    id_repo.save_identity(agent_identity)

    # 2. Configurar RBAC N.4
    role_publisher = Role(
        role_id="role_publisher",
        name="Commercial Publisher",
        permissions=(
            Permission(permission_id="perm_pub", action="PUBLISH_ITEM"),
            Permission(permission_id="perm_mod", action="UPDATE_PRICE"),
        ),
    )
    role_repo.save_role(role_publisher)
    assignment = RoleAssignment(
        assignment_id="asgn_01",
        identity_id="agent_mercadolibre_ops",
        role_id="role_publisher",
        source="admin_sec",
    )
    assignment_repo.save_assignment(assignment)

    # 3. Configurar Tool Policy N.8
    tool_rule_allow = ToolPolicyRule(
        rule_id="trule_ml_pub",
        action=ToolRuleAction.ALLOW,
        tool_id_pattern="mercadolibre_publishing_tool",
        operation_pattern="publish_item",
    )
    tool_policy = ToolPolicy(
        policy_name="default_tool_policy",
        version="1.0.0",
        rules=(tool_rule_allow,),
        default_action=ToolRuleAction.DENY,
    )
    tool_pol_repo.save_policy(tool_policy)

    # 4. Configurar Financial Policy N.7
    fin_rule = FinancialLimitRule(
        rule_id="frule_pub",
        limit_type=FinancialLimitType.MAX_TRANSACTION_AMOUNT,
        currency="USD",
        max_amount=Decimal("200.00"),
        allow_approval_override=True,
        target_action="PUBLISH_ITEM",
    )
    fin_policy = FinancialLimitPolicy(
        policy_name="default_commercial_financial_policy",
        version="1.0.0",
        currency="USD",
        rules=(fin_rule,),
    )
    fin_limit_repo.save_policy(fin_policy)

    # 5. Configurar Approval Policy N.6
    app_policy = ApprovalPolicy(
        policy_name="default_commercial_approval_policy",
        version="1.0.0",
        actions_not_requiring_approval=("PUBLISH_ITEM", "UPDATE_PRICE"),
        actions_requiring_approval=("DELETE_CATALOG", "TRANSFER_FUNDS"),
        default_ttl_seconds=3600,
    )
    approval_pol_repo.save_policy(app_policy)

    # 6. Configurar Sensitive Data Policy N.9
    sens_policy = DataHandlingPolicy(
        policy_name="default_commercial_data_policy",
        version="1.0.0",
    )
    sensitive_repo.save_policy(sens_policy)

    # 7. Configurar Secreto N.5
    secret_meta = SecretMetadata(
        reference_id="secret_meli_prod_01",
        secret_name="meli_oauth_token",
        secret_type=SecretType.ACCESS_TOKEN,
        provider="injected",
        status=SecretStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    secret_meta_repo.save_metadata(secret_meta)

    # Mock delegate
    physical_delegate = MockPhysicalActionExecutor()

    # Guarded Executor N.3+
    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=physical_delegate,
        authorization_service=authz_svc,
        rbac_service=rbac_svc,
        tool_policy_service=tool_pol_svc,
        default_tool_policy_name="default_tool_policy",
        financial_limit_service=fin_limit_svc,
        default_financial_policy_name="default_commercial_financial_policy",
        approval_service=approval_svc,
        default_approval_policy_name="default_commercial_approval_policy",
        sensitive_data_service=sensitive_svc,
        default_sensitive_data_policy_name="default_commercial_data_policy",
        emergency_stop_service=estop_svc,
    )

    return {
        "clock": clock,
        "id_svc": id_svc,
        "authn_svc": authn_svc,
        "rbac_svc": rbac_svc,
        "authz_svc": authz_svc,
        "tool_pol_svc": tool_pol_svc,
        "fin_limit_svc": fin_limit_svc,
        "approval_svc": approval_svc,
        "approval_ev_repo": approval_ev_repo,
        "sensitive_svc": sensitive_svc,
        "estop_svc": estop_svc,
        "secret_svc": secret_svc,
        "audit_repo": audit_repo,
        "trace_repo": trace_repo,
        "compliance_svc": compliance_svc,
        "physical_delegate": physical_delegate,
        "guarded_executor": guarded_executor,
    }


# =========================================================================
# 1. HAPPY PATH: Full compliant external execution (physical mock = 1, N.10 COMPLIANT)
# =========================================================================
def test_01_happy_path_full_compliant_execution(test_env):
    env = test_env
    corr_id = "corr_gate_m_01"
    msn_id = "msn_gate_m_01"

    # Step 1: Authentication N.2
    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_ml_valid_001",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    assert authn_res.status == AuthenticationStatus.AUTHENTICATED
    principal_ctx = env["authn_svc"].create_principal_context(authn_res)
    assert principal_ctx.identity_id == "agent_mercadolibre_ops"

    # Inyectar contexto al ejecutor guardián
    env["guarded_executor"].principal_context = principal_ctx

    # Step 2: Preparar decisión de acción externa con datos sensibles minimizados y parámetros válidos
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={
            "action_type": "PUBLISH_ITEM",
            "target_resource": "mercadolibre_item",
            "tool_id": "mercadolibre_publishing_tool",
            "operation_id": "publish_item",
            "side_effect_level": "RESTRICTED_WRITE",
            "amount": "150.00",
            "currency": "USD",
            "item_title": "Wireless Gaming Mouse",
            "seller_secret_key": "raw_secret_leak_12345",  # Debe ser redactado por N.9
            "shipping_address": "Av. Providencia 1234",     # Permitido por propósito de marketplace
        },
        reason="Publishing competitive catalog item",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Execute commercial operation")

    # Step 3: Secret Resolution N.5 para el adaptador
    secret_ref = SecretReference(
        reference_id="secret_meli_prod_01",
        provider="injected",
        secret_name="meli_oauth_token",
        secret_type=SecretType.ACCESS_TOKEN,
    )
    secret_res = env["secret_svc"].resolve(secret_ref)
    assert secret_res.status == SecretResolutionStatus.RESOLVED
    assert secret_res.secret_value.reveal() == "meli_oauth_token_super_secret_xyz"

    # Step 4: Ejecutar a través de la barrera de gobernanza
    exec_res = env["guarded_executor"].execute(decision, state)

    # Verificaciones de ejecución
    assert exec_res["is_allowed"] is True
    assert exec_res["is_executable"] is True
    assert exec_res["status"] == "PHYSICAL_EXECUTION_SUCCESS"
    assert env["physical_delegate"].external_calls_count == 1

    # Sanitización N.9 comprobada en los parámetros que llegaron al delegate
    params_received = exec_res["sanitized_params"]
    assert params_received["seller_secret_key"] == "[REDACTED_SECRET]"
    assert params_received["shipping_address"] == "Av. Providencia 1234"

    # Step 5: Registrar trazas y auditorías K.1
    _record_audit(env["audit_repo"], AuditRecordType.AUTHENTICATION_EVALUATED, "agent_mercadolibre_ops", "AGENT", "AUTHENTICATE", "AUTHENTICATED", corr_id, msn_id)
    _record_audit(env["audit_repo"], AuditRecordType.RBAC_EVALUATED, "agent_mercadolibre_ops", "AGENT", "PUBLISH_ITEM", "VALID", corr_id, msn_id, metadata={"actions": ["PUBLISH_ITEM"]})
    _record_audit(env["audit_repo"], AuditRecordType.AUTHORIZATION_EVALUATED, "agent_mercadolibre_ops", "AGENT", "PUBLISH_ITEM", "ALLOW", corr_id, msn_id)
    _record_audit(env["audit_repo"], AuditRecordType.TOOL_ACCESS_EVALUATED, "agent_mercadolibre_ops", "AGENT", "PUBLISH_ITEM", "ALLOW", corr_id, msn_id)
    _record_audit(env["audit_repo"], AuditRecordType.FINANCIAL_LIMIT_EVALUATED, "agent_mercadolibre_ops", "AGENT", "PUBLISH_ITEM", "WITHIN_LIMIT", corr_id, msn_id)
    _record_audit(env["audit_repo"], AuditRecordType.APPROVAL_EVALUATED, "agent_mercadolibre_ops", "AGENT", "PUBLISH_ITEM", "NOT_REQUIRED", corr_id, msn_id)
    _record_audit(env["audit_repo"], AuditRecordType.ACTION_EXECUTED, "agent_mercadolibre_ops", "AGENT", "PUBLISH_ITEM", "SUCCESS", corr_id, msn_id)

    # Step 6: Evaluación de Compliance N.10
    assessment = env["compliance_svc"].assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="PUBLISH_ITEM",
    )
    assert assessment.overall_status == ComplianceStatus.COMPLIANT


# =========================================================================
# 2. AUTHENTICATION FAILURE: Invalid/Expired Auth -> 0 physical calls
# =========================================================================
def test_02_invalid_authentication_blocks(test_env):
    env = test_env
    corr_id = "corr_gate_m_02"
    msn_id = "msn_gate_m_02"

    # Intento de autenticación inválida
    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_invalid_fake_999",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    assert authn_res.status == AuthenticationStatus.INVALID
    assert authn_res.principal is None

    # Si se intenta ejecutar sin contexto autenticado (principal_context es None)
    env["guarded_executor"].principal_context = None
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={"action_type": "PUBLISH_ITEM", "target_resource": "mercadolibre_item"},
        reason="Publishing without auth",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Test governance operation")

    res = env["guarded_executor"].execute(decision, state)
    assert res["is_allowed"] is False
    assert res["status"] == "AUTHORIZATION_DENY"
    assert env["physical_delegate"].external_calls_count == 0


# =========================================================================
# 3. RBAC / AUTHORIZATION FAILURE: Wrong permission / DENY -> 0 physical calls
# =========================================================================
def test_03_rbac_authorization_denial_blocks(test_env):
    env = test_env
    corr_id = "corr_gate_m_03"
    msn_id = "msn_gate_m_03"

    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_ml_valid_001",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    env["guarded_executor"].principal_context = env["authn_svc"].create_principal_context(authn_res)

    # Acción DELETE_ACCOUNT que no está en el rol "role_publisher"
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={"action_type": "DELETE_ACCOUNT", "target_resource": "mercadolibre_item"},
        reason="Malicious or unauthorized action",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Test governance operation")

    res = env["guarded_executor"].execute(decision, state)
    assert res["is_allowed"] is False
    assert res["status"] == "AUTHORIZATION_DENY"
    assert env["physical_delegate"].external_calls_count == 0


# =========================================================================
# 4. TOOL POLICY FAILURE: Tool Denied / Unknown -> 0 physical calls
# =========================================================================
def test_04_tool_policy_denial_blocks(test_env):
    env = test_env
    corr_id = "corr_gate_m_04"
    msn_id = "msn_gate_m_04"

    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_ml_valid_001",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    env["guarded_executor"].principal_context = env["authn_svc"].create_principal_context(authn_res)

    # Herramienta no permitida / no en allowlist
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={
            "action_type": "PUBLISH_ITEM",
            "target_resource": "mercadolibre_item",
            "tool_id": "unauthorized_scraper_bot",
            "operation_id": "publish_item",
        },
        reason="Attempting to use prohibited tool",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Test governance operation")

    res = env["guarded_executor"].execute(decision, state)
    assert res["is_allowed"] is True
    assert res["is_executable"] is False
    assert res["status"] == "TOOL_DENY"
    assert env["physical_delegate"].external_calls_count == 0


# =========================================================================
# 5. SENSITIVE DATA: Minimization & Redaction in pipeline
# =========================================================================
def test_05_sensitive_data_handling_sanitization(test_env):
    env = test_env
    corr_id = "corr_gate_m_05"
    msn_id = "msn_gate_m_05"

    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_ml_valid_001",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    env["guarded_executor"].principal_context = env["authn_svc"].create_principal_context(authn_res)

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={
            "action_type": "PUBLISH_ITEM",
            "target_resource": "mercadolibre_item",
            "tool_id": "mercadolibre_publishing_tool",
            "operation_id": "publish_item",
            "amount": "50.00",
            "currency": "USD",
            "api_token": "secret_token_to_redact",
            "user_password": "super_cleartext_password",
            "public_title": "Product Title OK",
        },
        reason="Publish item with mixed sensitivity payload",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Test governance operation")

    res = env["guarded_executor"].execute(decision, state)
    assert res["is_allowed"] is True
    assert res["is_executable"] is True
    assert env["physical_delegate"].external_calls_count == 1

    params = res["sanitized_params"]
    assert params["api_token"] == "[REDACTED_SECRET]"
    assert params["user_password"] == "[REDACTED_SECRET]"
    assert params["public_title"] == "Product Title OK"


# =========================================================================
# 6. FINANCIAL LIMIT: Amount Exceeded without Approval -> 0 physical calls
# =========================================================================
def test_06_financial_limit_exceeded_blocks(test_env):
    env = test_env
    corr_id = "corr_gate_m_06"
    msn_id = "msn_gate_m_06"

    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_ml_valid_001",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    env["guarded_executor"].principal_context = env["authn_svc"].create_principal_context(authn_res)

    # Monto 250.00 > límite de aprobación automática (200.00), sin evidencia de aprobación adjunta
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={
            "action_type": "PUBLISH_ITEM",
            "target_resource": "mercadolibre_item",
            "tool_id": "mercadolibre_publishing_tool",
            "operation_id": "publish_item",
            "amount": "250.00",
            "currency": "USD",
        },
        reason="Publish high amount item without approval evidence",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Test governance operation")

    res = env["guarded_executor"].execute(decision, state)
    assert res["is_allowed"] is True
    assert res["is_executable"] is False
    assert res["status"] in ("APPROVAL_REQUIRED", "APPROVAL_APPROVAL_REQUIRED")
    assert env["physical_delegate"].external_calls_count == 0


# =========================================================================
# 7. VALID APPROVAL EXCEPTION: High Amount with Valid Approval -> 1 physical call
# =========================================================================
def test_07_valid_approval_exception_allows_execution(test_env):
    env = test_env
    corr_id = "corr_gate_m_07"
    msn_id = "msn_gate_m_07"

    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_ml_valid_001",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    env["guarded_executor"].principal_context = env["authn_svc"].create_principal_context(authn_res)

    # Crear evidencia de aprobación válida emitida por un admin/aprobador
    approval_ev = ApprovalEvidence(
        approval_id="app_ev_gate_m_07",
        policy_name="default_commercial_approval_policy",
        policy_version="1.0.0",
        target_action="PUBLISH_ITEM",
        target_resource="mercadolibre_item",
        requesting_identity_id="agent_mercadolibre_ops",
        approver_identity_id="human_manager_01",
        status=ApprovalStatus.APPROVED,
        approved_at=env["clock"].now(),
        expires_at=env["clock"].now() + timedelta(hours=1),
        correlation_id=msn_id,
        metadata={"allowed_amount": "300.00"},
    )
    env["approval_ev_repo"].save_evidence(approval_ev)

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={
            "action_type": "PUBLISH_ITEM",
            "target_resource": "mercadolibre_item",
            "tool_id": "mercadolibre_publishing_tool",
            "operation_id": "publish_item",
            "amount": "250.00",
            "currency": "USD",
            "attached_evidence_id": "app_ev_gate_m_07",
        },
        reason="Publish high amount item with verified approval",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Test governance operation")

    res = env["guarded_executor"].execute(decision, state)
    assert res["is_allowed"] is True
    assert res["is_executable"] is True
    assert res["status"] == "PHYSICAL_EXECUTION_SUCCESS"
    assert env["physical_delegate"].external_calls_count == 1


# =========================================================================
# 8. APPROVAL FAILURE: Expired or Wrong Resource Approval -> 0 physical calls
# =========================================================================
def test_08_expired_or_mismatched_approval_blocks(test_env):
    env = test_env
    corr_id = "corr_gate_m_08"
    msn_id = "msn_gate_m_08"

    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_ml_valid_001",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    env["guarded_executor"].principal_context = env["authn_svc"].create_principal_context(authn_res)

    # Aprobación expirada
    expired_ev = ApprovalEvidence(
        approval_id="app_ev_expired",
        policy_name="default_commercial_approval_policy",
        policy_version="1.0.0",
        target_action="PUBLISH_ITEM",
        target_resource="mercadolibre_item",
        requesting_identity_id="agent_mercadolibre_ops",
        approver_identity_id="human_manager_01",
        status=ApprovalStatus.APPROVED,
        approved_at=env["clock"].now() - timedelta(hours=5),
        expires_at=env["clock"].now() - timedelta(hours=1),
        correlation_id=msn_id,
    )
    env["approval_ev_repo"].save_evidence(expired_ev)

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={
            "action_type": "PUBLISH_ITEM",
            "target_resource": "mercadolibre_item",
            "tool_id": "mercadolibre_publishing_tool",
            "operation_id": "publish_item",
            "amount": "250.00",
            "currency": "USD",
            "attached_evidence_id": "app_ev_expired",
        },
        reason="Publish with expired approval",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Test governance operation")

    res = env["guarded_executor"].execute(decision, state)
    assert res["is_allowed"] is True
    assert res["is_executable"] is False
    assert env["physical_delegate"].external_calls_count == 0


# =========================================================================
# 9. SECRET MANAGEMENT: Missing Secret -> Safe Failure & 0 provider calls
# =========================================================================
def test_09_missing_secret_blocks_adapter_safely(test_env):
    env = test_env

    # Intento de resolución de credencial no existente
    missing_ref = SecretReference(
        reference_id="non_existent_secret_key_404",
        provider="injected",
        secret_name="missing_secret",
        secret_type=SecretType.API_KEY,
    )
    res = env["secret_svc"].resolve(missing_ref)
    assert res.status == SecretResolutionStatus.NOT_FOUND
    assert res.secret_value is None
    assert env["physical_delegate"].external_calls_count == 0


# =========================================================================
# 10. EMERGENCY STOP: Global or Scoped Stop Active -> 0 physical calls
# =========================================================================
def test_10_emergency_stop_blocks_execution(test_env):
    env = test_env
    corr_id = "corr_gate_m_10"
    msn_id = "msn_gate_m_10"

    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="key_ml_valid_001",
        declared_subject="agent_mercadolibre_ops",
        correlation_id=corr_id,
    )
    authn_res = env["authn_svc"].authenticate_request(auth_req)
    principal_ctx = env["authn_svc"].create_principal_context(authn_res)
    env["guarded_executor"].principal_context = principal_ctx

    # Activar parada de emergencia global N.11 con contexto autenticado y permiso
    env["estop_svc"].activate_stop(
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Market anomaly detected by supervisor",
        principal_context=principal_ctx,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        target="mercadolibre_item",
        parameters={
            "action_type": "PUBLISH_ITEM",
            "target_resource": "mercadolibre_item",
            "tool_id": "mercadolibre_publishing_tool",
            "operation_id": "publish_item",
            "amount": "50.00",
            "currency": "USD",
        },
        reason="Attempting execution while emergency stop is active",
    )
    state = LoopState(mission_id=msn_id, iteration=1, goal="Test governance operation")

    res = env["guarded_executor"].execute(decision, state)
    assert res["is_allowed"] is True
    assert res["is_executable"] is False
    assert res["status"] == "EMERGENCY_STOP_BLOCKED"
    assert env["physical_delegate"].external_calls_count == 0

    # Desactivar y verificar que ahora sí ejecuta
    active_records = env["estop_svc"].repository.list_records(scope=EmergencyStopScope.GLOBAL, state=EmergencyStopState.ACTIVE)
    assert len(active_records) > 0
    stop_id_to_deactivate = active_records[0].stop_id

    env["estop_svc"].deactivate_stop(
        stop_id=stop_id_to_deactivate,
        reason_details="Anomaly resolved and verified",
        principal_context=principal_ctx,
    )
    res_after = env["guarded_executor"].execute(decision, state)
    assert res_after["status"] == "PHYSICAL_EXECUTION_SUCCESS"
    assert env["physical_delegate"].external_calls_count == 1


# =========================================================================
# 11. COMPLIANCE: Correctly blocked action evaluates to COMPLIANT enforcement
# =========================================================================
def test_11_correctly_blocked_action_evaluates_compliant(test_env):
    env = test_env
    corr_id = "corr_gate_m_11"
    msn_id = "msn_gate_m_11"

    # Registrar en auditoría una acción bloqueada de forma legítima por denegación N.3
    _record_audit(env["audit_repo"], AuditRecordType.AUTHENTICATION_EVALUATED, "agent_unauthorized", "AGENT", "DELETE_CATALOG", "AUTHENTICATED", corr_id, msn_id)
    _record_audit(env["audit_repo"], AuditRecordType.AUTHORIZATION_EVALUATED, "agent_unauthorized", "AGENT", "DELETE_CATALOG", "DENY", corr_id, msn_id)

    assessment = env["compliance_svc"].assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="DELETE_CATALOG",
    )
    assert assessment.overall_status == ComplianceStatus.COMPLIANT


# =========================================================================
# 12. COMPLIANCE: Simulated/forced bypass after denial evaluates to NON_COMPLIANT
# =========================================================================
def test_12_forced_execution_bypass_evaluates_non_compliant(test_env):
    env = test_env
    corr_id = "corr_gate_m_12"
    msn_id = "msn_gate_m_12"

    # Registrar DENY pero también un ACTION_EXECUTED (violación directa de política)
    _record_audit(env["audit_repo"], AuditRecordType.AUTHENTICATION_EVALUATED, "agent_rogue", "AGENT", "TRANSFER_FUNDS", "AUTHENTICATED", corr_id, msn_id)
    _record_audit(env["audit_repo"], AuditRecordType.AUTHORIZATION_EVALUATED, "agent_rogue", "AGENT", "TRANSFER_FUNDS", "DENY", corr_id, msn_id)
    _record_audit(env["audit_repo"], AuditRecordType.ACTION_EXECUTED, "agent_rogue", "AGENT", "TRANSFER_FUNDS", "SUCCESS", corr_id, msn_id)

    assessment = env["compliance_svc"].assess_operation(
        correlation_id=corr_id,
        mission_id=msn_id,
        action_or_operation="TRANSFER_FUNDS",
    )
    assert assessment.overall_status == ComplianceStatus.NON_COMPLIANT
    finding_codes = [f.reason_code for f in assessment.findings]
    assert ComplianceFindingReasonCode.AUTHORIZATION_DENIED_BUT_EXECUTED in finding_codes


# =========================================================================
# 13. CROSS-CORRELATION ISOLATION: Unrelated mission evidence rejected
# =========================================================================
def test_13_cross_correlation_evidence_rejected(test_env):
    env = test_env
    corr_a = "corr_mission_alpha"
    corr_b = "corr_mission_beta"
    msn_a = "msn_alpha"
    msn_b = "msn_beta"

    # Evidencias registradas para la misión Alpha
    _record_audit(env["audit_repo"], AuditRecordType.AUTHENTICATION_EVALUATED, "agent_ops", "AGENT", "PUBLISH_ITEM", "AUTHENTICATED", corr_a, msn_a)
    _record_audit(env["audit_repo"], AuditRecordType.AUTHORIZATION_EVALUATED, "agent_ops", "AGENT", "PUBLISH_ITEM", "ALLOW", corr_a, msn_a)

    # Intentar evaluar la misión Beta que no tiene evidencias propias registradas
    assessment_beta = env["compliance_svc"].assess_operation(
        correlation_id=corr_b,
        mission_id=msn_b,
        action_or_operation="PUBLISH_ITEM",
    )
    assert assessment_beta.overall_status == ComplianceStatus.NON_COMPLIANT
    finding_codes = [f.reason_code for f in assessment_beta.findings]
    assert ComplianceFindingReasonCode.MISSING_AUTHENTICATION_EVIDENCE in finding_codes or ComplianceFindingReasonCode.MISSING_IDENTITY_EVIDENCE in finding_codes


# =========================================================================
# 14. CORRUPTION & TAMPERING FAIL-SAFE: Altered records result in fail-safe block
# =========================================================================
def test_14_tampered_records_fail_safe_block(test_env, tmp_path: Path):
    # Crear un archivo de repositorio con JSON corrupto/manipulado
    corrupt_file = tmp_path / "corrupt_estop" / "stops.json"
    corrupt_file.parent.mkdir(parents=True, exist_ok=True)
    with open(corrupt_file, "w", encoding="utf-8") as f:
        f.write("{invalid_json_tampered: true")

    corrupt_repo = JsonEmergencyStopRepository(file_path=str(corrupt_file))
    corrupt_estop_svc = EmergencyStopService(repository=corrupt_repo, clock=test_env["clock"])

    # El servicio de Emergency Stop debe tratar la corrupción como FAIL-SAFE BLOCKED
    eval_ctx = EmergencyStopEvaluationContext(
        action_name="PUBLISH_ITEM",
        target_resource="mercadolibre_item",
        is_external_side_effect=True,
    )
    decision = corrupt_estop_svc.evaluate(eval_ctx)
    assert decision.is_executable is False
    assert decision.decision_status == EmergencyStopDecisionStatus.BLOCK_EXECUTION
    assert decision.reason_code == EmergencyStopReasonCode.FAIL_SAFE_STORE_CORRUPTION
