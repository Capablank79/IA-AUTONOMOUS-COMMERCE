"""
Domain Models for N.6 — Approval Policies (Transversal N — Security, Governance & Safety).

Responde a la pregunta:
"¿Esta acción requiere aprobación explícita antes de ejecutarse y, si la requiere, existe una aprobación válida?"

Principios:
- Authorization ALLOW != Approval satisfied.
- Approval NO puede sobreescribir DENY de N.3.
- Approval Evidence está estrictamente ligada a (action + resource + requester + context).
- Expiración determinista con ClockPort K.7.
- Self-approval bloqueada por defecto cuando la política requiere separación de funciones.
- Inmutabilidad estricta (frozen=True, MappingProxyType).
- Cero límites financieros numéricos (N.7).
- Cero fuga de secretos N.5.
- Cero CoT / razonamiento interno (K.2).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union

from src.domain.security.models import (
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)


class ApprovalStatus(str, Enum):
    """
    Estados canónicos de evaluación y ciclo de vida de aprobación (N.6).
    """
    NOT_REQUIRED = "NOT_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class ApprovalReasonCode(str, Enum):
    """
    Códigos de razón estructurados, estables y deterministas para N.6.
    """
    APPROVAL_NOT_REQUIRED = "APPROVAL_NOT_REQUIRED"
    APPROVAL_REQUIRED_BY_POLICY = "APPROVAL_REQUIRED_BY_POLICY"
    VALID_APPROVAL_ATTACHED = "VALID_APPROVAL_ATTACHED"
    APPROVAL_REJECTED_BY_OPERATOR = "APPROVAL_REJECTED_BY_OPERATOR"
    APPROVAL_EVIDENCE_EXPIRED = "APPROVAL_EVIDENCE_EXPIRED"
    APPROVAL_EVIDENCE_NOT_FOUND = "APPROVAL_EVIDENCE_NOT_FOUND"
    APPROVAL_EVIDENCE_CORRUPTED = "APPROVAL_EVIDENCE_CORRUPTED"
    ACTION_MISMATCH = "ACTION_MISMATCH"
    RESOURCE_MISMATCH = "RESOURCE_MISMATCH"
    REQUESTER_MISMATCH = "REQUESTER_MISMATCH"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"
    APPROVER_NOT_AUTHENTICATED = "APPROVER_NOT_AUTHENTICATED"
    AUTHORIZATION_NOT_ALLOWED = "AUTHORIZATION_NOT_ALLOWED"
    UNKNOWN_APPROVAL_POLICY = "UNKNOWN_APPROVAL_POLICY"
    EVALUATION_ERROR = "EVALUATION_ERROR"


def compute_approval_checksum(
    approval_id: str,
    target_action: str,
    target_resource: str,
    requesting_identity_id: str,
    approver_identity_id: str,
    policy_name: str,
    policy_version: str,
    status: str,
    approved_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Calcula un checksum SHA-256 canónico y determinista sobre la evidencia de aprobación.
    """
    app_dt_str = approved_at.isoformat() if approved_at else ""
    exp_dt_str = expires_at.isoformat() if expires_at else ""

    meta_dict = dict(metadata) if metadata else {}
    sanitized_meta = sanitize_security_data(meta_dict)
    sorted_meta_json = json.dumps(sanitized_meta, sort_keys=True, default=str)

    payload = (
        f"{approval_id}|{target_action}|{target_resource}|{requesting_identity_id}|"
        f"{approver_identity_id}|{policy_name}|{policy_version}|{status}|"
        f"{app_dt_str}|{exp_dt_str}|{sorted_meta_json}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalPolicy:
    """
    Definición explícita, inmutable y auditable de una política de aprobación.
    """
    policy_name: str
    version: str = "1.0.0"
    actions_not_requiring_approval: Tuple[str, ...] = field(default_factory=tuple)
    actions_requiring_approval: Tuple[str, ...] = field(default_factory=tuple)
    prohibited_actions: Tuple[str, ...] = field(default_factory=tuple)
    require_separation_of_duties: bool = True  # Impide self-approval por defecto
    default_ttl_seconds: int = 3600  # 1 hora por defecto
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.policy_name, "policy_name")
        if not self.version or not isinstance(self.version, str):
            raise ValueError("version must be a non-empty string.")
        if not isinstance(self.actions_not_requiring_approval, tuple):
            object.__setattr__(self, "actions_not_requiring_approval", tuple(self.actions_not_requiring_approval))
        if not isinstance(self.actions_requiring_approval, tuple):
            object.__setattr__(self, "actions_requiring_approval", tuple(self.actions_requiring_approval))
        if not isinstance(self.prohibited_actions, tuple):
            object.__setattr__(self, "prohibited_actions", tuple(self.prohibited_actions))
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))


