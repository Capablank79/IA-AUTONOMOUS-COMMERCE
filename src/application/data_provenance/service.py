"""
Servicio de Aplicación para Data Provenance (Hito L.2 - Transversal Data Quality / Governance).

Responsabilidades:
- Validar existencia de la fuente en L.1 Source Registry (rechazo explícito de source_id desconocido).
- Registrar linaje de datos de forma determinista e idempotente.
- Detección de ciclos y auto-referencias en el DAG de parent_provenance_ids.
- Soporte para linaje directo (SOURCE -> DIRECT FACT) y derivado (SOURCE FACTS -> TRANSFORMATION -> DERIVED FACT).
- Soporte para procedencia a nivel de campo (field-level provenance).
- Detección explícita de conflictos ante diferente contenido/checksum para la misma clave lógica.
- Trazabilidad y reconstrucción recursiva determinista del linaje hasta las fuentes raíz (trace_to_sources).
- Emisión desacoplada de eventos de auditoría (K.1 Audit Trail) ante registro de provenance o conflictos detectados.
- Fronteras estrictas: responde "¿de qué fuente y evidencia concreta proviene este dato?".
  NO calcula freshness (L.3), confidence (L.4), schema validation (L.5), entity resolution (L.6), duplicate detection (L.7) ni conflict resolution (L.8).
"""

from datetime import datetime, timezone
import logging
from typing import Optional, Sequence, Mapping, Any, Dict, Union, List, Set, Tuple
import uuid

from src.domain.data_provenance.models import (
    ProvenanceRecord,
    SubjectType,
    SourceLineageTrace,
    generate_deterministic_provenance_id,
    compute_provenance_checksum,
)
from src.domain.data_provenance.ports import ProvenanceRepositoryPort
from src.domain.source_registry.ports import SourceRegistryRepositoryPort
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


class DataProvenanceServiceError(Exception):
    """Excepción base para el servicio de Data Provenance."""
    pass


class UnknownSourceError(DataProvenanceServiceError):
    """Se lanza cuando un provenance_id intenta referenciar una fuente no registrada en L.1."""
    pass


class ProvenanceCycleError(DataProvenanceServiceError):
    """Se lanza cuando se detecta un ciclo en la jerarquía de padres de procedencia."""
    pass


class MissingParentProvenanceError(DataProvenanceServiceError):
    """Se lanza cuando un parent_provenance_id requerido no existe en el repositorio."""
    pass


class ProvenanceConflictServiceError(DataProvenanceServiceError):
    """Conflicto detectado durante el registro de procedencia."""
    pass


