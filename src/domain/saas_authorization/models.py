"""
Modelos de dominio para SaaS Authorization y Multi-Tenant RBAC & Scoping (Hito O.4 — SaaS / Platformization).

Define:
- SaaSAuthorizationStatus: ALLOW, DENY, UNKNOWN, ERROR.
- SaaSAuthorizationReasonCode: Códigos de razón estables y seguros para auditoría.
- SaaSAuthorizationRequest: Petición inmutable de autorización multi-tenant con contexto de sesión SaaS.
- SaaSAuthorizationContext: Contexto inmutable de evaluación de autorización con resolved permissions y tenant scope.
- SaaSAuthorizationDecision: Decisión inmutable, determinista y auditable con checksum SHA-256.
- Excepciones: SaaSAuthorizationError, SaaSAuthorizationDeniedError, SaaSAuthorizationSecurityViolationError.

Principios O.4:
- O.4 responde: "Dada una sesión SaaS activa, ¿puede esta identidad ejecutar esta acción dentro de este tenant y organization concretos?".
- Construye el pipeline de seguridad:
  SaaSSession (O.3) -> TenantContext/CrossTenantGuard (O.1) -> Organization Membership (O.2) -> Effective RBAC (N.4) -> N.3 Authorization -> SaaS Authorization Decision.
- O.4 NO reemplaza N.3 ni N.4; orquesta el scoping multi-tenant y la validación en tiempo real.
- Session nunca almacena permisos como autoridad persistente (evita stale permissions snapshot).
- Role revocado o membership removida/suspendida toma efecto inmediato sin requerir relogin o destruir la sesión.
- Default Deny estricto: sin sesión válida, tenant mismatch, membership inactiva o permiso faltante -> DENY.
- Inmutabilidad estricta (frozen=True, deep_freeze) y checksums criptográficos SHA-256 deterministas.
- Cero secretos, tokens OAuth ni PII innecesaria en contextos, decisiones y auditoría.
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
from src.domain.session.models import SaaSSession, SessionContext, SessionStatus
from src.domain.tenant.models import TenantContext, TenantScope, TenantScopedResource
from src.domain.organization.models import UserMembership, MembershipStatus
from src.domain.rbac.models import PermissionSet, normalize_action_token
from src.domain.authorization.models import (
    AuthorizationDecision,
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
)
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class SaaSAuthorizationStatus(str, Enum):
    """
    Decisión canónica y explícita de autorización SaaS multi-tenant (O.4).
    """
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class SaaSAuthorizationReasonCode(str, Enum):
    """
    Códigos de razón canónicos y seguros para trazabilidad y auditoría de autorización SaaS.
    """
    AUTHORIZED = "AUTHORIZED"
    SESSION_INVALID = "SESSION_INVALID"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_REVOKED = "SESSION_REVOKED"
    SESSION_TENANT_MISMATCH = "SESSION_TENANT_MISMATCH"
    ORGANIZATION_MISMATCH = "ORGANIZATION_MISMATCH"
    MEMBERSHIP_NOT_FOUND = "MEMBERSHIP_NOT_FOUND"
    MEMBERSHIP_NOT_ACTIVE = "MEMBERSHIP_NOT_ACTIVE"
    RESOURCE_TENANT_MISMATCH = "RESOURCE_TENANT_MISMATCH"
    MARKETPLACE_ACCOUNT_MISMATCH = "MARKETPLACE_ACCOUNT_MISMATCH"
    INSUFFICIENT_PERMISSIONS = "INSUFFICIENT_PERMISSIONS"
    POLICY_DENIED = "POLICY_DENIED"
    UNKNOWN_RESOURCE_OWNERSHIP = "UNKNOWN_RESOURCE_OWNERSHIP"
    MISSING_IDENTITY = "MISSING_IDENTITY"
    MISSING_TENANT = "MISSING_TENANT"
    DEFAULT_DENY = "DEFAULT_DENY"
    EVALUATION_ERROR = "EVALUATION_ERROR"


class SaaSAuthorizationError(Exception):
    """Excepción base para errores de autorización SaaS multi-tenant."""
    pass


class SaaSAuthorizationDeniedError(SaaSAuthorizationError):
    """Excepción lanzada cuando una operación no autorizada es bloqueada por el ejecutor guardián."""
    pass


class SaaSAuthorizationSecurityViolationError(SaaSAuthorizationError):
    """Excepción lanzada ante intentos de violación de aislamiento multi-tenant o manipulación de seguridad."""
    pass


@dataclass(frozen=True)
class SaaSAuthorizationRequest:
    """
    Petición inmutable de autorización multi-tenant para el Hito O.4.

    Transporta:
    - action: La acción solicitada (e.g. 'LISTING_PUBLISH', 'PRICE_UPDATE').
    - session_id: Identificador de la sesión SaaS activa (O.3).
    - session_context: Opcionalmente el SessionContext pre-validado.
    - session: Opcionalmente la SaaSSession completa.
    - tenant_id: Tenant objetivo solicitado (opcional si se infiere de session, validado contra session).
    - organization_id: Organización objetivo opcional (validada contra sesión y membresía).
    - identity_id: Identidad opcional (debe coincidir con la de la sesión).
    - resource: Referencia al recurso (str, ResourceReference, TenantScopedResource o dict).
    - resource_tenant_id: Tenant propietario del recurso si se conoce a priori.
    - resource_organization_id: Organización propietaria del recurso si aplica.
    - marketplace_account_id: Cuenta de marketplace asociada si aplica.
    - commercial_context: Contexto comercial sanitizado adicional.
    - correlation_id: ID único de correlación para trazas y auditoría.
    - policy_version: Versión de la política de autorización.
    """
    action: str
    session_id: Optional[str] = None
    session_context: Optional[SessionContext] = None
    session: Optional[SaaSSession] = None
    tenant_id: Optional[str] = None
    organization_id: Optional[str] = None
    identity_id: Optional[str] = None
    resource: Optional[Union[str, ResourceReference, TenantScopedResource, Mapping[str, Any]]] = None
    resource_tenant_id: Optional[str] = None
    resource_organization_id: Optional[str] = None
    marketplace_account_id: Optional[str] = None
    commercial_context: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: f"saas_authz_req_{uuid.uuid4().hex[:12]}")
    policy_version: str = "1.0.0"

    def __post_init__(self):
        if not self.action or not isinstance(self.action, str) or not self.action.strip():
            raise ValueError("action must be a non-empty string.")

        # Safe identifier checks
        if self.session_id is not None:
            validate_safe_identifier(self.session_id, field_name="session_id")
        if self.tenant_id is not None:
            validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.organization_id is not None:
            validate_safe_identifier(self.organization_id, field_name="organization_id")
        if self.identity_id is not None:
            validate_safe_identifier(self.identity_id, field_name="identity_id")
        if self.resource_tenant_id is not None:
            validate_safe_identifier(self.resource_tenant_id, field_name="resource_tenant_id")
        if self.resource_organization_id is not None:
            validate_safe_identifier(self.resource_organization_id, field_name="resource_organization_id")
        if self.marketplace_account_id is not None:
            validate_safe_identifier(self.marketplace_account_id, field_name="marketplace_account_id")

        if not self.correlation_id or not isinstance(self.correlation_id, str):
            object.__setattr__(self, "correlation_id", f"saas_authz_req_{uuid.uuid4().hex[:12]}")

        sanitized_commercial = sanitize_security_data(dict(self.commercial_context))
        frozen_commercial = deep_freeze(sanitized_commercial)
        object.__setattr__(self, "commercial_context", frozen_commercial)


def compute_saas_authorization_context_checksum(
    session_id: str,
    identity_id: str,
    tenant_id: str,
    organization_id: Optional[str],
    action: str,
    resource: Optional[str],
    resource_tenant_id: Optional[str],
    resource_organization_id: Optional[str],
    resolved_permissions: Sequence[str],
    resolved_roles: Sequence[str],
    n3_decision_reference: Optional[str],
    policy_version: str,
    correlation_id: str,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula un checksum SHA-256 canónico y determinista para el contexto de autorización SaaS."""
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "session_id": session_id,
        "identity_id": identity_id,
        "tenant_id": tenant_id,
        "organization_id": organization_id,
        "action": action,
        "resource": resource,
        "resource_tenant_id": resource_tenant_id,
        "resource_organization_id": resource_organization_id,
        "resolved_permissions": sorted(list(resolved_permissions)),
        "resolved_roles": sorted(list(resolved_roles)),
        "n3_decision_reference": n3_decision_reference,
        "policy_version": policy_version,
        "correlation_id": correlation_id,
        "metadata": sanitized_meta,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SaaSAuthorizationContext:
    """
    Contexto inmutable resultante de la resolución de seguridad multi-tenant de O.4.

    Contiene:
    - session_id: Identificador de la sesión SaaS activa.
    - identity_id: Identificador de la identidad.
    - tenant_id: Tenant canónico de la sesión y ámbito de evaluación.
    - organization_id: Organización canónica si aplica.
    - action: Acción solicitada.
    - resource: Representación canónica del recurso si existe.
    - resource_tenant_id: Tenant propietario verificado del recurso.
    - resource_organization_id: Organización propietaria del recurso si aplica.
    - resolved_permissions: Lista inmutable de permisos efectivos resueltos dinámicamente vía RBAC N.4.
    - resolved_roles: Lista inmutable de roles efectivos asignados a la identidad en este scope.
    - n3_decision_reference: Referencia a la decisión tomada por N.3 PolicyEngine.
    - policy_version: Versión de la política.
    - correlation_id: ID de correlación.
    - metadata: Metadatos sanitizados inmutables.
    - checksum: Checksum criptográfico SHA-256.
    """
    session_id: str
    identity_id: str
    tenant_id: str
    action: str
    organization_id: Optional[str] = None
    resource: Optional[str] = None
    resource_tenant_id: Optional[str] = None
    resource_organization_id: Optional[str] = None
    resolved_permissions: Tuple[str, ...] = field(default_factory=tuple)
    resolved_roles: Tuple[str, ...] = field(default_factory=tuple)
    n3_decision_reference: Optional[str] = None
    policy_version: str = "1.0.0"
    correlation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(init=False)

    def __post_init__(self):
        validate_safe_identifier(self.session_id, field_name="session_id")
        validate_safe_identifier(self.identity_id, field_name="identity_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.organization_id is not None:
            validate_safe_identifier(self.organization_id, field_name="organization_id")
        if self.resource_tenant_id is not None:
            validate_safe_identifier(self.resource_tenant_id, field_name="resource_tenant_id")
        if self.resource_organization_id is not None:
            validate_safe_identifier(self.resource_organization_id, field_name="resource_organization_id")

        if not isinstance(self.resolved_permissions, tuple):
            object.__setattr__(self, "resolved_permissions", tuple(sorted(self.resolved_permissions)))
        if not isinstance(self.resolved_roles, tuple):
            object.__setattr__(self, "resolved_roles", tuple(sorted(self.resolved_roles)))

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        digest = compute_saas_authorization_context_checksum(
            session_id=self.session_id,
            identity_id=self.identity_id,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            action=self.action,
            resource=self.resource,
            resource_tenant_id=self.resource_tenant_id,
            resource_organization_id=self.resource_organization_id,
            resolved_permissions=self.resolved_permissions,
            resolved_roles=self.resolved_roles,
            n3_decision_reference=self.n3_decision_reference,
            policy_version=self.policy_version,
            correlation_id=self.correlation_id,
            metadata=self.metadata,
        )
        object.__setattr__(self, "checksum", digest)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el contexto a un diccionario sanitizado."""
        return {
            "session_id": self.session_id,
            "identity_id": self.identity_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "action": self.action,
            "resource": self.resource,
            "resource_tenant_id": self.resource_tenant_id,
            "resource_organization_id": self.resource_organization_id,
            "resolved_permissions": list(self.resolved_permissions),
            "resolved_roles": list(self.resolved_roles),
            "n3_decision_reference": self.n3_decision_reference,
            "policy_version": self.policy_version,
            "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
            "checksum": self.checksum,
        }


def compute_saas_authorization_decision_checksum(
    decision_id: str,
    status: Union[SaaSAuthorizationStatus, str],
    reason_code: Union[SaaSAuthorizationReasonCode, str],
    context_checksum: str,
    evaluated_at: datetime,
    policy_version: str,
) -> str:
    """Calcula un checksum SHA-256 canónico y determinista para una decisión de autorización SaaS."""
    status_val = status.value if isinstance(status, SaaSAuthorizationStatus) else str(status)
    reason_val = reason_code.value if isinstance(reason_code, SaaSAuthorizationReasonCode) else str(reason_code)
    payload = {
        "decision_id": decision_id,
        "status": status_val,
        "reason_code": reason_val,
        "context_checksum": context_checksum,
        "evaluated_at": evaluated_at.isoformat() if evaluated_at else "",
        "policy_version": policy_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SaaSAuthorizationDecision:
    """
    Decisión inmutable y determinista de autorización SaaS multi-tenant (Hito O.4).

    Atributos:
    - decision_id: Identificador único de la decisión.
    - status: ALLOW, DENY, UNKNOWN, ERROR.
    - reason_code: Código de razón canónico y auditable.
    - context: Contexto completo inmutable con resolución de tenant, membresía y permisos.
    - n3_decision: Decisión N.3 subyacente (si fue evaluada).
    - message: Mensaje explicativo sanitizado.
    - evaluated_at: Timestamp UTC de evaluación.
    - policy_version: Versión de la política aplicada.
    - checksum: Checksum criptográfico SHA-256 determinista.
    """
    decision_id: str
    status: SaaSAuthorizationStatus
    reason_code: SaaSAuthorizationReasonCode
    context: SaaSAuthorizationContext
    n3_decision: Optional[AuthorizationDecision] = None
    message: Optional[str] = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    policy_version: str = "1.0.0"
    checksum: str = field(init=False)

    def __post_init__(self):
        validate_safe_identifier(self.decision_id, field_name="decision_id")

        if not isinstance(self.status, SaaSAuthorizationStatus):
            try:
                object.__setattr__(self, "status", SaaSAuthorizationStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid SaaSAuthorizationStatus: {self.status}") from e

        if not isinstance(self.reason_code, SaaSAuthorizationReasonCode):
            try:
                object.__setattr__(self, "reason_code", SaaSAuthorizationReasonCode(self.reason_code))
            except Exception as e:
                raise ValueError(f"Invalid SaaSAuthorizationReasonCode: {self.reason_code}") from e

        if not isinstance(self.context, SaaSAuthorizationContext):
            raise ValueError("context must be an instance of SaaSAuthorizationContext.")

        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC)")

        digest = compute_saas_authorization_decision_checksum(
            decision_id=self.decision_id,
            status=self.status,
            reason_code=self.reason_code,
            context_checksum=self.context.checksum,
            evaluated_at=self.evaluated_at,
            policy_version=self.policy_version,
        )
        object.__setattr__(self, "checksum", digest)

    @property
    def is_allowed(self) -> bool:
        """Indica si la acción está permitida en el tenant y sesión actuales."""
        return self.status == SaaSAuthorizationStatus.ALLOW

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la decisión a un diccionario sanitizado."""
        return {
            "decision_id": self.decision_id,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "context": self.context.to_dict(),
            "n3_decision": self.n3_decision.to_dict() if self.n3_decision else None,
            "message": self.message,
            "evaluated_at": self.evaluated_at.isoformat(),
            "policy_version": self.policy_version,
            "checksum": self.checksum,
        }
