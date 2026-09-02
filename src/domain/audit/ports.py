"""
Puertos de dominio para el Registro de Auditoría (Audit Trail - Hito K.1).

Define los contratos abstractos para:
- AuditRepositoryPort: Persistencia append-only, consultas indexadas y reconstrucción de timeline.
- AuditRecordPublisherPort: Publicación/despacho desacoplado de hechos de auditoría.
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from datetime import datetime

from src.domain.audit.models import AuditRecord, AuditRecordType, MissionAuditTimeline


class AuditRepositoryPort(ABC):
    """
    Puerto secundario de persistencia para registros de auditoría (Append-only).
    """

    @abstractmethod
    def append(self, record: AuditRecord) -> AuditRecord:
        """
        Persiste un AuditRecord de forma atómica e inmutable.
        Si ya existe un registro con el mismo audit_id o idempotency_key, retorna el registro existente
        garantizando idempotencia de replay y semántica append-only.
        """
        pass

    @abstractmethod
    def get_by_id(self, audit_id: str) -> Optional[AuditRecord]:
        """Obtiene un AuditRecord por su identificador único."""
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AuditRecord]:
        """Obtiene un AuditRecord por su clave determinista de idempotencia."""
        pass

    @abstractmethod
    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        """
        Consulta registros de auditoría ordenados cronológicamente por occurred_at
        (con desempate determinista por audit_id).
        """
        pass

    @abstractmethod
    def reconstruct_mission_timeline(self, mission_id: str) -> MissionAuditTimeline:
        """
        Reconstruye el timeline cronológico y causal inmutable de una misión específica.
        """
        pass
