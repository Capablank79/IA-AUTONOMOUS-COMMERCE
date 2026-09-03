"""
Servicio de Aplicación para Source Registry (Hito L.1 - Transversal Data Quality / Governance).

Responsabilidades:
- Validar y canonicalizar identidad de fuentes de datos.
- Registrar fuentes de forma determinista e idempotente.
- Detectar y prevenir conflictos de versiones o colisiones de identificadores canónicos.
- Registrar eventos de auditoría (K.1 Audit Trail) ante registros nuevos o conflictos cuando el servicio de auditoría esté disponible.
- Ofrecer métodos de resolución e inspección de catálogo de fuentes.
- Mantener fronteras estrictas de L.1: NO calcula freshness (L.3), confidence (L.4) ni data provenance (L.2).
"""

from datetime import datetime, timezone
import logging
from typing import Optional, Sequence, Mapping, Any, Dict, Union
import uuid

from src.domain.source_registry.models import (
    RegisteredSource,
    SourceType,
    SourceStatus,
    build_canonical_identifier,
    compute_source_checksum,
    sanitize_endpoint_reference,
)
from src.domain.source_registry.ports import SourceRegistryRepositoryPort
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


class SourceRegistryServiceError(Exception):
    """Excepción base para el servicio de Source Registry."""
    pass


class SourceConflictException(SourceRegistryServiceError):
    """Conflicto detectado durante el registro de una fuente."""
    pass


