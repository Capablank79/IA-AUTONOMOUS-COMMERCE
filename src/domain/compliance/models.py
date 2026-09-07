"""
Domain models for N.10 — Audit / Compliance (Transversal N — Security, Governance & Safety).

Responde deterministamente a la pregunta central de N.10:
"¿Podemos demostrar de forma verificable que una operación cumplió —o violó—
las políticas de seguridad y gobernanza aplicables?"

Principios N.10:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuplas).
- Estados canónicos: COMPLIANT, NON_COMPLIANT, INCOMPLETE, UNKNOWN, ERROR.
- Fail-Secure: UNKNOWN != COMPLIANT. Falta de evidencia o evidencia corrupta != COMPLIANT.
- Reconstrucción causal y retrospectiva referenciando K.1 (Audit), K.2 (Trace), N.1–N.9.
- No duplicación de eventos de auditoría (K.1) ni trazas (K.2).
- Checksums SHA-256 canónicos deterministas para evidencia, hallazgos y assessments.
- Integridad y aislamiento de correlación (correlation_id, mission_id, action/resource).
- Sanitización y respeto a la privacidad (N.9) y secretos (N.5): CERO payloads sensibles completos ni CoT.
- Versionado estricto de políticas evaluadas.
- Frontera N.11 estricta: NO implementa Emergency Stop / Kill Switch.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, Set

from src.domain.security.models import (
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)


class ComplianceStatus(str, Enum):
    """
    Estados canónicos de conformidad de cumplimiento (N.10).
    - COMPLIANT: Todos los requerimientos aplicables están verificados y conformes con evidencia válida.
    - NON_COMPLIANT: Violación explícita de al menos una política o control requerido.
    - INCOMPLETE: Falta evidencia requerida para concluir la evaluación (missing evidence).
    - UNKNOWN: Evidencia indeterminada, ambigua o con confianza no evaluable.
    - ERROR: Falla de integridad, corrupción de datos o excepción en la evaluación.
    """
    COMPLIANT = "COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class ComplianceFindingSeverity(str, Enum):
    """
    Taxonomía canónica de severidad para hallazgos de cumplimiento.
    """
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ComplianceFindingReasonCode(str, Enum):
    """
    Códigos de razón canónicos y estructurados para hallazgos de cumplimiento (Findings).
    """
    # Conformidad
    COMPLIANT_EXECUTION = "COMPLIANT_EXECUTION"
    COMPLIANT_BLOCKED_ACTION = "COMPLIANT_BLOCKED_ACTION"
    CONTROL_NOT_APPLICABLE = "CONTROL_NOT_APPLICABLE"

    # Evidencia faltante / incompleta
    MISSING_IDENTITY_EVIDENCE = "MISSING_IDENTITY_EVIDENCE"
    MISSING_AUTHENTICATION_EVIDENCE = "MISSING_AUTHENTICATION_EVIDENCE"
    MISSING_RBAC_EVIDENCE = "MISSING_RBAC_EVIDENCE"
    MISSING_AUTHORIZATION_EVIDENCE = "MISSING_AUTHORIZATION_EVIDENCE"
    MISSING_TOOL_POLICY_EVIDENCE = "MISSING_TOOL_POLICY_EVIDENCE"
    MISSING_FINANCIAL_LIMIT_EVIDENCE = "MISSING_FINANCIAL_LIMIT_EVIDENCE"
    MISSING_APPROVAL_EVIDENCE = "MISSING_APPROVAL_EVIDENCE"
    MISSING_SENSITIVE_DATA_EVIDENCE = "MISSING_SENSITIVE_DATA_EVIDENCE"
    MISSING_AUDIT_TRAIL = "MISSING_AUDIT_TRAIL"
    MISSING_TRACE_EVIDENCE = "MISSING_TRACE_EVIDENCE"
    MISSING_REQUIRED_CONTROL_EVIDENCE = "MISSING_REQUIRED_CONTROL_EVIDENCE"

    # Violaciones directas
    UNAUTHENTICATED_EXECUTION = "UNAUTHENTICATED_EXECUTION"
    AUTHORIZATION_DENIED_BUT_EXECUTED = "AUTHORIZATION_DENIED_BUT_EXECUTED"
    UNAUTHORIZED_EXECUTION = "UNAUTHORIZED_EXECUTION"
    TOOL_POLICY_DENIED_BUT_EXECUTED = "TOOL_POLICY_DENIED_BUT_EXECUTED"
    FINANCIAL_LIMIT_EXCEEDED_WITHOUT_APPROVAL = "FINANCIAL_LIMIT_EXCEEDED_WITHOUT_APPROVAL"
    FINANCIAL_LIMIT_EXCEEDED = "FINANCIAL_LIMIT_EXCEEDED"
    APPROVAL_REQUIRED_BUT_MISSING = "APPROVAL_REQUIRED_BUT_MISSING"
    APPROVAL_REJECTED_BUT_EXECUTED = "APPROVAL_REJECTED_BUT_EXECUTED"
    SELF_APPROVAL_VIOLATION = "SELF_APPROVAL_VIOLATION"
    SENSITIVE_DATA_LEAKAGE = "SENSITIVE_DATA_LEAKAGE"
    SENSITIVE_DATA_BLOCKED_BUT_EXECUTED = "SENSITIVE_DATA_BLOCKED_BUT_EXECUTED"
    EMERGENCY_STOP_BLOCKED_BUT_EXECUTED = "EMERGENCY_STOP_BLOCKED_BUT_EXECUTED"
    SECRET_EXPOSURE_VIOLATION = "SECRET_EXPOSURE_VIOLATION"
    POLICY_BYPASS_DETECTED = "POLICY_BYPASS_DETECTED"

    # Integridad y Correlación
    AUDIT_INTEGRITY_FAILURE = "AUDIT_INTEGRITY_FAILURE"
    TRACE_INTEGRITY_FAILURE = "TRACE_INTEGRITY_FAILURE"
    EVIDENCE_TAMPERED = "EVIDENCE_TAMPERED"
    CORRELATION_MISMATCH = "CORRELATION_MISMATCH"
    CROSS_CORRELATION_EVIDENCE_REJECTED = "CROSS_CORRELATION_EVIDENCE_REJECTED"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    UNKNOWN_EVIDENCE_STATUS = "UNKNOWN_EVIDENCE_STATUS"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    EVALUATION_ERROR = "EVALUATION_ERROR"


class ComplianceRequirementType(str, Enum):
    """
    Requerimientos canónicos explícitos basados en controles reales (N.1 a N.9 + K.1/K.2).
    """
    IDENTITY_REQUIRED = "IDENTITY_REQUIRED"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    RBAC_PERMISSIONS_REQUIRED = "RBAC_PERMISSIONS_REQUIRED"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    TOOL_POLICY_REQUIRED = "TOOL_POLICY_REQUIRED"
    FINANCIAL_LIMIT_REQUIRED = "FINANCIAL_LIMIT_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    SENSITIVE_DATA_POLICY_REQUIRED = "SENSITIVE_DATA_POLICY_REQUIRED"
    EMERGENCY_STOP_ENFORCED = "EMERGENCY_STOP_ENFORCED"
    SECRET_LEAKAGE_PROHIBITED = "SECRET_LEAKAGE_PROHIBITED"
    AUDIT_TRAIL_REQUIRED = "AUDIT_TRAIL_REQUIRED"
    TRACE_SEQUENCE_REQUIRED = "TRACE_SEQUENCE_REQUIRED"


class ComplianceEvidenceType(str, Enum):
    """
    Tipos de evidencia formal referenciable en una evaluación de compliance.
    """
    AUDIT_RECORD = "AUDIT_RECORD"
    TRACE_RECORD = "TRACE_RECORD"
    IDENTITY_DECISION = "IDENTITY_DECISION"
    AUTHENTICATION_RESULT = "AUTHENTICATION_RESULT"
    RBAC_EVALUATION = "RBAC_EVALUATION"
    AUTHORIZATION_DECISION = "AUTHORIZATION_DECISION"
    TOOL_ACCESS_DECISION = "TOOL_ACCESS_DECISION"
    FINANCIAL_LIMIT_DECISION = "FINANCIAL_LIMIT_DECISION"
    APPROVAL_DECISION = "APPROVAL_DECISION"
    APPROVAL_EVIDENCE = "APPROVAL_EVIDENCE"
    SENSITIVE_DATA_DECISION = "SENSITIVE_DATA_DECISION"
    EMERGENCY_STOP_DECISION = "EMERGENCY_STOP_DECISION"
    ACTION_RESULT = "ACTION_RESULT"
    OTHER = "OTHER"


def _canonical_json(data: Any) -> str:
    """Serializa deterministamente a JSON con claves ordenadas."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def compute_evidence_checksum(
    evidence_id: str,
    evidence_type: str,
    source_component: str,
    correlation_id: str,
    original_checksum: Optional[str] = None,
) -> str:
    """Calcula el checksum SHA-256 de una referencia de evidencia."""
    payload = {
        "evidence_id": evidence_id,
        "evidence_type": str(evidence_type),
        "source_component": source_component,
        "correlation_id": correlation_id,
        "original_checksum": original_checksum or "",
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def compute_finding_checksum(
    finding_id: str,
    requirement_id: str,
    status: str,
    severity: str,
    reason_code: str,
    correlation_id: str,
    evidence_ids: Sequence[str],
) -> str:
    """Calcula el checksum SHA-256 determinista de un hallazgo (Finding)."""
    payload = {
        "finding_id": finding_id,
        "requirement_id": requirement_id,
        "status": str(status),
        "severity": str(severity),
        "reason_code": str(reason_code),
        "correlation_id": correlation_id,
        "evidence_ids": sorted(list(evidence_ids)),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def compute_assessment_checksum(
    assessment_id: str,
    mission_id: Optional[str],
    correlation_id: str,
    overall_status: str,
    policy_name: str,
    policy_version: str,
    finding_checksums: Sequence[str],
    evaluated_at_iso: str,
) -> str:
    """Calcula el checksum SHA-256 determinista de una evaluación de cumplimiento completa."""
    payload = {
        "assessment_id": assessment_id,
        "mission_id": mission_id or "",
        "correlation_id": correlation_id,
        "overall_status": str(overall_status),
        "policy_name": policy_name,
        "policy_version": policy_version,
        "finding_checksums": sorted(list(finding_checksums)),
        "evaluated_at": evaluated_at_iso,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ComplianceRequirement:
    """
    Definición inmutable de un requerimiento de control de cumplimiento.
    """
    requirement_id: str
    requirement_type: ComplianceRequirementType
    title: str
    description: str
    is_mandatory: bool = True
    default_severity_on_failure: ComplianceFindingSeverity = ComplianceFindingSeverity.HIGH
    applicable_actions: Tuple[str, ...] = field(default_factory=tuple)
    applicable_resource_types: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.requirement_id, "requirement_id")
        if not isinstance(self.requirement_type, ComplianceRequirementType):
            try:
                object.__setattr__(self, "requirement_type", ComplianceRequirementType(self.requirement_type))
            except Exception as e:
                raise ValueError(f"Invalid requirement_type: {self.requirement_type}") from e
        if not isinstance(self.default_severity_on_failure, ComplianceFindingSeverity):
            try:
                object.__setattr__(self, "default_severity_on_failure", ComplianceFindingSeverity(self.default_severity_on_failure))
            except Exception as e:
                raise ValueError(f"Invalid default_severity_on_failure: {self.default_severity_on_failure}") from e
        if not isinstance(self.applicable_actions, tuple):
            object.__setattr__(self, "applicable_actions", tuple(self.applicable_actions))
        if not isinstance(self.applicable_resource_types, tuple):
            object.__setattr__(self, "applicable_resource_types", tuple(self.applicable_resource_types))
        sanitized_meta = sanitize_security_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    def applies_to_action(self, action: str, resource_type: Optional[str] = None) -> bool:
        """Determina si este requerimiento aplica a una acción y recurso dados."""
        if not self.applicable_actions and not self.applicable_resource_types:
            return True
        action_match = not self.applicable_actions or action in self.applicable_actions or "*" in self.applicable_actions
        resource_match = not self.applicable_resource_types or (resource_type and resource_type in self.applicable_resource_types) or "*" in self.applicable_resource_types
        return action_match and resource_match


@dataclass(frozen=True)
class ComplianceEvidenceReference:
    """
    Referencia inmutable y ligera a una evidencia recolectada de otros componentes (K.1, K.2, N.1-N.9).
    NO duplica payloads completos; referencia identificadores, tipos, hashes y metadatos no sensibles.
    """
    evidence_id: str
    evidence_type: ComplianceEvidenceType
    source_component: str
    correlation_id: str
    mission_id: Optional[str] = None
    original_checksum: Optional[str] = None
    integrity_verified: bool = True
    observed_status: str = "UNKNOWN"
    summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        if not self.evidence_id or not isinstance(self.evidence_id, str):
            raise ValueError("evidence_id must be a non-empty string.")
        if not isinstance(self.evidence_type, ComplianceEvidenceType):
            try:
                object.__setattr__(self, "evidence_type", ComplianceEvidenceType(self.evidence_type))
            except Exception as e:
                raise ValueError(f"Invalid evidence_type: {self.evidence_type}") from e
        if not self.source_component or not isinstance(self.source_component, str):
            raise ValueError("source_component must be a non-empty string.")
        if not self.correlation_id or not isinstance(self.correlation_id, str):
            raise ValueError("correlation_id must be a non-empty string.")

        sanitized_meta = sanitize_security_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if not self.checksum:
            computed = compute_evidence_checksum(
                evidence_id=self.evidence_id,
                evidence_type=self.evidence_type.value,
                source_component=self.source_component,
                correlation_id=self.correlation_id,
                original_checksum=self.original_checksum,
            )
            object.__setattr__(self, "checksum", computed)


@dataclass(frozen=True)
class ComplianceFinding:
    """
    Hallazgo inmutable resultante de la evaluación de un requerimiento específico.
    """
    finding_id: str
    requirement_id: str
    status: ComplianceStatus
    severity: ComplianceFindingSeverity
    reason_code: ComplianceFindingReasonCode
    description: str
    correlation_id: str
    mission_id: Optional[str] = None
    action_or_operation: Optional[str] = None
    resource: Optional[str] = None
    evidence_references: Tuple[ComplianceEvidenceReference, ...] = field(default_factory=tuple)
    policy_name: str = "default_compliance_policy"
    policy_version: str = "1.0.0"
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.finding_id, "finding_id")
        if not isinstance(self.status, ComplianceStatus):
            try:
                object.__setattr__(self, "status", ComplianceStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e
        if not isinstance(self.severity, ComplianceFindingSeverity):
            try:
                object.__setattr__(self, "severity", ComplianceFindingSeverity(self.severity))
            except Exception as e:
                raise ValueError(f"Invalid severity: {self.severity}") from e
        if not isinstance(self.reason_code, ComplianceFindingReasonCode):
            try:
                object.__setattr__(self, "reason_code", ComplianceFindingReasonCode(self.reason_code))
            except Exception as e:
                raise ValueError(f"Invalid reason_code: {self.reason_code}") from e
        if not self.correlation_id or not isinstance(self.correlation_id, str):
            raise ValueError("correlation_id must be a non-empty string.")
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC).")
        if not isinstance(self.evidence_references, tuple):
            object.__setattr__(self, "evidence_references", tuple(self.evidence_references))

        sanitized_meta = sanitize_security_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if not self.checksum:
            evidence_ids = [e.evidence_id for e in self.evidence_references]
            computed = compute_finding_checksum(
                finding_id=self.finding_id,
                requirement_id=self.requirement_id,
                status=self.status.value,
                severity=self.severity.value,
                reason_code=self.reason_code.value,
                correlation_id=self.correlation_id,
                evidence_ids=evidence_ids,
            )
            object.__setattr__(self, "checksum", computed)

    @property
    def is_violation(self) -> bool:
        """Determina si este hallazgo constituye una no-conformidad o falla."""
        return self.status in (ComplianceStatus.NON_COMPLIANT, ComplianceStatus.ERROR)

    @property
    def is_incomplete(self) -> bool:
        return self.status == ComplianceStatus.INCOMPLETE

    @property
    def is_compliant(self) -> bool:
        return self.status == ComplianceStatus.COMPLIANT


@dataclass(frozen=True)
class ComplianceCheck:
    """
    Evaluación intermedia inmutable de un requerimiento sobre un conjunto de evidencias.
    """
    requirement: ComplianceRequirement
    finding: ComplianceFinding


@dataclass(frozen=True)
class CompliancePolicy:
    """
    Definición inmutable de una Política de Cumplimiento (CompliancePolicy).
    Configura qué requerimientos deben exigirse y su versión.
    """
    policy_name: str
    version: str = "1.0.0"
    description: str = "Standard Compliance Policy"
    requirements: Tuple[ComplianceRequirement, ...] = field(default_factory=tuple)
    fail_on_incomplete: bool = True
    reject_cross_correlation_evidence: bool = True
    require_audit_integrity: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.policy_name, "policy_name")
        if not self.version or not isinstance(self.version, str):
            raise ValueError("version must be a non-empty string.")
        if not isinstance(self.requirements, tuple):
            object.__setattr__(self, "requirements", tuple(self.requirements))
        sanitized_meta = sanitize_security_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))