@dataclass(frozen=True)
class ApprovalEvidence:
    """
    Evidencia criptográficamente verificable e inmutable de una aprobación humana u operativa.
    """
    approval_id: str
    target_action: str
    target_resource: str
    requesting_identity_id: str
    approver_identity_id: str
    policy_name: str
    policy_version: str
    status: ApprovalStatus = ApprovalStatus.APPROVED
    approved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    correlation_id: str = ""
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.approval_id, "approval_id")
        validate_safe_identifier(self.policy_name, "policy_name")
        if not self.target_action or not isinstance(self.target_action, str):
            raise ValueError("target_action must be a non-empty string.")
        if not self.target_resource or not isinstance(self.target_resource, str):
            raise ValueError("target_resource must be a non-empty string.")
        if not self.requesting_identity_id or not isinstance(self.requesting_identity_id, str):
            raise ValueError("requesting_identity_id must be a non-empty string.")
        if not self.approver_identity_id or not isinstance(self.approver_identity_id, str):
            raise ValueError("approver_identity_id must be a non-empty string.")

        if not isinstance(self.status, ApprovalStatus):
            try:
                object.__setattr__(self, "status", ApprovalStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid ApprovalStatus: {self.status}") from e

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        expected_checksum = compute_approval_checksum(
            approval_id=self.approval_id,
            target_action=self.target_action,
            target_resource=self.target_resource,
            requesting_identity_id=self.requesting_identity_id,
            approver_identity_id=self.approver_identity_id,
            policy_name=self.policy_name,
            policy_version=self.policy_version,
            status=self.status.value,
            approved_at=self.approved_at,
            expires_at=self.expires_at,
            metadata=self.metadata,
        )
        if not self.checksum:
            object.__setattr__(self, "checksum", expected_checksum)
        elif self.checksum != expected_checksum:
            raise ValueError("ApprovalEvidence integrity check failed: Checksum mismatch.")

    def is_expired(self, current_time: datetime) -> bool:
        """Verifica si la evidencia ha expirado según el tiempo de referencia."""
        if self.expires_at is None:
            return False
        cur = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        return cur > exp

    def is_valid_for(
        self,
        action: str,
        resource: str,
        requesting_identity_id: str,
        policy_name: str,
        policy_version: str,
        current_time: datetime,
        require_separation_of_duties: bool = True,
    ) -> Tuple[bool, ApprovalReasonCode]:
        """Valida que la evidencia aplique estrictamente al contexto solicitado."""
        if self.status == ApprovalStatus.REJECTED:
            return False, ApprovalReasonCode.APPROVAL_REJECTED_BY_OPERATOR
        if self.status != ApprovalStatus.APPROVED:
            return False, ApprovalReasonCode.APPROVAL_EVIDENCE_NOT_FOUND
        if self.is_expired(current_time):
            return False, ApprovalReasonCode.APPROVAL_EVIDENCE_EXPIRED
        if self.target_action != action:
            return False, ApprovalReasonCode.ACTION_MISMATCH
        if self.target_resource != resource:
            return False, ApprovalReasonCode.RESOURCE_MISMATCH
        if self.requesting_identity_id != requesting_identity_id:
            return False, ApprovalReasonCode.REQUESTER_MISMATCH
        if self.policy_name != policy_name or self.policy_version != policy_version:
            return False, ApprovalReasonCode.POLICY_VERSION_MISMATCH
        if require_separation_of_duties and (self.requesting_identity_id == self.approver_identity_id):
            return False, ApprovalReasonCode.SELF_APPROVAL_FORBIDDEN
        return True, ApprovalReasonCode.VALID_APPROVAL_ATTACHED


@dataclass(frozen=True)
class ApprovalRequest:
    """
    Petición formal e inmutable para evaluar la necesidad de aprobación o verificar evidencia.
    """
    action: str
    resource: str
    requesting_identity_id: str
    policy_name: str
    correlation_id: str = ""
    mission_id: Optional[str] = None
    is_external_impact: bool = False
    is_irreversible: bool = False
    attached_evidence_id: Optional[str] = None
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.action or not isinstance(self.action, str):
            raise ValueError("action must be a non-empty string.")
        if not self.resource or not isinstance(self.resource, str):
            raise ValueError("resource must be a non-empty string.")
        if not self.requesting_identity_id or not isinstance(self.requesting_identity_id, str):
            raise ValueError("requesting_identity_id must be a non-empty string.")
        validate_safe_identifier(self.policy_name, "policy_name")
        sanitized_ctx = sanitize_security_data(dict(self.context))
        object.__setattr__(self, "context", deep_freeze(sanitized_ctx))


@dataclass(frozen=True)
class ApprovalDecision:
    """
    Decisión determinista, auditable e inmutable resultante de la evaluación de políticas de aprobación.
    """
    decision_id: str
    status: ApprovalStatus
    reason_code: ApprovalReasonCode
    reason: str
    policy_name: str
    policy_version: str
    evaluated_at: datetime
    requesting_identity_id: str
    target_action: str
    target_resource: str
    correlation_id: str = ""
    evidence_id: Optional[str] = None
    approver_identity_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.decision_id, "decision_id")
        validate_safe_identifier(self.policy_name, "policy_name")
        if not isinstance(self.status, ApprovalStatus):
            try:
                object.__setattr__(self, "status", ApprovalStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid ApprovalStatus: {self.status}") from e
        if not isinstance(self.reason_code, ApprovalReasonCode):
            try:
                object.__setattr__(self, "reason_code", ApprovalReasonCode(self.reason_code))
            except Exception as e:
                raise ValueError(f"Invalid ApprovalReasonCode: {self.reason_code}") from e

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        raw_payload = (
            f"{self.decision_id}|{self.status.value}|{self.reason_code.value}|{self.reason}|"
            f"{self.policy_name}|{self.policy_version}|{self.evaluated_at.isoformat()}|"
            f"{self.requesting_identity_id}|{self.target_action}|{self.target_resource}|"
            f"{self.evidence_id or ''}|{self.approver_identity_id or ''}|"
            f"{self.expires_at.isoformat() if self.expires_at else ''}"
        )
        expected_checksum = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        if not self.checksum:
            object.__setattr__(self, "checksum", expected_checksum)

    @property
    def is_executable(self) -> bool:
        """Indica si la acción puede proceder según la evaluación de aprobación."""
        return self.status in (ApprovalStatus.NOT_REQUIRED, ApprovalStatus.APPROVED)

    @property
    def approval_reference(self) -> Optional[str]:
        return self.evidence_id
