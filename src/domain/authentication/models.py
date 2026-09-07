"""
Modelos de dominio para Autenticación (Hito N.2 - Transversal N Security, Governance y Safety).

Define:
- AuthenticationMethod: Métodos canónicos de autenticación basados en capacidades reales del repo.
  (OAUTH2, BEARER_TOKEN, API_CREDENTIAL, INTERNAL_SERVICE, SYSTEM_ASSERTION, UNKNOWN)
- AuthenticationStatus: Estados canónicos de resultado de autenticación.
  (AUTHENTICATED, UNAUTHENTICATED, EXPIRED, INVALID, UNKNOWN, ERROR)
- AuthenticationRequest: Solicitud inmutable de autenticación (portadora de contexto/credencial efímera).
- AuthenticationResult: Resultado explícito, determinista e inmutable de autenticación.
  Contiene IdentityReference/principal N.1, método, proveedor, estado, timestamp, expiración,
  reason codes, correlation_id y metadata sanitizada. CERO secretos.
- PrincipalContext: Contexto seguro downstream desacoplado de credenciales.

Principios N.2:
- N.2 responde exclusivamente a: "¿Puede este actor demostrar de forma válida que es la identidad que declara?".
- N.1: quién es. N.2: cómo demuestra quién es. N.3: qué puede hacer (NO mezclar con autorización).
- Cero almacenamiento o exposición de secretos (access_token, refresh_token, passwords, API keys, etc.).
- Preservación estricta de UNKNOWN (UNKNOWN != AUTHENTICATED).
- No fallback permisivo: expired -> EXPIRED/UNAUTHENTICATED, invalid -> INVALID/UNAUTHENTICATED.
- Determinismo e inmutabilidad (frozen=True, deep_freeze).
- Integración N.1: Autenticación exitosa resuelve a una Identity N.1 estable; tokens distintos para el
  mismo subject resultan en la misma identity_id.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Dict, Union, Tuple, Sequence

from src.domain.identity.models import (
    IdentityReference,
    IdentityType,
    PrincipalIdentity,
)
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class AuthenticationMethod(str, Enum):
    """
    Métodos canónicos de autenticación basados en el repositorio real.
    """
    OAUTH2 = "OAUTH2"
    BEARER_TOKEN = "BEARER_TOKEN"
    API_CREDENTIAL = "API_CREDENTIAL"
    INTERNAL_SERVICE = "INTERNAL_SERVICE"
    SYSTEM_ASSERTION = "SYSTEM_ASSERTION"
    UNKNOWN = "UNKNOWN"


class AuthenticationStatus(str, Enum):
    """
    Estados canónicos del resultado de autenticación.
    """
    AUTHENTICATED = "AUTHENTICATED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


@dataclass(frozen=True)
class AuthenticationRequest:
    """
    Solicitud inmutable de autenticación recibida por el sistema.
    Permite encapsular credenciales o tokens efímeros para su validación inmediata.
    Las credenciales NO deben persistirse ni loggearse en crudo.
    """
    method: AuthenticationMethod
    provider: str
    token_or_secret: Optional[str] = field(repr=False, default=None)
    declared_subject: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.method, AuthenticationMethod):
            try:
                object.__setattr__(self, "method", AuthenticationMethod(self.method))
            except Exception as e:
                raise ValueError(f"Invalid AuthenticationMethod: {self.method}") from e

        if not self.provider or not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string.")

        norm_provider = self.provider.strip().lower()
        validate_safe_identifier(norm_provider, field_name="provider")
        object.__setattr__(self, "provider", norm_provider)

        if self.declared_subject is not None:
            norm_subject = str(self.declared_subject).strip()
            if norm_subject:
                validate_safe_identifier(norm_subject, field_name="declared_subject")
                object.__setattr__(self, "declared_subject", norm_subject)
            else:
                object.__setattr__(self, "declared_subject", None)

        if self.correlation_id is not None:
            norm_cid = str(self.correlation_id).strip()
            if norm_cid:
                validate_safe_identifier(norm_cid, field_name="correlation_id")
                object.__setattr__(self, "correlation_id", norm_cid)
            else:
                object.__setattr__(self, "correlation_id", None)

        # Sanitizar y congelar metadata
        sanitized = sanitize_security_data(dict(self.metadata)) if self.metadata else {}
        frozen_meta = deep_freeze(sanitized)
        object.__setattr__(self, "metadata", frozen_meta)


def compute_auth_result_checksum(
    identity_id: Optional[str],
    method: Union[AuthenticationMethod, str],
    provider: str,
    status: Union[AuthenticationStatus, str],
    authenticated_at: Optional[datetime],
    expires_at: Optional[datetime],
    reason_codes: Sequence[str],
    correlation_id: Optional[str],
    metadata: Mapping[str, Any],
) -> str:
    """
    Calcula el checksum SHA-256 determinista para un AuthenticationResult.
    Asegura integridad y no-repudio del resultado de autenticación.
    """
    m_val = method.value if hasattr(method, "value") else str(method)
    s_val = status.value if hasattr(status, "value") else str(status)

    sanitized_meta = sanitize_security_data(dict(metadata))

    payload = {
        "identity_id": identity_id or "",
        "method": m_val,
        "provider": provider,
        "status": s_val,
        "authenticated_at": authenticated_at.isoformat() if authenticated_at else "",
        "expires_at": expires_at.isoformat() if expires_at else "",
        "reason_codes": sorted(list(reason_codes)),
        "correlation_id": correlation_id or "",
        "metadata": sanitized_meta,
    }

    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthenticationResult:
    """
    Resultado explícito, determinista e inmutable del proceso de autenticación.
    Nunca almacena ni expone secretos (tokens, llaves, passwords).
    """
    status: AuthenticationStatus
    method: AuthenticationMethod
    provider: str
    principal: Optional[IdentityReference] = None
    authenticated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.status, AuthenticationStatus):
            try:
                object.__setattr__(self, "status", AuthenticationStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid AuthenticationStatus: {self.status}") from e

        if not isinstance(self.method, AuthenticationMethod):
            try:
                object.__setattr__(self, "method", AuthenticationMethod(self.method))
            except Exception as e:
                raise ValueError(f"Invalid AuthenticationMethod: {self.method}") from e

        if not self.provider or not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string.")

        norm_provider = self.provider.strip().lower()
        validate_safe_identifier(norm_provider, field_name="provider")
        object.__setattr__(self, "provider", norm_provider)

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

        # Sanitizar y congelar metadata
        sanitized = sanitize_security_data(dict(self.metadata)) if self.metadata else {}
        frozen_meta = deep_freeze(sanitized)
        object.__setattr__(self, "metadata", frozen_meta)

        # Regla estricta: si está AUTHENTICATED, debe tener authenticated_at y principal válido
        if self.status == AuthenticationStatus.AUTHENTICATED:
            if self.principal is None:
                raise ValueError("Authenticated result must have a valid principal.")
            if self.authenticated_at is None:
                object.__setattr__(self, "authenticated_at", datetime.now(timezone.utc))

        # Checksum determinista
        identity_id = self.principal.identity_id if self.principal else None
        cksum = compute_auth_result_checksum(
            identity_id=identity_id,
            method=self.method,
            provider=self.provider,
            status=self.status,
            authenticated_at=self.authenticated_at,
            expires_at=self.expires_at,
            reason_codes=self.reason_codes,
            correlation_id=self.correlation_id,
            metadata=dict(self.metadata),
        )
        object.__setattr__(self, "checksum", cksum)

    @property
    def is_authenticated(self) -> bool:
        """Indica si el actor fue validado exitosamente y no está expirado."""
        return self.status == AuthenticationStatus.AUTHENTICATED

    @property
    def identity_id(self) -> Optional[str]:
        """Atajo al identity_id del principal si existe."""
        return self.principal.identity_id if self.principal else None


@dataclass(frozen=True)
class PrincipalContext:
    """
    Contexto seguro de ejecución para capas downstream.
    Expone la identidad demostrada y el estado de autenticación sin secretos.
    """
    principal: IdentityReference
    auth_result: AuthenticationResult

    def __post_init__(self):
        if not isinstance(self.principal, IdentityReference):
            raise ValueError("principal must be an IdentityReference.")
        if not isinstance(self.auth_result, AuthenticationResult):
            raise ValueError("auth_result must be an AuthenticationResult.")

    @property
    def is_authenticated(self) -> bool:
        return self.auth_result.is_authenticated

    @property
    def identity_id(self) -> str:
        return self.principal.identity_id

    @property
    def provider(self) -> str:
        return self.auth_result.provider

    @property
    def method(self) -> AuthenticationMethod:
        return self.auth_result.method

    @property
    def expires_at(self) -> Optional[datetime]:
        return self.auth_result.expires_at
