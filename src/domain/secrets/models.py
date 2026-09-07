"""
Modelos de dominio para Gestión de Secretos (Hito N.5 — Transversal N Security, Governance y Safety).

Define:
- SecretType: Taxonomía canónica de secretos (API_KEY, CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN, WEBHOOK_SECRET, INTERNAL_CREDENTIAL, UNKNOWN).
- SecretStatus: Estados de ciclo de vida del secreto (ACTIVE, REVOKED, EXPIRED, ROTATED, UNKNOWN).
- SecretResolutionStatus: Estado del resultado de resolución (RESOLVED, NOT_FOUND, EXPIRED, REVOKED, ERROR, UNKNOWN).
- SecretReference: Referencia inmutable y liviana que nunca contiene el material secreto.
- SecretValue: Wrapper seguro en memoria para el material confidencial con repr/str redactados y sin serialización directa.
- SecretMetadata: Metadatos inmutables y auditables de un secreto sin incluir valores en crudo.
- SecretResolutionResult: Resultado inmutable de un intento de resolución de secreto.

Principios N.5:
- N.5 responde: "¿Cómo obtiene, almacena, referencia, rota y usa el sistema credenciales/secretos sin exponerlos ni convertirlos en datos de dominio?".
- Secret Reference != Secret Value: Los modelos de dominio solo almacenan/reciben referencias.
- Secret != Identity (N.1), Secret != Authentication (N.2), Secret != Permissions (N.4).
- Cero almacenamiento en texto plano en el repo. Cero persistencia accidental.
- Aislamiento estricto: el valor confidencial solo se materializa en memoria dentro del SecretValue y se devela exclusivamente en la frontera de infraestructura (`reveal()`).
- Integridad verificada mediante checksum SHA-256 determinista sobre metadatos sanitizados.
- UNKNOWN se preserva explícitamente y se diferencia de estados activos/válidos.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Union, Sequence, Dict

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class SecretType(str, Enum):
    """
    Taxonomía canónica de tipos de secretos del sistema.
    Basada en secretos reales requeridos por adaptadores y proveedores.
    """
    API_KEY = "API_KEY"
    CLIENT_SECRET = "CLIENT_SECRET"
    ACCESS_TOKEN = "ACCESS_TOKEN"
    REFRESH_TOKEN = "REFRESH_TOKEN"
    WEBHOOK_SECRET = "WEBHOOK_SECRET"
    INTERNAL_CREDENTIAL = "INTERNAL_CREDENTIAL"
    UNKNOWN = "UNKNOWN"


class SecretStatus(str, Enum):
    """
    Estados canónicos de ciclo de vida de un secreto.
    """
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    ROTATED = "ROTATED"
    UNKNOWN = "UNKNOWN"


class SecretResolutionStatus(str, Enum):
    """
    Estados de la operación de resolución de un secreto.
    """
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class SecretValue:
    """
    Wrapper seguro en memoria para material confidencial.

    Protecciones estrictas:
    - __repr__ y __str__ siempre retornan "[REDACTED]".
    - No es indexable ni serializable a JSON por defecto.
    - El valor en crudo solo es accesible mediante `reveal()` en la frontera de infraestructura.
    - Comparación en tiempo constante contra otros SecretValue para evitar timing attacks básicos.
    """
    __slots__ = ("_value", "_revealed_count")

    def __init__(self, raw_value: str):
        if not isinstance(raw_value, str):
            raise ValueError("Secret value must be a string.")
        if len(raw_value) == 0:
            raise ValueError("Secret value cannot be empty.")
        self._value = raw_value
        self._revealed_count = 0

    def reveal(self) -> str:
        """
        Devela el valor en crudo del secreto.
        Debe invocarse exclusivamente en la frontera final de infraestructura (headers HTTP, payload de cliente API).
        """
        self._revealed_count += 1
        return self._value

    def __repr__(self) -> str:
        return "<SecretValue: [REDACTED]>"

    def __str__(self) -> str:
        return "[REDACTED]"

    def __format__(self, format_spec: str) -> str:
        return "[REDACTED]"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return False
        # Comparación constante
        import hmac
        return hmac.compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        # Prevenir uso accidental como clave de dict o set que filtre valor
        return hash(self.__class__)


def compute_secret_metadata_checksum(
    reference_id: str,
    secret_name: str,
    provider: str,
    secret_type: Union[SecretType, str],
    version: str,
    status: Union[SecretStatus, str],
    created_at: Optional[datetime],
    expires_at: Optional[datetime],
    metadata: Mapping[str, Any],
) -> str:
    """
    Calcula el checksum SHA-256 canónico y determinista para los metadatos de un secreto.
    NUNCA procesa ni incluye material confidencial.
    """
    st_val = secret_type.value if isinstance(secret_type, SecretType) else str(secret_type)
    stat_val = status.value if isinstance(status, SecretStatus) else str(status)
    sanitized_meta = sanitize_security_data(dict(metadata))

    payload = {
        "reference_id": reference_id,
        "secret_name": secret_name,
        "provider": provider,
        "secret_type": st_val,
        "version": version,
        "status": stat_val,
        "created_at": created_at.isoformat() if created_at else "",
        "expires_at": expires_at.isoformat() if expires_at else "",
        "metadata": sanitized_meta,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SecretReference:
    """
    Referencia inmutable y liviana a un secreto.

    ESTRICTAMENTE DESACOPLADA DEL VALOR:
    Nunca contiene contraseñas, tokens, api keys ni strings sensibles.
    Se utiliza en modelos de dominio, configuraciones y pipelines para solicitar credenciales bajo demanda.
    """
    reference_id: str
    provider: str
    secret_name: str
    secret_type: SecretType = SecretType.UNKNOWN
    version: str = "1"
    env_var_fallback: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.reference_id, field_name="reference_id")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string.")
        if not isinstance(self.secret_name, str) or not self.secret_name.strip():
            raise ValueError("secret_name must be a non-empty string.")

        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(self, "secret_name", self.secret_name.strip())

        if not isinstance(self.secret_type, SecretType):
            try:
                object.__setattr__(self, "secret_type", SecretType(self.secret_type))
            except Exception as e:
                raise ValueError(f"Invalid secret_type: {self.secret_type}") from e

        if not self.version or not isinstance(self.version, str):
            raise ValueError("version must be a non-empty string.")

        if self.env_var_fallback is not None:
            if not isinstance(self.env_var_fallback, str) or not self.env_var_fallback.strip():
                raise ValueError("env_var_fallback must be a non-empty string when provided.")
            object.__setattr__(self, "env_var_fallback", self.env_var_fallback.strip())

    @property
    def is_unknown(self) -> bool:
        return self.secret_type == SecretType.UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la referencia a diccionario serializable."""
        return {
            "reference_id": self.reference_id,
            "provider": self.provider,
            "secret_name": self.secret_name,
            "secret_type": self.secret_type.value,
            "version": self.version,
            "env_var_fallback": self.env_var_fallback,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SecretReference":
        """Reconstruye una SecretReference desde un diccionario."""
        return cls(
            reference_id=data["reference_id"],
            provider=data["provider"],
            secret_name=data["secret_name"],
            secret_type=SecretType(data.get("secret_type", "UNKNOWN")),
            version=data.get("version", "1"),
            env_var_fallback=data.get("env_var_fallback"),
        )


@dataclass(frozen=True)
class SecretMetadata:
    """
    Metadatos inmutables, auditables y versionados sobre un secreto.
    Representa el estado administrativo del secreto sin exponer su material.
    """
    reference_id: str
    secret_name: str
    provider: str
    secret_type: SecretType
    version: str = "1"
    status: SecretStatus = SecretStatus.ACTIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.reference_id, field_name="reference_id")
        if not isinstance(self.secret_name, str) or not self.secret_name.strip():
            raise ValueError("secret_name must be a non-empty string.")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string.")

        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(self, "secret_name", self.secret_name.strip())

        if not isinstance(self.secret_type, SecretType):
            try:
                object.__setattr__(self, "secret_type", SecretType(self.secret_type))
            except Exception as e:
                raise ValueError(f"Invalid secret_type: {self.secret_type}") from e

        if not isinstance(self.status, SecretStatus):
            try:
                object.__setattr__(self, "status", SecretStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e

        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC).")
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware (UTC).")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware (UTC).")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if not self.checksum:
            digest = compute_secret_metadata_checksum(
                reference_id=self.reference_id,
                secret_name=self.secret_name,
                provider=self.provider,
                secret_type=self.secret_type,
                version=self.version,
                status=self.status,
                created_at=self.created_at,
                expires_at=self.expires_at,
                metadata=self.metadata,
            )
            object.__setattr__(self, "checksum", digest)

    @property
    def is_active(self) -> bool:
        return self.status == SecretStatus.ACTIVE

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        current_time = now or datetime.now(timezone.utc)
        return current_time >= self.expires_at

    def to_reference(self, env_var_fallback: Optional[str] = None) -> SecretReference:
        """Crea una SecretReference correspondiente a estos metadatos."""
        return SecretReference(
            reference_id=self.reference_id,
            provider=self.provider,
            secret_name=self.secret_name,
            secret_type=self.secret_type,
            version=self.version,
            env_var_fallback=env_var_fallback,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convierte los metadatos a diccionario seguro (sin secreto)."""
        return {
            "reference_id": self.reference_id,
            "secret_name": self.secret_name,
            "provider": self.provider,
            "secret_type": self.secret_type.value,
            "version": self.version,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "metadata": dict(self.metadata),
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SecretMetadata":
        """Reconstruye SecretMetadata desde un diccionario."""
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        updated_at = datetime.fromisoformat(data["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        expires_at = None
        if data.get("expires_at"):
            expires_at = datetime.fromisoformat(data["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

        return cls(
            reference_id=data["reference_id"],
            secret_name=data["secret_name"],
            provider=data["provider"],
            secret_type=SecretType(data.get("secret_type", "UNKNOWN")),
            version=data.get("version", "1"),
            status=SecretStatus(data.get("status", "ACTIVE")),
            created_at=created_at,
            updated_at=updated_at,
            expires_at=expires_at,
            metadata=data.get("metadata", {}),
            checksum=data.get("checksum"),
        )


@dataclass(frozen=True)
class SecretResolutionResult:
    """
    Resultado inmutable de la resolución de un secreto.

    Contiene:
    - status: RESOLVED, NOT_FOUND, EXPIRED, REVOKED, ERROR, UNKNOWN.
    - reference: La SecretReference original solicitada.
    - secret_value: El wrapper SecretValue (None si no pudo resolverse).
    - metadata: SecretMetadata resuelta si estaba disponible.
    - error_message: Detalle en caso de fallo (sin revelar credenciales).
    - resolved_at: Timestamp UTC de la resolución.
    """
    status: SecretResolutionStatus
    reference: SecretReference
    secret_value: Optional[SecretValue] = None
    metadata: Optional[SecretMetadata] = None
    error_message: Optional[str] = None
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.status, SecretResolutionStatus):
            try:
                object.__setattr__(self, "status", SecretResolutionStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e

        if not isinstance(self.reference, SecretReference):
            raise ValueError("reference must be a SecretReference instance.")

        if self.resolved_at.tzinfo is None:
            raise ValueError("resolved_at must be timezone-aware (UTC).")

        if self.status == SecretResolutionStatus.RESOLVED:
            if self.secret_value is None:
                raise ValueError("secret_value cannot be None when status is RESOLVED.")
        else:
            if self.secret_value is not None:
                raise ValueError("secret_value must be None when status is not RESOLVED.")

    @property
    def is_resolved(self) -> bool:
        return self.status == SecretResolutionStatus.RESOLVED

    def reveal_value(self) -> str:
        """
        Devela el valor en crudo si la resolución fue exitosa.
        Lanza RuntimeError si no está resuelto.
        """
        if not self.is_resolved or self.secret_value is None:
            raise RuntimeError(f"Cannot reveal secret: status is {self.status.value}. Error: {self.error_message}")
        return self.secret_value.reveal()

    def to_audit_payload(self) -> Dict[str, Any]:
        """
        Genera payload seguro para registro de auditoría (K.1) o traza (K.2).
        NUNCA incluye el valor secreto.
        """
        return {
            "status": self.status.value,
            "reference_id": self.reference.reference_id,
            "provider": self.reference.provider,
            "secret_name": self.reference.secret_name,
            "secret_type": self.reference.secret_type.value,
            "version": self.reference.version,
            "has_value": self.is_resolved,
            "error_message": self.error_message,
            "resolved_at": self.resolved_at.isoformat(),
        }
