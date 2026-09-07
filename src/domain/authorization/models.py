"""
Modelos de dominio para la Autorización Explícita y Determinista (Hito N.3).

Define:
- AuthorizationStatus: ALLOW, DENY, UNKNOWN, ERROR.
- AuthorizationReasonCode: Códigos de razón canónicos y seguros.
- ResourceReference: Referencia canónica inmutable al recurso objetivo.
- ActionReference: Referencia estructurada a la acción solicitada.
- AuthorizationRequest: Petición inmutable de autorización con contexto de autenticación y comercial.
- AuthorizationDecision: Decisión inmutable, determinista y auditable con checksum SHA-256.

Principios:
- N.3 responde: "¿Esta identidad autenticada puede realizar esta acción sobre este recurso/contexto?".
- Decisión explícita: ALLOW, DENY, UNKNOWN, ERROR.
- Autenticación exitosa != Autorización automática.
- Default DENY: Si no hay regla o política suficiente -> UNKNOWN / DENY (NUNCA default ALLOW).
- Precondición estricta: UNAUTHENTICATED / EXPIRED / INVALID / UNKNOWN auth -> NO ALLOW.
- Cero almacenamiento o propagación de secretos, tokens o passwords (sanitización integral K.8).
- Determinismo e inmutabilidad frozen.
- No implementa RBAC (N.4) ni Approval Policies (Gate M).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Union, Sequence
import uuid

from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.authentication.models import PrincipalContext, AuthenticationStatus
from src.domain.security.models import sanitize_security_data, deep_freeze


class AuthorizationStatus(str, Enum):
    """
    Decisión canónica y explícita de autorización (N.3).
    """
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class AuthorizationReasonCode(str, Enum):
    """
    Códigos de razón canónicos, estables y seguros para auditoría y trazabilidad.
    """
    AUTHORIZED_BY_POLICY = "AUTHORIZED_BY_POLICY"
    UNAUTHENTICATED_PRINCIPAL = "UNAUTHENTICATED_PRINCIPAL"
    EXPIRED_AUTHENTICATION = "EXPIRED_AUTHENTICATION"
    INVALID_AUTHENTICATION = "INVALID_AUTHENTICATION"
    UNKNOWN_AUTHENTICATION = "UNKNOWN_AUTHENTICATION"
    MISSING_PRINCIPAL_CONTEXT = "MISSING_PRINCIPAL_CONTEXT"
    ACTION_PROHIBITED = "ACTION_PROHIBITED"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    RESOURCE_MISMATCH = "RESOURCE_MISMATCH"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    POLICY_DENIED = "POLICY_DENIED"
    POLICY_EVALUATION_ERROR = "POLICY_EVALUATION_ERROR"
    HIGH_IMPACT_UNAUTHORIZED = "HIGH_IMPACT_UNAUTHORIZED"
    DEFAULT_DENY = "DEFAULT_DENY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class ResourceReference:
    """
    Referencia inmutable al recurso objetivo sobre el cual se solicita la acción.
    """
    resource_type: str
    resource_id: str
    provider: Optional[str] = None
    account_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.resource_type or not isinstance(self.resource_type, str):
            raise ValueError("resource_type must be a non-empty string")
        if not self.resource_id or not isinstance(self.resource_id, str):
            raise ValueError("resource_id must be a non-empty string")
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    @property
    def canonical_resource_id(self) -> str:
        """Construye un identificador canónico del recurso."""
        prov = self.provider.lower() if self.provider else "global"
        return f"{prov}:{self.resource_type.lower()}:{self.resource_id}"


@dataclass(frozen=True)
class ActionReference:
    """
    Referencia estructurada a una acción comercial u operacional del sistema.
    """
    action_type: str
    category: str = "COMMERCIAL"
    is_external_impact: bool = False
    is_financial: bool = False

    def __post_init__(self):
        if not self.action_type or not isinstance(self.action_type, str):
            raise ValueError("action_type must be a non-empty string")
        clean_action = self.action_type.strip().upper()
        object.__setattr__(self, "action_type", clean_action)


@dataclass(frozen=True)
class AuthorizationRequest:
    """
    Petición inmutable de autorización (Hito N.3).
    Contiene la identidad autenticada, la acción solicitada, el recurso objetivo,
    el contexto comercial/de seguridad y los identificadores de correlación.
    """
    action: str
    principal_context: Optional[PrincipalContext] = None
    identity_id: Optional[str] = None
    resource: Optional[Union[str, ResourceReference]] = None
    commercial_context: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    policy_version: str = "1.0.0"

    def __post_init__(self):
        if not self.action or not isinstance(self.action, str):
            raise ValueError("action must be a non-empty string")
        clean_action = self.action.strip().upper()
        object.__setattr__(self, "action", clean_action)

        # Resolver identity_id desde principal_context si no se proveyó
        if not self.identity_id and self.principal_context is not None:
            object.__setattr__(self, "identity_id", self.principal_context.identity_id)
        elif not self.identity_id:
            object.__setattr__(self, "identity_id", "unauthenticated")

        if not self.correlation_id or not isinstance(self.correlation_id, str):
            object.__setattr__(self, "correlation_id", str(uuid.uuid4()))

        sanitized_context = sanitize_security_data(dict(self.commercial_context))
        object.__setattr__(self, "commercial_context", deep_freeze(sanitized_context))

    @property
    def target_resource_str(self) -> Optional[str]:
        if self.resource is None:
            return None
        if isinstance(self.resource, ResourceReference):
            return self.resource.canonical_resource_id
        return str(self.resource)


def compute_authorization_checksum(
    status: Union[AuthorizationStatus, str],
    identity_id: str,
    action: str,
    resource: Optional[str],
    matched_policy: Optional[str],
    policy_version: str,
    reason_codes: Sequence[str],
    correlation_id: str,
) -> str:
    """
    Calcula un checksum criptográfico determinista SHA-256 para una decisión de autorización.
    """
    status_str = status.value if isinstance(status, AuthorizationStatus) else str(status)
    payload = {
        "status": status_str,
        "identity_id": identity_id,
        "action": action,
        "resource": resource or "",
        "matched_policy": matched_policy or "",
        "policy_version": policy_version,
        "reason_codes": sorted(list(reason_codes)),
        "correlation_id": correlation_id,
    }
    canonical_json = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthorizationDecision:
    """
    Decisión inmutable, estructurada y determinista de autorización (Hito N.3).
    """
    decision_id: str
    status: AuthorizationStatus
    identity_id: str
    action: str
    resource: Optional[str] = None
    matched_policy: Optional[str] = None
    policy_version: str = "1.0.0"
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        if not self.decision_id or not isinstance(self.decision_id, str):
            raise ValueError("decision_id must be a non-empty string")
        if not isinstance(self.status, AuthorizationStatus):
            try:
                object.__setattr__(self, "status", AuthorizationStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e

        if not self.identity_id or not isinstance(self.identity_id, str):
            raise ValueError("identity_id must be a non-empty string")
        if not self.action or not isinstance(self.action, str):
            raise ValueError("action must be a non-empty string")

        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC)")

        if not isinstance(self.reason_codes, tuple):
            object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        # Checksum determinista
        if not self.checksum:
            digest = compute_authorization_checksum(
                status=self.status,
                identity_id=self.identity_id,
                action=self.action,
                resource=self.resource,
                matched_policy=self.matched_policy,
                policy_version=self.policy_version,
                reason_codes=self.reason_codes,
                correlation_id=self.correlation_id,
            )
            object.__setattr__(self, "checksum", digest)

    @property
    def is_allowed(self) -> bool:
        """Garantiza que sólo ALLOW devuelve True. DENY, UNKNOWN y ERROR son False."""
        return self.status == AuthorizationStatus.ALLOW

    @property
    def is_denied(self) -> bool:
        return self.status == AuthorizationStatus.DENY

    @property
    def is_unknown(self) -> bool:
        return self.status == AuthorizationStatus.UNKNOWN

    @property
    def is_error(self) -> bool:
        return self.status == AuthorizationStatus.ERROR
