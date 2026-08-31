from abc import ABC, abstractmethod
from typing import Optional, List
from .models import ActionRecord


class ActionRepository(ABC):
    """
    Puerto secundario (interface de persistencia) para guardar y recuperar ActionRecord.
    """

    @abstractmethod
    def save(self, action: ActionRecord) -> None:
        """
        Guarda un registro de acción. Si ya existe, actualiza su estado/versión.
        """
        pass

    @abstractmethod
    def get_by_id(self, action_id: str) -> Optional[ActionRecord]:
        """
        Recupera una acción por su identidad única.
        """
        pass

    @abstractmethod
    def get_by_decision_id(self, decision_id: str) -> List[ActionRecord]:
        """
        Recupera todas las acciones asociadas a una decisión específica.
        """
        pass

    @abstractmethod
    def get_by_mission_id(self, mission_id: str) -> List[ActionRecord]:
        """
        Recupera todas las acciones asociadas a una misión específica.
        """
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ActionRecord]:
        """
        Recupera una acción por su clave de idempotencia.
        """
        pass

    @abstractmethod
    def exists(self, action_id: str) -> bool:
        """
        Verifica la existencia de una acción por ID.
        """
        pass
