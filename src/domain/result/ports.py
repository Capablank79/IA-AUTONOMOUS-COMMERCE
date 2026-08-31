from abc import ABC, abstractmethod
from typing import Optional, List
from .models import ActionResultRecord


class ResultRepository(ABC):
    """
    Puerto secundario (interface de persistencia) para guardar y recuperar ActionResultRecord.
    """

    @abstractmethod
    def save(self, result: ActionResultRecord) -> None:
        """
        Guarda un registro de resultado de acción.
        """
        pass

    @abstractmethod
    def get_by_id(self, result_id: str) -> Optional[ActionResultRecord]:
        """
        Recupera un resultado por su identidad única.
        """
        pass

    @abstractmethod
    def get_by_action_id(self, action_id: str) -> Optional[ActionResultRecord]:
        """
        Recupera el resultado asociado a una acción específica.
        """
        pass

    @abstractmethod
    def get_by_decision_id(self, decision_id: str) -> List[ActionResultRecord]:
        """
        Recupera todos los resultados asociados a una decisión específica.
        """
        pass

    @abstractmethod
    def get_by_mission_id(self, mission_id: str) -> List[ActionResultRecord]:
        """
        Recupera todos los resultados asociados a una misión específica.
        """
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ActionResultRecord]:
        """
        Recupera un resultado por su clave de idempotencia.
        """
        pass

    @abstractmethod
    def exists(self, result_id: str) -> bool:
        """
        Verifica la existencia de un resultado por ID.
        """
        pass
