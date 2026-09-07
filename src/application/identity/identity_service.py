"""
Servicio de Aplicación para la Gestión y Resolución de Identidad Canónica (Hito N.1).

Responsabilidades:
- Registro determinista e idempotente de identidades (USER, AGENT, SYSTEM, SERVICE, SCHEDULER, MARKETPLACE, EXTERNAL_TOOL).
- Resolución de identidad a partir de actores de auditoría (K.1), trazas operacionales (K.2) o sujetos externos (OAuth).
- Manejo explícito de identidades UNKNOWN (no inventa identidades para actores no reconocidos).
- Fronteras estrictas:
  * Responde "¿Quién es el actor?".
  * NO resuelve autenticación (N.2), autorización (N.3) ni permisos RBAC (N.4).
  * Cero almacenamiento o propagación de tokens o secretos.
"""

from datetime import datetime, timezone
import logging
from typing import Optional, Sequence, Mapping, Any, Union

from src.domain.identity.models import (
    Identity,
    IdentityType,
    IdentityStatus,
    IdentityReference,
    PrincipalIdentity,
    build_canonical_identifier,
    audit_actor_to_identity_reference,
    identity_to_audit_actor,
    agent_trace_to_identity_reference,
    create_oauth_user_identity,
)
from src.domain.identity.ports import IdentityRepositoryPort
from src.domain.audit.models import AuditActor
from src.domain.agent_trace.models import AgentTraceRecord
from src.domain.security.models import validate_safe_identifier, sanitize_security_data

logger = logging.getLogger(__name__)


class IdentityService:
    """
    Servicio de aplicación para el registro, consulta y resolución de Identidad Canónica (N.1).
    """

    def __init__(self, repository: IdentityRepositoryPort):
        self.repository = repository

    def register_identity(
        self,
        identity_id: str,
        identity_type: Union[IdentityType, str],
        canonical_identifier: Optional[str] = None,
        display_name: Optional[str] = None,
        provider: Optional[str] = None,
        external_subject_id: Optional[str] = None,
        status: Union[IdentityStatus, str] = IdentityStatus.ACTIVE,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Identity:
        """
        Registra una nueva identidad de forma atómica e idempotente.
        """
        validate_safe_identifier(identity_id, field_name="identity_id")

        id_type_enum = IdentityType(identity_type) if not isinstance(identity_type, IdentityType) else identity_type
        id_status_enum = IdentityStatus(status) if not isinstance(status, IdentityStatus) else status

        if not canonical_identifier:
            raw_id = external_subject_id or identity_id
            canonical_identifier = build_canonical_identifier(
                identity_type=id_type_enum,
                provider=provider,
                raw_identifier=raw_id,
            )

        now = datetime.now(timezone.utc)
        identity = Identity(
            identity_id=identity_id,
            identity_type=id_type_enum,
            canonical_identifier=canonical_identifier,
            display_name=display_name,
            provider=provider,
            external_subject_id=external_subject_id,
            status=id_status_enum,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

        return self.repository.save_identity(identity)

    def register_oauth_user(
        self,
        provider: str,
        user_id: str,
        display_name: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Identity:
        """
        Registra o recupera un usuario a partir de su identidad OAuth de forma determinista y estable.
        No acepta ni almacena access_token ni refresh_token.
        """
        existing = self.repository.find_by_external_subject(provider=provider, external_subject_id=user_id)
        if existing:
            return existing

        identity = create_oauth_user_identity(
            provider=provider,
            user_id=user_id,
            display_name=display_name,
            metadata=metadata,
        )
        return self.repository.save_identity(identity)

    def register_system_agent(
        self,
        agent_name: str,
        display_name: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Identity:
        """
        Registra un agente autónomo o servicio interno con identidad canónica y estable.
        La identidad del agente no varía entre ejecuciones o ciclos.
        """
        clean_name = agent_name.strip()
        safe_id = clean_name.replace(":", "_").replace("/", "_").replace("\\", "_")
        identity_id = f"agent_{safe_id}"

        canonical_id = build_canonical_identifier(
            identity_type=IdentityType.AGENT,
            provider="internal",
            raw_identifier=clean_name,
        )

        existing = self.repository.get_identity(identity_id)
        if existing:
            return existing

        now = datetime.now(timezone.utc)
        identity = Identity(
            identity_id=identity_id,
            identity_type=IdentityType.AGENT,
            canonical_identifier=canonical_id,
            display_name=display_name or clean_name,
            provider="internal",
            external_subject_id=clean_name,
            status=IdentityStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        return self.repository.save_identity(identity)

    def get_identity(self, identity_id: str) -> Optional[Identity]:
        """Obtiene una identidad por ID."""
        return self.repository.get_identity(identity_id)

    def resolve_identity(self, raw_reference: Union[str, AuditActor, AgentTraceRecord, None]) -> IdentityReference:
        """
        Resuelve una referencia cruda, AuditActor o AgentTraceRecord hacia un IdentityReference canónico.
        Si la referencia no es reconocible o está vacía, retorna un IdentityReference de tipo UNKNOWN.
        """
        if raw_reference is None:
            return IdentityReference(
                identity_id="unknown_actor",
                identity_type=IdentityType.UNKNOWN,
                canonical_identifier="unknown:internal:unresolved",
                display_name="Unknown Actor",
            )

        if isinstance(raw_reference, AuditActor):
            # Si el actor_id existe en el repositorio de identidades, obtener sus datos
            existing = self.repository.get_identity(raw_reference.actor_id)
            if existing:
                return existing.to_reference()
            return audit_actor_to_identity_reference(raw_reference)

        if isinstance(raw_reference, AgentTraceRecord):
            comp_id = f"agent_{raw_reference.component_name.strip().replace(':', '_')}"
            existing = self.repository.get_identity(comp_id)
            if existing:
                return existing.to_reference()
            return agent_trace_to_identity_reference(raw_reference)

        if isinstance(raw_reference, str):
            clean_ref = raw_reference.strip()
            if not clean_ref or clean_ref.lower() in ("unknown", "none", "null", ""):
                return IdentityReference(
                    identity_id="unknown_actor",
                    identity_type=IdentityType.UNKNOWN,
                    canonical_identifier="unknown:internal:unresolved",
                    display_name="Unknown Actor",
                )

            # Si contiene separadores como ":" es un canonical_identifier candidato
            if ":" in clean_ref:
                existing_by_canonical = self.repository.find_by_canonical_identifier(clean_ref)
                if existing_by_canonical:
                    return existing_by_canonical.to_reference()
            else:
                try:
                    existing = self.repository.get_identity(clean_ref)
                    if existing:
                        return existing.to_reference()
                except Exception:
                    pass

            # No se encuentra en el registro
            safe_id = clean_ref.replace(":", "_").replace("/", "_").replace("\\", "_")
            return IdentityReference(
                identity_id=f"id_{safe_id}",
                identity_type=IdentityType.UNKNOWN,
                canonical_identifier=f"unknown:unregistered:{clean_ref.lower()}",
                display_name=clean_ref,
            )

        return IdentityReference(
            identity_id="unknown_actor",
            identity_type=IdentityType.UNKNOWN,
            canonical_identifier="unknown:internal:unresolved",
            display_name="Unknown Actor",
        )

    def list_identities(
        self,
        identity_type: Optional[IdentityType] = None,
        provider: Optional[str] = None,
        status: Optional[IdentityStatus] = None,
        limit: int = 100,
    ) -> Sequence[Identity]:
        """Lista identidades registradas."""
        return self.repository.list_identities(
            identity_type=identity_type,
            provider=provider,
            status=status,
            limit=limit,
        )