class DataProvenanceService:
    """
    Servicio de aplicación para el gobierno del linaje y procedencia de datos (L.2).
    """

    def __init__(
        self,
        repository: ProvenanceRepositoryPort,
        source_registry_repository: Optional[SourceRegistryRepositoryPort] = None,
        audit_service: Optional[Any] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
    ):
        self.repository = repository
        self.source_registry_repository = source_registry_repository
        self.audit_service = audit_service
        self.audit_repository = audit_repository

    def record_provenance(
        self,
        source_id: str,
        subject_type: Union[SubjectType, str],
        subject_id: str,
        provenance_id: Optional[str] = None,
        source_version: str = "1.0.0",
        source_record_id: Optional[str] = None,
        evidence_id: Optional[str] = None,
        field_path: Optional[str] = None,
        parent_provenance_ids: Sequence[str] = (),
        transformation_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        captured_at: Optional[datetime] = None,
        schema_version: str = "1.0.0",
        metadata: Optional[Mapping[str, Any]] = None,
        actor: Optional[AuditActor] = None,
        allow_unregistered_source: bool = False,
    ) -> ProvenanceRecord:
        """
        Registra el linaje de un dato o hecho de negocio.
        Valida que source_id exista en L.1 Source Registry (a menos que se permita explícitamente).
        Valida que no existan ciclos con parent_provenance_ids.
        Genera un provenance_id determinista si no se especificó.
        """
        # 1. Validar existencia en L.1 Source Registry
        if self.source_registry_repository is not None and not allow_unregistered_source:
            if not self.source_registry_repository.exists(source_id):
                raise UnknownSourceError(
                    f"Cannot record provenance for unregistered source '{source_id}'. "
                    f"Source must be registered in L.1 Source Registry first."
                )

        # 2. Convertir y validar subject_type
        if isinstance(subject_type, str):
            try:
                subj_type_enum = SubjectType(subject_type)
            except ValueError as e:
                raise ValueError(f"Invalid subject_type: {subject_type}") from e
        else:
            subj_type_enum = subject_type

        # 3. Normalizar parents y verificar existencia / ciclos
        clean_parents: List[str] = []
        for p in parent_provenance_ids:
            if p and isinstance(p, str) and p.strip():
                clean_parents.append(p.strip())

        # 4. Generar o validar provenance_id
        if not provenance_id:
            resolved_prov_id = generate_deterministic_provenance_id(
                source_id=source_id,
                subject_type=subj_type_enum,
                subject_id=subject_id,
                field_path=field_path,
                evidence_id=evidence_id,
                source_record_id=source_record_id,
                parent_provenance_ids=clean_parents,
                transformation_id=transformation_id,
            )
        else:
            resolved_prov_id = provenance_id.strip()
            validate_safe_identifier(resolved_prov_id, field_name="provenance_id")

        # 5. Validar ciclo directo o indirecto con padres
        if resolved_prov_id in clean_parents:
            raise ProvenanceCycleError(
                f"Self-parenting cycle detected: provenance '{resolved_prov_id}' cannot be its own parent."
            )

        for pid in clean_parents:
            self._verify_no_cycles(candidate_id=resolved_prov_id, current_parent_id=pid, visited=set())

        # 6. Preparar timestamp y correlación
        now = datetime.now(timezone.utc)
        record_captured_at = captured_at if captured_at is not None else now
        if record_captured_at.tzinfo is None:
            record_captured_at = record_captured_at.replace(tzinfo=timezone.utc)

        corr_id = correlation_id or f"corr-{resolved_prov_id}"

        # 7. Construir entidad de dominio inmutable
        record = ProvenanceRecord(
            provenance_id=resolved_prov_id,
            source_id=source_id,
            subject_type=subj_type_enum,
            subject_id=subject_id,
            captured_at=record_captured_at,
            source_version=source_version,
            source_record_id=source_record_id,
            evidence_id=evidence_id,
            field_path=field_path,
            parent_provenance_ids=tuple(clean_parents),
            transformation_id=transformation_id,
            correlation_id=corr_id,
            causation_id=causation_id,
            schema_version=schema_version,
            metadata=dict(metadata or {}),
        )

        # 8. Persistir
        try:
            saved_record = self.repository.save_provenance(record)
        except Exception as e:
            # Emitir auditoría de conflicto si es relevante
            self._emit_audit_event(
                event_type="PROVENANCE_CONFLICT_DETECTED",
                record=record,
                actor=actor,
                success=False,
                error_msg=str(e),
            )
            raise ProvenanceConflictServiceError(f"Failed to record provenance: {e}") from e

        # 9. Emitir auditoría de registro exitoso (K.1 Audit Trail)
        self._emit_audit_event(
            event_type="DATA_PROVENANCE_RECORDED",
            record=saved_record,
            actor=actor,
            success=True,
        )

        return saved_record

    def get_provenance(self, provenance_id: str) -> Optional[ProvenanceRecord]:
        """Obtiene un registro de procedencia por su provenance_id exacto."""
        validate_safe_identifier(provenance_id, field_name="provenance_id")
        return self.repository.get_provenance(provenance_id)

    def find_for_subject(
        self,
        subject_id: str,
        subject_type: Optional[Union[SubjectType, str]] = None,
        field_path: Optional[str] = None,
    ) -> Sequence[ProvenanceRecord]:
        """Busca registros de procedencia asociados a un sujeto/entidad."""
        validate_safe_identifier(subject_id, field_name="subject_id")
        st_enum = None
        if subject_type is not None:
            st_enum = SubjectType(subject_type) if isinstance(subject_type, str) else subject_type
        return self.repository.find_by_subject(
            subject_id=subject_id,
            subject_type=st_enum,
            field_path=field_path,
        )

    def trace_to_sources(
        self,
        provenance_id: Optional[str] = None,
        subject_id: Optional[str] = None,
        field_path: Optional[str] = None,
        subject_type: Optional[Union[SubjectType, str]] = None,
    ) -> SourceLineageTrace:
        """
        Reconstruye recursivamente el grafo DAG de linaje de datos hacia atrás
        hasta identificar todas las fuentes raíz (root source_ids de L.1).
        Detecta ciclos y dependencias faltantes sin devolver linajes falsos.
        """
        target_record: Optional[ProvenanceRecord] = None

        if provenance_id:
            target_record = self.get_provenance(provenance_id)
            if not target_record:
                raise ValueError(f"ProvenanceRecord with id '{provenance_id}' not found.")
        elif subject_id:
            records = self.find_for_subject(
                subject_id=subject_id,
                subject_type=subject_type,
                field_path=field_path,
            )
            if not records:
                raise ValueError(f"No provenance records found for subject '{subject_id}'.")
            target_record = records[0]
        else:
            raise ValueError("Either provenance_id or subject_id must be provided to trace lineage.")

        # Recorrer grafo DAG hacia atrás
        records_collected: Dict[str, ProvenanceRecord] = {target_record.provenance_id: target_record}
        root_sources: Set[str] = set()
        unresolved_parents: Set[str] = set()
        visited: Set[str] = set()

        def _traverse(current_rec: ProvenanceRecord, path: List[str]):
            curr_id = current_rec.provenance_id
            if curr_id in path:
                raise ProvenanceCycleError(
                    f"Cycle detected in lineage DAG traversal: {' -> '.join(path + [curr_id])}"
                )

            new_path = path + [curr_id]

            if not current_rec.parent_provenance_ids:
                # Nodo raíz
                root_sources.add(current_rec.source_id)
                return

            for parent_id in current_rec.parent_provenance_ids:
                parent_rec = self.get_provenance(parent_id)
                if not parent_rec:
                    unresolved_parents.add(parent_id)
                    # Si no se encuentra el padre, agregamos la fuente del registro actual si es conocida
                    continue
                records_collected[parent_id] = parent_rec
                _traverse(parent_rec, new_path)

        _traverse(target_record, [])

        is_complete = (len(unresolved_parents) == 0)

        return SourceLineageTrace(
            target_provenance_id=target_record.provenance_id,
            subject_type=target_record.subject_type,
            subject_id=target_record.subject_id,
            field_path=target_record.field_path,
            root_source_ids=tuple(sorted(root_sources)),
            records_in_lineage=tuple(records_collected.values()),
            is_complete=is_complete,
            unresolved_parent_ids=tuple(sorted(unresolved_parents)),
        )

    def _verify_no_cycles(self, candidate_id: str, current_parent_id: str, visited: Set[str]) -> None:
        """Verifica que candidate_id no aparezca como ancestro de current_parent_id."""
        if current_parent_id in visited:
            return
        visited.add(current_parent_id)

        if current_parent_id == candidate_id:
            raise ProvenanceCycleError(
                f"Cycle detected: '{candidate_id}' is already an ancestor in parent chain '{current_parent_id}'."
            )

        parent_rec = self.repository.get_provenance(current_parent_id)
        if parent_rec and parent_rec.parent_provenance_ids:
            for p in parent_rec.parent_provenance_ids:
                self._verify_no_cycles(candidate_id, p, visited)

    def _emit_audit_event(
        self,
        event_type: str,
        record: ProvenanceRecord,
        actor: Optional[AuditActor],
        success: bool = True,
        error_msg: Optional[str] = None,
    ) -> None:
        """Emite evento de auditoría K.1 si audit_service o audit_repository están configurados."""
        if not self.audit_service and not self.audit_repository:
            return

        effective_actor = actor or AuditActor(
            actor_type=AuditActorType.SYSTEM,
            actor_id="data-provenance-service",
            details={"role": "DATA_GOVERNANCE"},
        )

        audit_rec = AuditRecord(
            audit_id=f"aud-prov-{uuid.uuid4().hex[:12]}",
            record_type=AuditRecordType.ACTION_EXECUTED,
            occurred_at=datetime.now(timezone.utc),
            actor=effective_actor,
            subject_type="DATA_PROVENANCE",
            subject_id=record.provenance_id,
            action_or_operation=event_type,
            status="SUCCESS" if success else "FAILED",
            correlation_id=record.correlation_id,
            causation_id=record.causation_id,
            mission_id=record.metadata.get("mission_id"),
            evidence_reference=record.evidence_id,
            metadata={
                "provenance_id": record.provenance_id,
                "source_id": record.source_id,
                "subject_type": record.subject_type.value,
                "subject_id": record.subject_id,
                "field_path": record.field_path,
                "is_derived": record.is_derived,
                "error": error_msg,
            },
        )

        try:
            if self.audit_repository:
                self.audit_repository.save_record(audit_rec)
            elif self.audit_service and hasattr(self.audit_service, "record_audit"):
                self.audit_service.record_audit(audit_rec)
        except Exception as e:
            logger.warning(f"Failed to emit audit record for provenance '{record.provenance_id}': {e}")
