"""
Modelos de dominio para Tenant Isolation (Hito O.1 — SaaS / Platformization).

Define:
- TenantId: Identificador canónico, seguro e inmutable de un tenant.
- TenantContext: Contexto inmutable de ejecución y seguridad que transporta tenant_id, identity_id, correlation_id, marketplace_account_id y metadata sanitizada.
- TenantReference: Referencia inmutable a un tenant para trazabilidad y auditoría.
- TenantScope: Alcance o scope formal asociado a un tenant (compatible con RBAC y gobernanza).
- TenantResolutionStatus: Estados canónicos de resolución de tenant (VALID, INVALID, UNKNOWN, MISMATCH, NOT_FOUND).
- CrossTenantAccessError: Excepción canónica lanzada cuando se intenta una operación cross-tenant no autorizada.
- TenantSecurityViolationError: Excepción ante ausencia de tenant o violación de aislamiento.

Principios O.1:
- "¿Puede el sistema garantizar que los datos, secretos, memoria, decisiones, caché y acciones de un tenant nunca sean visibles ni utilizables por otro tenant?"
- Tenant != Marketplace Account != Identity != Mission.
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- Cero almacenamiento de secretos o credenciales en TenantContext.
- Validaciones rigurosas contra path traversal (validate_safe_identifier).
- Fail-Safe: Ante tenant ausente o mismatch -> ERROR / DENY / UNKNOWN. NUNCA default tenant ni fallback silencioso.
- Determinismo y reproducibilidad SHA-256.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Union, Sequence, Dict

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.identity.models import IdentityReference


class CrossTenantAccessError(Exception):
    """Excepción lanzada cuando ocurre un intento de acceso entre diferentes tenants."""
    pass


class TenantSecurityViolationError(Exception):
    """Excepción lanzada ante falta de contexto de tenant obligatorio o identificador inseguro."""
    pass


class TenantResolutionStatus(str, Enum):
    """Estados canónicos de resolución de contexto de tenant."""
    VALID = "VALID"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"
    MISMATCH = "MISMATCH"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class TenantId:
    """
    Identificador canónico, seguro e inmutable para un Tenant en la plataforma.

    Reglas:
    - No vacío, safe basename, sin path traversal ('/', '\\', '..', ':').
    - Estable, explícito y no derivado de tokens ni sesiones efímeras.
    """
    value: str

    def __post_init__(self):
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("TenantId.value must be a non-empty string.")
        clean_val = self.value.strip()
        validate_safe_identifier(clean_val, field_name="TenantId")
        object.__setattr__(self, "value", clean_val)

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"TenantId('{self.value}')"


@dataclass(frozen=True)
class TenantReference:
    """
    Referencia liviana e inmutable a un Tenant.
    Utilizada en auditorías, trazas y relaciones de bajo acoplamiento.
    """
    tenant_id: str
    tenant_name: Optional[str] = None
    created_at: Optional[datetime] = None

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.tenant_name is not None and not isinstance(self.tenant_name, str):
            raise ValueError("tenant_name must be a string if provided.")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass(frozen=True)
class TenantScope:
    """
    Alcance de recursos delimitado por un tenant.
    Permite asociar sub-scopes (e.g. marketplace_account_id, environment) manteniendo el tenant como raíz de aislamiento.
    """
    tenant_id: str
    marketplace_account_id: Optional[str] = None
    environment: str = "PRODUCTION"

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.marketplace_account_id is not None:
            validate_safe_identifier(self.marketplace_account_id, field_name="marketplace_account_id")
        if not isinstance(self.environment, str) or not self.environment.strip():
            raise ValueError("environment must be a non-empty string.")
        object.__setattr__(self, "environment", self.environment.strip().upper())

    @property
    def canonical_scope(self) -> str:
        """Genera un token de scope canónico para RBAC y autorización."""
        if self.marketplace_account_id:
            return f"tenant_{self.tenant_id}__account_{self.marketplace_account_id}"
        return f"tenant_{self.tenant_id}"

    def matches(self, other_tenant_id: str, other_account_id: Optional[str] = None) -> bool:
        """Verifica si este scope coincide con el tenant_id y cuenta solicitada."""
        if self.tenant_id != other_tenant_id:
            return False
        if self.marketplace_account_id is not None and other_account_id is not None:
            return self.marketplace_account_id == other_account_id
        return True


def compute_tenant_context_checksum(
    tenant_id: str,
    identity_id: Optional[str],
    correlation_id: Optional[str],
    marketplace_account_id: Optional[str],
    metadata: Mapping[str, Any],
) -> str:
    """Calcula un checksum SHA-256 canónico y determinista para el contexto de tenant."""
    sanitized_meta = sanitize_security_data(dict(metadata))
    payload = {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "correlation_id": correlation_id,
        "marketplace_account_id": marketplace_account_id,
        "metadata": sanitized_meta,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TenantContext:
    """
    Contexto inmutable y explícito de Tenant que viaja a través de todas las capas de ejecución sensible.

    Contiene:
    - tenant_id: Identificador canónico y seguro del tenant (obligatorio).
    - identity_id: Identificador canónico del actor/usuario ejecutor (N.1, opcional o resuelto).
    - correlation_id: ID de trazabilidad (K.1/K.2, opcional).
    - marketplace_account_id: ID de la cuenta de marketplace asociada (opcional, desacoplado de tenant_id).
    - metadata: Metadatos sanitizados inmutables.
    - checksum: Verificación criptográfica SHA-256 de integridad.

    ESTRICTAMENTE SIN SECRETOS:
    Cero credenciales, passwords, tokens, API keys o CoT.
    """
    tenant_id: str
    identity_id: Optional[str] = None
    correlation_id: Optional[str] = None
    marketplace_account_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(init=False)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")

        if self.identity_id is not None:
            validate_safe_identifier(self.identity_id, field_name="identity_id")

        if self.correlation_id is not None:
            if not isinstance(self.correlation_id, str) or not self.correlation_id.strip():
                raise ValueError("correlation_id must be a non-empty string when provided.")

        if self.marketplace_account_id is not None:
            validate_safe_identifier(self.marketplace_account_id, field_name="marketplace_account_id")

        sanitized = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized)
        object.__setattr__(self, "metadata", frozen_meta)

        chk = compute_tenant_context_checksum(
            tenant_id=self.tenant_id,
            identity_id=self.identity_id,
            correlation_id=self.correlation_id,
            marketplace_account_id=self.marketplace_account_id,
            metadata=self.metadata,
        )
        object.__setattr__(self, "checksum", chk)

    @property
    def scope(self) -> TenantScope:
        """Retorna el TenantScope asociado a este contexto."""
        return TenantScope(
            tenant_id=self.tenant_id,
            marketplace_account_id=self.marketplace_account_id,
        )

    def matches_tenant(self, expected_tenant_id: Union[str, TenantId]) -> bool:
        """Comprueba coincidencia exacta con un tenant_id esperado."""
        target = expected_tenant_id.value if isinstance(expected_tenant_id, TenantId) else expected_tenant_id
        return self.tenant_id == target

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el contexto a diccionario seguro."""
        return {
            "tenant_id": self.tenant_id,
            "identity_id": self.identity_id,
            "correlation_id": self.correlation_id,
            "marketplace_account_id": self.marketplace_account_id,
            "metadata": dict(self.metadata),
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TenantContext":
        """Instancia un TenantContext desde un diccionario seguro."""
        return cls(
            tenant_id=data["tenant_id"],
            identity_id=data.get("identity_id"),
            correlation_id=data.get("correlation_id"),
            marketplace_account_id=data.get("marketplace_account_id"),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class TenantScopedResource:
    """
    Contrato base / wrapper de datos para cualquier recurso perteneciente a un tenant.
    Garantiza que el resource_id y tenant_id formen una clave compuesta inequívoca.
    """
    tenant_id: str
    resource_id: str
    resource_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        validate_safe_identifier(self.resource_id, field_name="resource_id")
        if not isinstance(self.resource_type, str) or not self.resource_type.strip():
            raise ValueError("resource_type must be a non-empty string.")

        sanitized = sanitize_security_data(dict(self.payload))
        frozen_payload = deep_freeze(sanitized)
        object.__setattr__(self, "payload", frozen_payload)

    @property
    def composite_key(self) -> str:
        """Clave lógica compuesta: tenant_id + '::' + resource_id."""
        return f"{self.tenant_id}::{self.resource_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "payload": dict(self.payload),
            "created_at": self.created_at.isoformat(),
        }
