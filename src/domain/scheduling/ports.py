from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from .models import Schedule, ScheduleOccurrence, ExecutionStatus


class ScheduleRepository(ABC):
    """Puerto secundario para la persistencia durable de Schedules y Occurrences."""

    @abstractmethod
    def save(self, schedule: Schedule) -> None:
        pass

    @abstractmethod
    def get_by_id(self, schedule_id: str) -> Optional[Schedule]:
        pass

    @abstractmethod
    def list_all(self) -> List[Schedule]:
        pass

    @abstractmethod
    def list_due(self, current_time: datetime) -> List[Schedule]:
        pass

    @abstractmethod
    def delete(self, schedule_id: str) -> bool:
        pass

    @abstractmethod
    def save_occurrence(self, occurrence: ScheduleOccurrence) -> None:
        pass

    @abstractmethod
    def get_occurrence(self, occurrence_id: str) -> Optional[ScheduleOccurrence]:
        pass

    @abstractmethod
    def get_occurrence_by_idempotency_key(self, idempotency_key: str) -> Optional[ScheduleOccurrence]:
        pass

    @abstractmethod
    def list_occurrences(self, schedule_id: Optional[str] = None) -> List[ScheduleOccurrence]:
        pass


class MissionTriggerPort(ABC):
    """
    Puerto desacoplado para disparar misiones existentes desde un schedule occurrence.
    No contiene lógica de negocio ni dependencias de marketplace.
    """

    @abstractmethod
    def trigger(self, schedule: Schedule, occurrence: ScheduleOccurrence) -> Tuple[str, ExecutionStatus, Optional[Dict[str, Any]], Optional[str]]:
        """
        Dispara la misión asociada al schedule.
        Retorna: (mission_id, ExecutionStatus, result_summary, error_message)
        Preserva fielmente el estado UNKNOWN si la misión o el orquestador reportan incertidumbre o bloqueo.
        """
        pass
