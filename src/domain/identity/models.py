"""
Modelos de dominio para Identidad Canónica (Hito N.1 - Transversal N Security, Governance y Safety).

Define:
- IdentityType: Taxonomía canónica de actores reales (USER, AGENT, SYSTEM, SERVICE, SCHEDULER, MARKETPLACE, EXTERNAL_TOOL, UNKNOWN).
- IdentityStatus: Estados canónicos de ciclo de vida de la identidad (ACTIVE, SUSPENDED, DEPRECATED, UNKNOWN).
- Identity: Entidad de dominio inmutable para la identidad canónica de un actor.
- IdentityReference / PrincipalIdentity: Referencia inmutable liviana para correlación y vinculación.
- Mappers y adaptadores con AuditActor (K.1), AgentTrace (K.2) y OAuth (Hito E).

Principios N.1:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- N.1 responde exclusivamente a: "¿Quién o qué actor está realizando esta operación dentro del sistema?".
- N.1 NO responde ni evalúa si el actor está autenticado (N.2), autorizado (N.3) o tiene permisos RBAC (N.4).
- Cero almacenamiento de credenciales, access tokens, refresh tokens, passwords, API keys o CoT.
- Identidad determinista y canónica (independiente de tokens efímeros o sesiones volátiles).
- UNKNOWN se preserva explícitamente y se diferencia de identidades confirmadas (no fallar abierto ni inventar identidades).
- Verificación criptográfica de integridad SHA-256.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Any, Dict, Union, Tuple, Sequence

from src.domain.security.models import (
    validate_safe_identifier,
    SENSITIVE_KEYS,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.audit.models import AuditActor, AuditActorType
from src.domain.agent_trace.models import AgentTraceRecord


class IdentityType(str, Enum):
    """
    Taxonomía canónica de tipos de actores del sistema.
    Basada en actores reales del repositorio.
    """
    USER = "USER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"
    SERVICE = "SERVICE"
    SCHEDULER = "SCHEDULER"
    MARKETPLACE = "MARKETPLACE"
    EXTERNAL_TOOL = "EXTERNAL_TOOL"
    UNKNOWN = "UNKNOWN"


class IdentityStatus(str, Enum):
    """
    Estados canónicos de ciclo de vida de la identidad.
    """
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DEPRECATED = "DEPRECATED"
    UNKNOWN = "UNKNOWN"


def build_canonical_identifier(
    identity_type: Union[IdentityType, str],
    provider: Optional[str],
    raw_identifier: str,
) -> str:
    """
    Construye un identificador canónico determinista y normalizado.
    Ejemplos:
    - 'user:mercadolibre:123456789'
    - 'agent:internal:autonomous_loop'
    - 'system:internal:policy_engine'
    - 'unknown:system:unresolved_001'
    """
    it_val = identity_type.value if hasattr(identity_type, "value") else str(identity_type)
    norm_it = it_val.strip().lower()
    norm_provider = (provider.strip().lower() if provider and provider.strip() else "internal")
    norm_id = raw_identifier.strip().lower()
    return f"{norm_it}:{norm_provider}:{norm_id}"


def compute_identity_checksum(
    identity_id: str,
    identity_type: Union[IdentityType, str],
    canonical_identifier: str,
    display_name: Optional[str],
    provider: Optional[str],
    external_subject_id: Optional[str],
    status: Union[IdentityStatus, str],
    schema_version: str,
    metadata: Mapping[str, Any],
) -> str:
    """
    Calcula el checksum canónico SHA-256 determinista para una Identity.
    Cubre todos los campos semánticos inmutables.
    """
    it_val = identity_type.value if hasattr(identity_type, "value") else str(identity_type)
    stat_val = status.value if hasattr(status, "value") else str(status)

    sanitized_meta = sanitize_security_data(dict(metadata))

    semantic_payload = {
        "identity_id": identity_id,
        "identity_type": it_val,
        "canonical_identifier": canonical_identifier,
        "display_name": display_name or "",
        "provider": provider or "",
        "external_subject_id": external_subject_id or "",
        "status": stat_val,
        "schema_version": schema_version,
        "metadata": sanitized_meta,
    }

    serialized = json.dumps(semantic_payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdentityReference:
    """
    Referencia inmutable y liviana a una identidad para correlación en eventos, trazas y auditorías.
    """
    identity_id: str
    identity_type: IdentityType
    canonical_identifier: str
    display_name: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.identity_id, field_name="identity_id")
        if not isinstance(self.identity_type, IdentityType):
            try:
                object.__setattr__(self, "identity_type", IdentityType(self.identity_type))
            except Exception as e:
                raise ValueError(f"Invalid identity_type: {self.identity_type}") from e
        if not self.canonical_identifier or not isinstance(self.canonical_identifier, str):
            raise ValueError("canonical_identifier must be a non-empty string.")

    @property
    def is_unknown(self) -> bool:
        return self.identity_type == IdentityType.UNKNOWN


PrincipalIdentity = IdentityReference


@dataclass(frozen=True)
class Identity:
    """
    Entidad de dominio inmutable para la Identidad Canónica (N.1).
    Representa quién es el actor dentro del sistema de forma estable,
    desacoplada de credenciales, sesiones y tokens.
    """
    identity_id: str
    identity_type: IdentityType
    canonical_identifier: str
    created_at: datetime
    updated_at: datetime
    display_name: Optional[str] = None
    provider: Optional[str] = None
    external_subject_id: Optional[str] = None
    status: IdentityStatus = IdentityStatus.ACTIVE
    schema_version: str = "1.0.0"
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 1. Validar identificador seguro contra path traversal
        validate_safe_identifier(self.identity_id, field_name="identity_id")

        # 2. Validar canonical_identifier
        if not self.canonical_identifier or not isinstance(self.canonical_identifier, str) or not self.canonical_identifier.strip():
            raise ValueError("canonical_identifier must be a non-empty string.")

        # 3. Validar enums
        if not isinstance(self.identity_type, IdentityType):
            try:
                object.__setattr__(self, "identity_type", IdentityType(self.identity_type))
            except Exception as e:
                raise ValueError(f"Invalid identity_type: {self.identity_type}") from e

        if not isinstance(self.status, IdentityStatus):
            try:
                object.__setattr__(self, "status", IdentityStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e

        # 4. Validar timestamps timezone-aware (UTC)
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware (UTC)")

        # 5. Sanitizar metadata recursivamente y congelar
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        frozen_meta = deep_freeze(sanitized_meta)
        object.__setattr__(self, "metadata", frozen_meta)

        # 6. Calcular o validar checksum SHA-256
        expected_checksum = compute_identity_checksum(
            identity_id=self.identity_id,
            identity_type=self.identity_type,
            canonical_identifier=self.canonical_identifier,
            display_name=self.display_name,
            provider=self.provider,
            external_subject_id=self.external_subject_id,
            status=self.status,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )

        if not self.checksum:
            object.__setattr__(self, "checksum", expected_checksum)
        elif self.checksum != expected_checksum:
            raise ValueError(
                f"Checksum mismatch for Identity '{self.identity_id}': "
                f"provided '{self.checksum}' != expected '{expected_checksum}'"
            )

    @property
    def is_unknown(self) -> bool:
        return self.identity_type == IdentityType.UNKNOWN

    def to_reference(self) -> IdentityReference:
        """Genera una referencia liviana inmutable a esta identidad."""
        return IdentityReference(
            identity_id=self.identity_id,
            identity_type=self.identity_type,
            canonical_identifier=self.canonical_identifier,
            display_name=self.display_name,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la entidad a un diccionario serializable."""
        return {
            "identity_id": self.identity_id,
            "identity_type": self.identity_type.value,
            "canonical_identifier": self.canonical_identifier,
            "display_name": self.display_name,
            "provider": self.provider,
            "external_subject_id": self.external_subject_id,
            "status": self.status.value,
            "schema_version": self.schema_version,
            "checksum": self.checksum,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Identity":
        """Reconstruye una instancia inmutable desde un diccionario."""
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        updated_at = datetime.fromisoformat(data["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        return cls(
            identity_id=data["identity_id"],
            identity_type=IdentityType(data["identity_type"]),
            canonical_identifier=data["canonical_identifier"],
            display_name=data.get("display_name"),
            provider=data.get("provider"),
            external_subject_id=data.get("external_subject_id"),
            status=IdentityStatus(data.get("status", "ACTIVE")),
            schema_version=data.get("schema_version", "1.0.0"),
            checksum=data.get("checksum", ""),
            created_at=created_at,
            updated_at=updated_at,
            metadata=data.get("metadata", {}),
        )


# =====================================================================
# Adaptadores e Integración canónica (Audit K.1, AgentTrace K.2, OAuth)
# =====================================================================

AUDIT_ACTOR_TYPE_MAP: Dict[AuditActorType, IdentityType] = {
    AuditActorType.SYSTEM: IdentityType.SYSTEM,
    AuditActorType.AGENT: IdentityType.AGENT,
    AuditActorType.USER: IdentityType.USER,
    AuditActorType.POLICY_ENGINE: IdentityType.SYSTEM,
    AuditActorType.ACTION_EXECUTOR: IdentityType.SERVICE,
    AuditActorType.SCHEDULER: IdentityType.SCHEDULER,
    AuditActorType.EXTERNAL_TOOL: IdentityType.EXTERNAL_TOOL,
    AuditActorType.MARKETPLACE: IdentityType.MARKETPLACE,
}

IDENTITY_TO_AUDIT_ACTOR_TYPE_MAP: Dict[IdentityType, AuditActorType] = {
    IdentityType.USER: AuditActorType.USER,
    IdentityType.AGENT: AuditActorType.AGENT,
    IdentityType.SYSTEM: AuditActorType.SYSTEM,
    IdentityType.SERVICE: AuditActorType.ACTION_EXECUTOR,
    IdentityType.SCHEDULER: AuditActorType.SCHEDULER,
    IdentityType.MARKETPLACE: AuditActorType.MARKETPLACE,
    IdentityType.EXTERNAL_TOOL: AuditActorType.EXTERNAL_TOOL,
    IdentityType.UNKNOWN: AuditActorType.SYSTEM,
}


def audit_actor_to_identity_reference(actor: AuditActor) -> IdentityReference:
    """
    Mapea un AuditActor existente de K.1 a un IdentityReference canónico de N.1.
    """
    mapped_type = AUDIT_ACTOR_TYPE_MAP.get(actor.actor_type, IdentityType.SYSTEM)
    actor_id_clean = actor.actor_id.strip() if actor.actor_id else "unknown"

    # Derivar canonical_identifier
    canonical_id = build_canonical_identifier(
        identity_type=mapped_type,
        provider="internal",
        raw_identifier=actor_id_clean,
    )

    # Generar un identity_id seguro
    safe_id = actor_id_clean.replace(":", "_").replace("/", "_").replace("\\", "_")
    if not safe_id or safe_id == ".":
        safe_id = "actor_unknown"

    return IdentityReference(
        identity_id=f"id_{safe_id}",
        identity_type=mapped_type,
        canonical_identifier=canonical_id,
        display_name=actor_id_clean,
    )


def identity_to_audit_actor(identity: Union[Identity, IdentityReference]) -> AuditActor:
    """
    Mapea una Identity o IdentityReference a un AuditActor de K.1.
    Permite vincular acciones de auditoría con la identidad canónica sin romper la taxonomía K.1.
    """
    actor_type = IDENTITY_TO_AUDIT_ACTOR_TYPE_MAP.get(identity.identity_type, AuditActorType.SYSTEM)
    return AuditActor(
        actor_type=actor_type,
        actor_id=identity.identity_id,
        details={"canonical_identifier": identity.canonical_identifier},
    )


def agent_trace_to_identity_reference(trace: AgentTraceRecord) -> IdentityReference:
    """
    Mapea un AgentTraceRecord de K.2 a un IdentityReference canónico de N.1.
    Usa component_name de forma determinista para la identidad del agente,
    manteniendo la identidad del agente constante entre ejecuciones distintas.
    """
    comp_clean = trace.component_name.strip() if trace.component_name else "unknown_agent"
    canonical_id = build_canonical_identifier(
        identity_type=IdentityType.AGENT,
        provider="internal",
        raw_identifier=comp_clean,
    )
    safe_id = comp_clean.replace(":", "_").replace("/", "_").replace("\\", "_")
    return IdentityReference(
        identity_id=f"agent_{safe_id}",
        identity_type=IdentityType.AGENT,
        canonical_identifier=canonical_id,
        display_name=comp_clean,
    )


def create_oauth_user_identity(
    provider: str,
    user_id: str,
    display_name: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Identity:
    """
    Construye una Identity de tipo USER a partir de un sujeto OAuth externo (Hito E).
    ESTRICTAMENTE DESACOPLADO DE TOKENS: No acepta ni almacena access_token o refresh_token.
    """
    if not provider or not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string.")
    if not user_id or not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty string.")

    norm_provider = provider.strip().lower()
    norm_user_id = user_id.strip()

    # Identity_id determinista y seguro contra path traversal
    safe_provider = norm_provider.replace(":", "_").replace("/", "_").replace("\\", "_")
    safe_uid = norm_user_id.replace(":", "_").replace("/", "_").replace("\\", "_")
    identity_id = f"usr_{safe_provider}_{safe_uid}"

    canonical_id = build_canonical_identifier(
        identity_type=IdentityType.USER,
        provider=norm_provider,
        raw_identifier=norm_user_id,
    )

    now = datetime.now(timezone.utc)
    return Identity(
        identity_id=identity_id,
        identity_type=IdentityType.USER,
        canonical_identifier=canonical_id,
        display_name=display_name or f"{norm_provider}:{norm_user_id}",
        provider=norm_provider,
        external_subject_id=norm_user_id,
        status=IdentityStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        metadata=metadata or {},
    )
