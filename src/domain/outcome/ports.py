from abc import ABC, abstractmethod
from typing import Optional, List
from .models import OutcomeRecord


class OutcomeRepository(ABC):
    """
    Puerto secundario (interfaz de persistencia) para guardar y consultar OutcomeRecord.
    """

    @abstractmethod
    def save(self, outcome: OutcomeRecord) -> None:
        """
        Guarda o actualiza un registro de outcome inmutable.
        """
        pass

    @abstractmethod
    def get_by_id(self, outcome_id: str) -> Optional[OutcomeRecord]:
        """
        Recupera un outcome por su identidad única.
        """
        pass

    @abstractmethod
    def get_by_action_id(self, action_id: str) -> List[OutcomeRecord]:
        """
        Recupera outcomes asociados a una acción específica.
        """
        pass

    @abstractmethod
    def get_by_decision_id(self, decision_id: str) -> List[OutcomeRecord]:
        """
        Recupera outcomes asociados a una decisión específica.
        """
        pass

    @abstractmethod
    def get_by_mission_id(self, mission_id: str) -> List[OutcomeRecord]:
        """
        Recupera outcomes asociados a una misión específica.
        """
        pass

    @abstractmethod
    def get_by_result_id(self, result_id: str) -> List[OutcomeRecord]:
        """
        Recupera outcomes asociados a un result_id específico.
        """
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[OutcomeRecord]:
        """
        Recupera un outcome por su clave de idempotencia.
        """
        pass

    @abstractmethod
    def exists(self, outcome_id: str) -> bool:
        """
        Verifica si existe un outcome por ID.
        """
        pass
