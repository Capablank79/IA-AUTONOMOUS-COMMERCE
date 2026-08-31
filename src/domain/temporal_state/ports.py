from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List
from .models import TemporalSnapshot


class TemporalStateRepository(ABC):
    """
    Puerto secundario para persistir y reconstruir snapshots de estado en una línea de tiempo.
    """

    @abstractmethod
    def save_snapshot(self, snapshot: TemporalSnapshot) -> None:
        """
        Persiste un snapshot temporal de estado.
        """
        pass

    @abstractmethod
    def get_snapshot_by_id(self, snapshot_id: str) -> Optional[TemporalSnapshot]:
        """
        Recupera un snapshot específico por su id.
        """
        pass

    @abstractmethod
    def get_history_for_entity(self, entity_type: str, entity_id: str) -> List[TemporalSnapshot]:
        """
        Recupera la historia cronológica completa de snapshots para una entidad.
        """
        pass

    @abstractmethod
    def get_state_at(self, entity_type: str, entity_id: str, timestamp: datetime) -> Optional[TemporalSnapshot]:
        """
        Reconstruye el estado más reciente de la entidad en o antes del punto temporal especificado T.
        """
        pass