@dataclass(frozen=True)
class ComplianceAssessment:
    """
    Evaluación final inmutable y consolidada de cumplimiento para una operación o misión.
    Responde formalmente a si la operación fue COMPLIANT, NON_COMPLIANT, INCOMPLETE, UNKNOWN o ERROR.
    """
    assessment_id: str
    overall_status: ComplianceStatus
    policy_name: str
    policy_version: str
    correlation_id: str
    mission_id: Optional[str] = None
    action_or_operation: Optional[str] = None
    resource: Optional[str] = None
    findings: Tuple[ComplianceFinding, ...] = field(default_factory=tuple)
    evidence_references: Tuple[ComplianceEvidenceReference, ...] = field(default_factory=tuple)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reconstruction_summary: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.assessment_id, "assessment_id")
        if not isinstance(self.overall_status, ComplianceStatus):
            try:
                object.__setattr__(self, "overall_status", ComplianceStatus(self.overall_status))
            except Exception as e:
                raise ValueError(f"Invalid overall_status: {self.overall_status}") from e
        if not self.policy_name or not isinstance(self.policy_name, str):
            raise ValueError("policy_name must be a non-empty string.")
        if not self.policy_version or not isinstance(self.policy_version, str):
            raise ValueError("policy_version must be a non-empty string.")
        if not self.correlation_id or not isinstance(self.correlation_id, str):
            raise ValueError("correlation_id must be a non-empty string.")
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC).")
        if not isinstance(self.findings, tuple):
            object.__setattr__(self, "findings", tuple(self.findings))
        if not isinstance(self.evidence_references, tuple):
            object.__setattr__(self, "evidence_references", tuple(self.evidence_references))

        sanitized_summary = sanitize_security_data(self.reconstruction_summary)
        object.__setattr__(self, "reconstruction_summary", deep_freeze(sanitized_summary))
        sanitized_meta = sanitize_security_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if not self.checksum:
            finding_hashes = [f.checksum or "" for f in self.findings]
            computed = compute_assessment_checksum(
                assessment_id=self.assessment_id,
                mission_id=self.mission_id,
                correlation_id=self.correlation_id,
                overall_status=self.overall_status.value,
                policy_name=self.policy_name,
                policy_version=self.policy_version,
                finding_checksums=finding_hashes,
                evaluated_at_iso=self.evaluated_at.isoformat(),
            )
            object.__setattr__(self, "checksum", computed)

    @property
    def is_compliant(self) -> bool:
        return self.overall_status == ComplianceStatus.COMPLIANT

    @property
    def is_non_compliant(self) -> bool:
        return self.overall_status == ComplianceStatus.NON_COMPLIANT

    @property
    def is_incomplete(self) -> bool:
        return self.overall_status == ComplianceStatus.INCOMPLETE

    @property
    def has_violations(self) -> bool:
        return any(f.is_violation for f in self.findings)

    @property
    def critical_findings(self) -> Tuple[ComplianceFinding, ...]:
        return tuple(f for f in self.findings if f.severity in (ComplianceFindingSeverity.HIGH, ComplianceFindingSeverity.CRITICAL) and f.is_violation)


