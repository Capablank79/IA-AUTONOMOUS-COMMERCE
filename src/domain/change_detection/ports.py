"""
Puertos de Dominio para Detección de Cambios (Change Detection - Hito J.4).
"""

from typing import List, Optional, Protocol, Tuple, Any
from .models import ChangeRecord, ChangeSubjectType


class ChangeDetectionEnginePort(Protocol):
    """
    Puerto de Dominio para el motor de detección de cambios determinista.
    Compara estados/observaciones T0 vs T1 respetando orden temporal,
    desacoplamiento de contratos y preservación de UNKNOWN.
    """
    def compare_observations(
        self,
        previous_observation: Optional[Any],
        current_observation: Any,
        correlation_id: Optional[str] = None,
        metadata: Optional[Any] = None,
    ) -> ChangeRecord:
        """Compara dos MarketObservation consecutivas."""
        ...

    def compare_opportunities(
        self,
        previous_opportunity: Optional[Any],
        current_opportunity: Any,
        correlation_id: Optional[str] = None,
    ) -> ChangeRecord:
        """Compara dos OpportunityRecord consecutivas."""
        ...


class ChangeRecordRepositoryPort(Protocol):
    """
    Puerto de Dominio para la persistencia durable de registros de cambio (ChangeRecord).
    Garantiza atomicidad, idempotencia, sanitización de secretos y resiliencia ante reinicio.
    """
    def save(self, change_record: ChangeRecord) -> None:
        """Persiste un ChangeRecord de forma atómica e idempotente."""
        ...

    def save_all(self, change_records: List[ChangeRecord]) -> int:
        """Persiste una lista de ChangeRecord y retorna la cantidad de nuevos registros guardados."""
        ...

    def get_by_id(self, change_id: str) -> Optional[ChangeRecord]:
        """Obtiene un ChangeRecord por su ID primario."""
        ...

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ChangeRecord]:
        """Obtiene un ChangeRecord por su clave determinista de idempotencia."""
        ...

    def list_by_subject(
        self,
        subject_type: ChangeSubjectType,
        subject_id: str,
        limit: int = 100,
    ) -> List[ChangeRecord]:
        """Lista los registros de cambio para un sujeto ordenados cronológicamente."""
        ...

    def list_all(self, limit: int = 1000) -> List[ChangeRecord]:
        """Lista todos los registros de cambio persistidos."""
        ...
