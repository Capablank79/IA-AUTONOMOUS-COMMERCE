"""
Modelos de dominio para SaaS Authentication y Gestión Multi-Tenant de Sesiones (Hito O.3 — SaaS / Platformization).

Define:
- SessionStatus: ACTIVE, EXPIRED, REVOKED, INVALID, UNKNOWN.
- SessionValidationReasonCode: Taxonomía de códigos de razón de validación.
- SaaSSession: Entidad inmutable de Sesión SaaS acotada a Tenant y opcionalmente Organization.
- SessionContext: Contexto inmutable de ejecución seguro para capas downstream.
- SessionReference: Referencia liviana inmutable a una sesión.
- SessionValidationResult: Resultado determinista de la validación de sesión.
- Excepciones de sesión (SessionError, SessionNotFoundError, SessionValidationError, SessionExpiredError, SessionRevokedError, SessionTenantMismatchError, SessionSecurityViolationError).

Principios O.3:
- "¿Cómo establece y mantiene una sesión SaaS autenticada que además esté vinculada de forma segura a tenant y organization?"
- Reutiliza N.2 Authentication (AuthenticationResult como base de confianza) e Identity N.1.
- Toda sesión SaaS ACTIVE debe estar vinculada a un Tenant válido (O.1).
- Si se especifica Organization, la identidad debe tener membresía ACTIVE en dicha Organization dentro del Tenant (O.2).
- Session ID: Criptográficamente no predecible, seguro (safe string), nunca es el OAuth token, identity_id ni tenant_id.
- Cero almacenamiento de contraseñas, tokens OAuth, refresh tokens ni API keys.
- Inmutabilidad estricta (frozen=True, deep_freeze) y checksums SHA-256 canónicos deterministas.
- Fail-Safe: Sin sesión o sesión inválida/expirada/revocada -> DENY / INVALID. NUNCA default tenant ni acceso silencioso.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import secrets
from types import MappingProxyType
from typing import Mapping, Optional, Any, Dict, Union, Tuple, Sequence

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)


class SessionStatus(str, Enum):
    """Estados canónicos de ciclo de vida de una Sesión SaaS."""
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class SessionValidationReasonCode(str, Enum):
    """Códigos de razón estándar para resultados de validación de sesiones."""
    VALID = "VALID"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_REVOKED = "SESSION_REVOKED"
    INTEGRITY_COMPROMISED = "INTEGRITY_COMPROMISED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    ORGANIZATION_MISMATCH = "ORGANIZATION_MISMATCH"
    MEMBERSHIP_INACTIVE = "MEMBERSHIP_INACTIVE"
    UNAUTHENTICATED_INPUT = "UNAUTHENTICATED_INPUT"
    UNKNOWN_STATUS = "UNKNOWN_STATUS"


class SessionError(Exception):
    """Excepción base para errores relacionados con sesiones SaaS."""
    pass


class SessionNotFoundError(SessionError):
    """Lanzada cuando una sesión solicitada no existe."""
    pass


class SessionValidationError(SessionError):
    """Lanzada cuando una sesión no supera las validaciones de seguridad o estado."""
    pass


class SessionExpiredError(SessionValidationError):
    """Lanzada cuando se intenta operar con una sesión expirada."""
    pass


class SessionRevokedError(SessionValidationError):
    """Lanzada cuando se intenta operar con una sesión revocada."""
    pass


class SessionTenantMismatchError(SessionValidationError):
    """Lanzada cuando el tenant de la sesión no coincide con el tenant solicitado u objetivo."""
    pass


class SessionSecurityViolationError(SessionError):
    """Lanzada ante violaciones de seguridad críticas en la gestión de sesiones."""
    pass


def generate_secure_session_id(prefix: str = "sess_") -> str:
    """
    Genera un identificador de sesión criptográficamente no predecible y seguro.
    No deriva de tokens OAuth, ni identity_id, ni tenant_id.
    """
    random_hex = secrets.token_hex(24)
    session_id = f"{prefix}{random_hex}"
    validate_safe_identifier(session_id, field_name="session_id")
    return session_id


def compute_session_checksum(
    session_id: str,
    identity_id: str,
    tenant_id: str,
    organization_id: Optional[str],
    authentication_method: str,
    authentication_provider: str,
    created_at: datetime,
    expires_at: datetime,
    status: Union[SessionStatus, str],
    schema_version: str,
    metadata: Mapping[str, Any],
) -> str:
    """Calcula un checksum SHA-256 canónico y determinista para una SaaSSession."""
    status_val = status.value if isinstance(status, SessionStatus) else str(status)
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "session_id": session_id,
        "identity_id": identity_id,
        "tenant_id": tenant_id,
        "organization_id": organization_id,
        "authentication_method": authentication_method,
        "authentication_provider": authentication_provider,
        "created_at": created_at.isoformat() if created_at else "",
        "expires_at": expires_at.isoformat() if expires_at else "",
        "status": status_val,
        "schema_version": schema_version,
        "metadata": sanitized_meta,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_session_context_checksum(
    session_id: str,
    identity_id: str,
    tenant_id: str,
    organization_id: Optional[str],
    authentication_method: str,
    authentication_provider: str,
    status: Union[SessionStatus, str],
    correlation_id: Optional[str],
    metadata: Mapping[str, Any],
) -> str:
    """Calcula un checksum SHA-256 canónico y determinista para un SessionContext."""
    status_val = status.value if isinstance(status, SessionStatus) else str(status)
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "session_id": session_id,
        "identity_id": identity_id,
        "tenant_id": tenant_id,
        "organization_id": organization_id,
        "authentication_method": authentication_method,
        "authentication_provider": authentication_provider,
        "status": status_val,
        "correlation_id": correlation_id,
        "metadata": sanitized_meta,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionReference:
    """Referencia liviana e inmutable a una sesión SaaS."""
    session_id: str
    identity_id: str
    tenant_id: str
    organization_id: Optional[str] = None
    status: SessionStatus = SessionStatus.ACTIVE
    expires_at: Optional[datetime] = None

    def __post_init__(self):
        validate_safe_identifier(self.session_id, field_name="session_id")
        validate_safe_identifier(self.identity_id, field_name="identity_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.organization_id is not None:
            validate_safe_identifier(self.organization_id, field_name="organization_id")
        if not isinstance(self.status, SessionStatus):
            try:
                object.__setattr__(self, "status", SessionStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid SessionStatus: {self.status}") from e

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "identity_id": self.identity_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "status": self.status.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True)
class SessionContext:
    """
    Contexto de sesión inmutable y seguro para el tránsito downstream.
    Permite construir de forma determinista el TenantContext (O.1) y enlazarlo con RBAC (N.4) y Autorización (N.3).

    ESTRICTAMENTE SIN SECRETOS:
    Cero passwords, tokens, llaves API, refresh tokens o CoT.
    """
    session_id: str
    identity_id: str
    tenant_id: str
    organization_id: Optional[str] = None
    authentication_method: str = "UNKNOWN"
    authentication_provider: str = "unknown"
    status: SessionStatus = SessionStatus.ACTIVE
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(init=False)

    def __post_init__(self):
        validate_safe_identifier(self.session_id, field_name="session_id")
        validate_safe_identifier(self.identity_id, field_name="identity_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.organization_id is not None:
            validate_safe_identifier(self.organization_id, field_name="organization_id")

        if not isinstance(self.status, SessionStatus):
            try:
                object.__setattr__(self, "status", SessionStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid SessionStatus: {self.status}") from e

        if self.correlation_id is not None:
            norm_cid = str(self.correlation_id).strip()
            if norm_cid:
                validate_safe_identifier(norm_cid, field_name="correlation_id")
                object.__setattr__(self, "correlation_id", norm_cid)
            else:
                object.__setattr__(self, "correlation_id", None)

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        chk = compute_session_context_checksum(
            session_id=self.session_id,
            identity_id=self.identity_id,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            authentication_method=self.authentication_method,
            authentication_provider=self.authentication_provider,
            status=self.status,
            correlation_id=self.correlation_id,
            metadata=self.metadata,
        )
        object.__setattr__(self, "checksum", chk)

    @property
    def is_active(self) -> bool:
        return self.status == SessionStatus.ACTIVE

    def to_tenant_context(self) -> TenantContext:
        """
        Crea un TenantContext formal e inmutable (Hito O.1) a partir de este SessionContext.
        """
        extra_meta = dict(self.metadata)
        extra_meta["session_id"] = self.session_id
        if self.organization_id:
            extra_meta["organization_id"] = self.organization_id
        extra_meta["authentication_provider"] = self.authentication_provider
        extra_meta["authentication_method"] = self.authentication_method

        return TenantContext(
            tenant_id=self.tenant_id,
            identity_id=self.identity_id,
            correlation_id=self.correlation_id,
            metadata=extra_meta,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "identity_id": self.identity_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "authentication_method": self.authentication_method,
            "authentication_provider": self.authentication_provider,
            "status": self.status.value,
            "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SessionContext":
        return cls(
            session_id=data["session_id"],
            identity_id=data["identity_id"],
            tenant_id=data["tenant_id"],
            organization_id=data.get("organization_id"),
            authentication_method=data.get("authentication_method", "UNKNOWN"),
            authentication_provider=data.get("authentication_provider", "unknown"),
            status=SessionStatus(data.get("status", "ACTIVE")),
            correlation_id=data.get("correlation_id"),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class SaaSSession:
    """
    Entidad de dominio inmutable para una Sesión SaaS Multi-Tenant (O.3).

    Atributos:
    - session_id: ID criptográficamente seguro, no predecible.
    - identity_id: Identidad canónica del actor autenticado (N.1).
    - tenant_id: Tenant al que está vinculada la sesión (O.1).
    - organization_id: Organización opcional dentro del tenant (O.2).
    - authentication_method: Método de autenticación con el que se originó (N.2).
    - authentication_provider: Proveedor de autenticación (N.2).
    - created_at: Timestamp UTC timezone-aware de creación.
    - expires_at: Timestamp UTC timezone-aware de expiración determinista.
    - last_validated_at: Timestamp opcional de última validación.
    - status: SessionStatus (ACTIVE, EXPIRED, REVOKED, INVALID, UNKNOWN).
    - schema_version: Versión del esquema ("1.0.0").
    - checksum: Checksum SHA-256 determinista de integridad.
    - metadata: Metadatos sanitizados inmutables.
    """
    session_id: str
    identity_id: str
    tenant_id: str
    created_at: datetime
    expires_at: datetime
    organization_id: Optional[str] = None
    authentication_method: str = "UNKNOWN"
    authentication_provider: str = "unknown"
    last_validated_at: Optional[datetime] = None
    status: SessionStatus = SessionStatus.ACTIVE
    schema_version: str = "1.0.0"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.session_id, field_name="session_id")
        validate_safe_identifier(self.identity_id, field_name="identity_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.organization_id is not None:
            validate_safe_identifier(self.organization_id, field_name="organization_id")

        if not isinstance(self.status, SessionStatus):
            try:
                object.__setattr__(self, "status", SessionStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid SessionStatus: {self.status}") from e

        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware (UTC)")
        if self.last_validated_at is not None and self.last_validated_at.tzinfo is None:
            raise ValueError("last_validated_at must be timezone-aware (UTC)")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        expected_chk = compute_session_checksum(
            session_id=self.session_id,
            identity_id=self.identity_id,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            authentication_method=self.authentication_method,
            authentication_provider=self.authentication_provider,
            created_at=self.created_at,
            expires_at=self.expires_at,
            status=self.status,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )

        if not self.checksum:
            object.__setattr__(self, "checksum", expected_chk)
        elif self.checksum != expected_chk:
            raise ValueError(
                f"Checksum mismatch for SaaSSession '{self.session_id}': "
                f"provided '{self.checksum}' != expected '{expected_chk}'"
            )

    @property
    def is_active(self) -> bool:
        return self.status == SessionStatus.ACTIVE

    def is_expired(self, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now >= self.expires_at

    def to_reference(self) -> SessionReference:
        return SessionReference(
            session_id=self.session_id,
            identity_id=self.identity_id,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            status=self.status,
            expires_at=self.expires_at,
        )

    def to_context(self, correlation_id: Optional[str] = None) -> SessionContext:
        return SessionContext(
            session_id=self.session_id,
            identity_id=self.identity_id,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            authentication_method=self.authentication_method,
            authentication_provider=self.authentication_provider,
            status=self.status,
            correlation_id=correlation_id,
            metadata=self.metadata,
        )

    def with_status(
        self,
        new_status: SessionStatus,
        last_validated_at: Optional[datetime] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "SaaSSession":
        """Crea una nueva instancia inmutable con un estado actualizado y checksum recalculado."""
        new_meta = dict(metadata) if metadata is not None else dict(self.metadata)
        return SaaSSession(
            session_id=self.session_id,
            identity_id=self.identity_id,
            tenant_id=self.tenant_id,
            organization_id=self.organization_id,
            authentication_method=self.authentication_method,
            authentication_provider=self.authentication_provider,
            created_at=self.created_at,
            expires_at=self.expires_at,
            last_validated_at=last_validated_at or self.last_validated_at,
            status=new_status,
            schema_version=self.schema_version,
            checksum="",  # Auto-recomputes in __post_init__
            metadata=new_meta,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "identity_id": self.identity_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "authentication_method": self.authentication_method,
            "authentication_provider": self.authentication_provider,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "last_validated_at": self.last_validated_at.isoformat() if self.last_validated_at else None,
            "status": self.status.value,
            "schema_version": self.schema_version,
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SaaSSession":
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        last_validated_at = None
        if data.get("last_validated_at"):
            last_validated_at = datetime.fromisoformat(data["last_validated_at"])
            if last_validated_at.tzinfo is None:
                last_validated_at = last_validated_at.replace(tzinfo=timezone.utc)

        return cls(
            session_id=data["session_id"],
            identity_id=data["identity_id"],
            tenant_id=data["tenant_id"],
            organization_id=data.get("organization_id"),
            authentication_method=data.get("authentication_method", "UNKNOWN"),
            authentication_provider=data.get("authentication_provider", "unknown"),
            created_at=created_at,
            expires_at=expires_at,
            last_validated_at=last_validated_at,
            status=SessionStatus(data.get("status", "ACTIVE")),
            schema_version=data.get("schema_version", "1.0.0"),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class SessionValidationResult:
    """
    Resultado explícito, determinista e inmutable de la validación de una sesión SaaS.
    """
    is_valid: bool
    status: SessionStatus
    session: Optional[SaaSSession] = None
    session_context: Optional[SessionContext] = None
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    validated_at: Optional[datetime] = None
    correlation_id: Optional[str] = None
    checksum: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.status, SessionStatus):
            try:
                object.__setattr__(self, "status", SessionStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid SessionStatus: {self.status}") from e

        if self.correlation_id is not None:
            norm_cid = str(self.correlation_id).strip()
            if norm_cid:
                validate_safe_identifier(norm_cid, field_name="correlation_id")
                object.__setattr__(self, "correlation_id", norm_cid)
            else:
                object.__setattr__(self, "correlation_id", None)

        if self.reason_codes:
            clean_reasons = tuple(str(r).strip() for r in self.reason_codes if str(r).strip())
            object.__setattr__(self, "reason_codes", clean_reasons)
        else:
            object.__setattr__(self, "reason_codes", tuple())

        payload = {
            "is_valid": self.is_valid,
            "status": self.status.value,
            "session_id": self.session.session_id if self.session else "",
            "reason_codes": sorted(list(self.reason_codes)),
            "validated_at": self.validated_at.isoformat() if self.validated_at else "",
            "correlation_id": self.correlation_id or "",
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        object.__setattr__(self, "checksum", hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    @property
    def session_id(self) -> Optional[str]:
        return self.session.session_id if self.session else None

    @property
    def tenant_id(self) -> Optional[str]:
        return self.session.tenant_id if self.session else None

    @property
    def identity_id(self) -> Optional[str]:
        return self.session.identity_id if self.session else None
