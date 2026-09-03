"""
Puertos de dominio para Data Provenance (Hito L.2).

Define:
- ProvenanceRepositoryPort: Contrato para almacenamiento, consulta e indexación de registros de procedencia.
"""

from typing import Protocol, Optional, Sequence, List
from .models import ProvenanceRecord, SubjectType


class ProvenanceRepositoryPort(Protocol):
    """
    Puerto de repositorio para la persistencia e indexación de registros de procedencia (linaje de datos).
    """

    def save_provenance(self, record: ProvenanceRecord) -> ProvenanceRecord:
        """
        Persiste un ProvenanceRecord de forma atómica e idempotente.
        Lanza excepción ante colisión/conflicto de contenido bajo el mismo provenance_id.
        """
        ...

    def get_provenance(self, provenance_id: str) -> Optional[ProvenanceRecord]:
        """
        Obtiene un registro de procedencia por su provenance_id exacto.
        Verifica integridad criptográfica SHA-256 en lectura.
        """
        ...

    def find_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[SubjectType] = None,
        field_path: Optional[str] = None,
    ) -> Sequence[ProvenanceRecord]:
        """
        Busca registros de procedencia asociados a un sujeto/entidad de negocio
        (opcionalmente filtrando por tipo de sujeto y/o campo específico).
        """
        ...

    def find_by_source(
        self,
        source_id: str,
        limit: int = 100,
    ) -> Sequence[ProvenanceRecord]:
        """
        Busca registros de procedencia que referencian directamente una fuente L.1.
        """
        ...

    def find_by_evidence(
        self,
        evidence_id: str,
    ) -> Sequence[ProvenanceRecord]:
        """
        Busca registros de procedencia asociados a una evidencia específica.
        """
        ...

    def exists(self, provenance_id: str) -> bool:
        """
        Verifica si existe un registro de procedencia con el provenance_id provisto.
        """
        ...