@dataclass(frozen=True)
class ComplianceReport:
    """
    Representación estructurada y exportable de un reporte de cumplimiento (N.10).
    Respetuoso de la privacidad (N.9) y secretos (N.5).
    """
    report_id: str
    assessment: ComplianceAssessment
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    title: str = "Compliance & Governance Assessment Report"
    summary_text: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.report_id, "report_id")
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware (UTC).")
        if not self.summary_text:
            status_str = self.assessment.overall_status.value
            object.__setattr__(
                self,
                "summary_text",
                f"Assessment {self.assessment.assessment_id} concluded with status {status_str} under policy {self.assessment.policy_name}:{self.assessment.policy_version}. Findings: {len(self.assessment.findings)}."
            )

    def is_compliant(self) -> bool:
        return self.assessment.is_compliant

    def summary(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "overall_status": self.assessment.overall_status.value,
            "policy_name": self.assessment.policy_name,
            "policy_version": self.assessment.policy_version,
            "correlation_id": self.assessment.correlation_id,
            "mission_id": self.assessment.mission_id,
            "findings_count": len(self.assessment.findings),
            "evidence_count": len(self.assessment.evidence_references),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Exporta una representación sanitizada y estructurada para auditoría."""
        return {
            "report_id": self.report_id,
            "title": self.title,
            "generated_at": self.generated_at.isoformat(),
            "summary_text": self.summary_text,
            "assessment": {
                "assessment_id": self.assessment.assessment_id,
                "overall_status": self.assessment.overall_status.value,
                "policy_name": self.assessment.policy_name,
                "policy_version": self.assessment.policy_version,
                "correlation_id": self.assessment.correlation_id,
                "mission_id": self.assessment.mission_id,
                "action_or_operation": self.assessment.action_or_operation,
                "resource": self.assessment.resource,
                "evaluated_at": self.assessment.evaluated_at.isoformat(),
                "checksum": self.assessment.checksum,
                "reconstruction_summary": dict(self.assessment.reconstruction_summary),
                "findings": [
                    {
                        "finding_id": f.finding_id,
                        "requirement_id": f.requirement_id,
                        "status": f.status.value,
                        "severity": f.severity.value,
                        "reason_code": f.reason_code.value,
                        "description": f.description,
                        "checksum": f.checksum,
                        "evidence_ids": [e.evidence_id for e in f.evidence_references],
                    }
                    for f in self.assessment.findings
                ],
                "evidence_count": len(self.assessment.evidence_references),
            },
        }
