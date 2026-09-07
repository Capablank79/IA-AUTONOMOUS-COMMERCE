"""
Persistence implementation for Compliance Policies (N.10).

Provides thread-safe in-memory and JSON persistence for CompliancePolicy.
"""

from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional, Sequence
import json
import os
import tempfile

from src.domain.compliance.models import (
    CompliancePolicy,
    ComplianceRequirement,
    ComplianceRequirementType,
    ComplianceFindingSeverity,
)
from src.domain.compliance.ports import CompliancePolicyRepositoryPort
from src.domain.security.models import validate_safe_identifier


def create_default_commercial_compliance_policy(
    policy_name: str = "default_commercial_compliance_policy",
    version: str = "1.0.0",
) -> CompliancePolicy:
    """Crea la política de cumplimiento estándar para operaciones comerciales y de seguridad."""
    requirements = (
        ComplianceRequirement(
            requirement_id="REQ_IDENTITY",
            requirement_type=ComplianceRequirementType.IDENTITY_REQUIRED,
            title="Identity Verification Required",
            description="All operations must have a known and valid actor identity (N.1).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.HIGH,
        ),
        ComplianceRequirement(
            requirement_id="REQ_AUTHENTICATION",
            requirement_type=ComplianceRequirementType.AUTHENTICATION_REQUIRED,
            title="Actor Authentication Required",
            description="Actor identity must be successfully authenticated with valid credentials/tokens (N.2).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.CRITICAL,
        ),
        ComplianceRequirement(
            requirement_id="REQ_RBAC",
            requirement_type=ComplianceRequirementType.RBAC_PERMISSIONS_REQUIRED,
            title="RBAC Permissions Required",
            description="Actor must have effective permissions for the requested action and scope (N.4).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.HIGH,
        ),
        ComplianceRequirement(
            requirement_id="REQ_AUTHORIZATION",
            requirement_type=ComplianceRequirementType.AUTHORIZATION_REQUIRED,
            title="Explicit Authorization Required",
            description="Action execution requires an explicit ALLOW decision from authorization (N.3).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.CRITICAL,
        ),
        ComplianceRequirement(
            requirement_id="REQ_TOOL_POLICY",
            requirement_type=ComplianceRequirementType.TOOL_POLICY_REQUIRED,
            title="Tool Allowlist / Policy Compliance",
            description="Tool invocations must conform to tool allowlist/denylist rules and side effect levels (N.8).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.HIGH,
        ),
        ComplianceRequirement(
            requirement_id="REQ_FINANCIAL_LIMIT",
            requirement_type=ComplianceRequirementType.FINANCIAL_LIMIT_REQUIRED,
            title="Financial Limit Compliance",
            description="Financial/monetary operations must stay within configured limit thresholds (N.7).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.HIGH,
        ),
        ComplianceRequirement(
            requirement_id="REQ_APPROVAL",
            requirement_type=ComplianceRequirementType.APPROVAL_REQUIRED,
            title="Approval Policy Compliance",
            description="High-impact or out-of-limit actions require valid, verified and unexpired human/system approvals (N.6).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.CRITICAL,
        ),
        ComplianceRequirement(
            requirement_id="REQ_SENSITIVE_DATA",
            requirement_type=ComplianceRequirementType.SENSITIVE_DATA_POLICY_REQUIRED,
            title="Sensitive Data Handling Compliance",
            description="Operations processing sensitive/PII data must be authorized for purpose and redacted (N.9).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.HIGH,
        ),
        ComplianceRequirement(
            requirement_id="REQ_EMERGENCY_STOP",
            requirement_type=ComplianceRequirementType.EMERGENCY_STOP_ENFORCED,
            title="Emergency Stop Enforcement",
            description="Operations must adhere to upper execution governance emergency stops with zero physical execution when active (N.11).",
            is_mandatory=False,
            default_severity_on_failure=ComplianceFindingSeverity.CRITICAL,
        ),
        ComplianceRequirement(
            requirement_id="REQ_SECRET_PROTECTION",
            requirement_type=ComplianceRequirementType.SECRET_LEAKAGE_PROHIBITED,
            title="Secret Leakage Prohibited",
            description="Credentials, tokens, API keys and private CoT must never leak into logs, traces or domain payloads (N.5/K.8).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.CRITICAL,
        ),
        ComplianceRequirement(
            requirement_id="REQ_AUDIT_TRAIL",
            requirement_type=ComplianceRequirementType.AUDIT_TRAIL_REQUIRED,
            title="Audit Trail Required",
            description="Auditable security decisions and execution events must be recorded with verified integrity (K.1).",
            is_mandatory=True,
            default_severity_on_failure=ComplianceFindingSeverity.HIGH,
        ),
    )

    return CompliancePolicy(
        policy_name=policy_name,
        version=version,
        description="Standard commercial governance & compliance evaluation policy.",
        requirements=requirements,
        fail_on_incomplete=True,
        reject_cross_correlation_evidence=True,
        require_audit_integrity=True,
    )


class InMemoryCompliancePolicyRepository(CompliancePolicyRepositoryPort):
    """
    Repositorio thread-safe en memoria para políticas de cumplimiento.
    """

    def __init__(self, initial_policies: Optional[Sequence[CompliancePolicy]] = None):
        self._lock = RLock()
        self._policies: Dict[str, Dict[str, CompliancePolicy]] = {}

        # Registrar política por defecto
        default_policy = create_default_commercial_compliance_policy()
        self.save(default_policy)

        if initial_policies:
            for p in initial_policies:
                self.save(p)

    def save(self, policy: CompliancePolicy) -> None:
        with self._lock:
            if policy.policy_name not in self._policies:
                self._policies[policy.policy_name] = {}
            self._policies[policy.policy_name][policy.version] = policy

    def get_by_name(self, policy_name: str, version: Optional[str] = None) -> Optional[CompliancePolicy]:
        with self._lock:
            versions = self._policies.get(policy_name)
            if not versions:
                return None
            if version is not None:
                return versions.get(version)
            # Retornar la versión más reciente por orden de clave
            sorted_versions = sorted(versions.keys())
            return versions[sorted_versions[-1]]

    def list_all(self) -> Sequence[CompliancePolicy]:
        with self._lock:
            result = []
            for v_map in self._policies.values():
                result.extend(v_map.values())
            return tuple(result)
