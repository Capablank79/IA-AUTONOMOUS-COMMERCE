from abc import ABC, abstractmethod
from typing import Optional, List
from .models import DecisionRecord, DecisionType, DecisionStatus

class DecisionRepository(ABC):
    """
    Puerto secundario (interface de persistencia) para guardar y recuperar DecisionRecord.
    Sigue la firma y estándar exacto del patrón Hexagonal usado en MissionRepository.
    """

    @abstractmethod
    def save(self, decision: DecisionRecord) -> None:
        """
        Guarda un registro de decisión. Si ya existe, actualiza su estado/versión.
        """
        pass

    @abstractmethod
    def get_by_id(self, decision_id: str) -> Optional[DecisionRecord]:
        """
        Recupera una decisión por su identidad única.
        """
        pass

    @abstractmethod
    def get_by_mission_id(self, mission_id: str) -> List[DecisionRecord]:
        """
        Recupera todas las decisiones asociadas a una misión específica.
        """
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[DecisionRecord]:
        """
        Recupera una decisión por su clave de idempotencia.
        """
        pass

    @abstractmethod
    def exists(self, decision_id: str) -> bool:
        """
        Verifica la existencia de una decisión.
        """
        pass