class SourceRegistryService:
    """
    Servicio de aplicación para el catálogo y gobierno de fuentes canónicas de datos (L.1).
    """

    def __init__(
        self,
        repository: SourceRegistryRepositoryPort,
        audit_service: Optional[Any] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
    ):
        self.repository = repository
        self.audit_service = audit_service
        self.audit_repository = audit_repository

    def register_source(
        self,
        source_id: str,
        name: str,
        source_type: Union[SourceType, str],
        provider: str,
        canonical_identifier: Optional[str] = None,
        description: Optional[str] = None,
        endpoint_reference: Optional[str] = None,
        status: Union[SourceStatus, str] = SourceStatus.ACTIVE,
        version: str = "1.0.0",
        schema_version: str = "1.0.0",
        metadata: Optional[Mapping[str, Any]] = None,
        actor: Optional[AuditActor] = None,
        correlation_id: Optional[str] = None,
    ) -> RegisteredSource:
        """
        Registra una fuente de datos en el catálogo canónico.
        Es idempotente si los datos son idénticos.
        Lanza SourceConflictException si hay colisión o discrepancia de contenido.
        """
        # 1. Validar identificadores
        validate_safe_identifier(source_id, field_name="source_id")
        validate_safe_identifier(version, field_name="version")

        # 2. Normalizar tipo y status
        st_enum = SourceType(source_type) if isinstance(source_type, str) else source_type
        stat_enum = SourceStatus(status) if isinstance(status, str) else status

        # 3. Canonical identifier
        final_canonical_id = canonical_identifier or build_canonical_identifier(
            source_type=st_enum,
            provider=provider,
            raw_identifier=source_id,
        )

        now = datetime.now(timezone.utc)

        # 4. Construir objeto inmutable RegisteredSource
        source = RegisteredSource(
            source_id=source_id,
            name=name,
            source_type=st_enum,
            provider=provider,
            canonical_identifier=final_canonical_id,
            description=description,
            endpoint_reference=endpoint_reference,
            status=stat_enum,
            version=version,
            schema_version=schema_version,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

        # 5. Comprobar si ya existe una versión previa exacta
        existing = self.repository.get_source(source_id=source_id, version=version)
        if existing:
            if existing.checksum == source.checksum:
                # Replay idempotente: retornar existente sin duplicar auditoría
                return existing
            else:
                self._record_audit_event(
                    action="SOURCE_CONFLICT",
                    subject_id=source_id,
                    status="CONFLICT",
                    actor=actor,
                    correlation_id=correlation_id,
                    details={
                        "reason": "Checksum mismatch on existing version",
                        "existing_checksum": existing.checksum,
                        "attempted_checksum": source.checksum,
                    },
                )
                raise SourceConflictException(
                    f"Conflict registering source '{source_id}' version '{version}': "
                    f"Existing checksum '{existing.checksum}' != attempted '{source.checksum}'"
                )

        # 6. Comprobar colisión por canonical_identifier
        existing_canonical = self.repository.find_by_canonical_identifier(final_canonical_id)
        if existing_canonical and existing_canonical.source_id != source_id:
            self._record_audit_event(
                action="SOURCE_CONFLICT",
                subject_id=source_id,
                status="CONFLICT",
                actor=actor,
                correlation_id=correlation_id,
                details={
                    "reason": "Canonical identifier collision",
                    "canonical_identifier": final_canonical_id,
                    "existing_source_id": existing_canonical.source_id,
                },
            )
            raise SourceConflictException(
                f"Canonical identifier '{final_canonical_id}' already assigned to source '{existing_canonical.source_id}'"
            )

        # 7. Persistir
        saved_source = self.repository.save_source(source)

        # 8. Registrar evento en Audit Trail si aplica
        self._record_audit_event(
            action="SOURCE_REGISTERED",
            subject_id=source_id,
            status="SUCCESS",
            actor=actor,
            correlation_id=correlation_id,
            details={
                "source_type": saved_source.source_type.value,
                "provider": saved_source.provider,
                "canonical_identifier": saved_source.canonical_identifier,
                "version": saved_source.version,
                "checksum": saved_source.checksum,
            },
        )

        return saved_source

    def get_source(self, source_id: str, version: Optional[str] = None) -> Optional[RegisteredSource]:
        """Obtiene una fuente por ID y versión opcional."""
        return self.repository.get_source(source_id=source_id, version=version)

    def find_by_canonical_identifier(self, canonical_identifier: str) -> Optional[RegisteredSource]:
        """Busca una fuente por su identificador canónico."""
        return self.repository.find_by_canonical_identifier(canonical_identifier)

    def list_sources(
        self,
        source_type: Optional[SourceType] = None,
        provider: Optional[str] = None,
        status: Optional[SourceStatus] = None,
        limit: int = 100,
    ) -> Sequence[RegisteredSource]:
        """Lista fuentes registradas con filtros opcionales."""
        return self.repository.list_sources(
            source_type=source_type,
            provider=provider,
            status=status,
            limit=limit,
        )

    def exists(self, source_id: str, version: Optional[str] = None) -> bool:
        """Verifica la existencia de una fuente registrada."""
        return self.repository.exists(source_id=source_id, version=version)

    def _record_audit_event(
        self,
        action: str,
        subject_id: str,
        status: str,
        details: Dict[str, Any],
        actor: Optional[AuditActor] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        """Registra un evento de auditoría de forma no intrusiva y desacoplada."""
        if not self.audit_repository and not self.audit_service:
            return

        act = actor or AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="source-registry-service")
        corr_id = correlation_id or f"corr-sr-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)

        try:
            # Reutilizar AuditRecordType.ACTION_EXECUTED o POLICY_EVALUATED
            record = AuditRecord(
                audit_id=f"aud-sr-{uuid.uuid4().hex[:12]}",
                record_type=AuditRecordType.ACTION_EXECUTED,
                occurred_at=now,
                actor=act,
                subject_type="SOURCE_REGISTRY",
                subject_id=subject_id,
                action_or_operation=action,
                status=status,
                correlation_id=corr_id,
                entity_reference=subject_id,
                metadata=details,
            )

            if self.audit_repository:
                if hasattr(self.audit_repository, "append"):
                    self.audit_repository.append(record)
                elif hasattr(self.audit_repository, "save_record"):
                    self.audit_repository.save_record(record)
            elif hasattr(self.audit_service, "audit_repository") and self.audit_service.audit_repository:
                if hasattr(self.audit_service.audit_repository, "append"):
                    self.audit_service.audit_repository.append(record)
                elif hasattr(self.audit_service.audit_repository, "save_record"):
                    self.audit_service.audit_repository.save_record(record)
        except Exception as e:
            logger.warning(f"Failed to record source registry audit event: {e}")
