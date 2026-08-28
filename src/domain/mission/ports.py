from abc import ABC, abstractmethod
from typing import Optional
from .models import Mission, MissionResult

class MissionOrchestrator(ABC):
    """
    Puerto primario para la orquestación de misiones de negocio.
    Define el contrato para iniciar, monitorear y gestionar el ciclo de vida de una misión.
    """
    
    @abstractmethod
    def submit(self, mission: Mission) -> None:
        """
        Registra y encola una misión para su ejecución.
        No bloquea esperando el resultado.
        """
        pass

    @abstractmethod
    def get_result(self, mission_id: str) -> Optional[MissionResult]:
        """
        Recupera el resultado o estado actual de una misión específica.
        """
        pass

    @abstractmethod
    def cancel(self, mission_id: str) -> None:
        """
        Solicita la detención de una misión en curso.
        """
        pass

class MissionRepository(ABC):
    """
    Puerto secundario para la persistencia de misiones.
    """
    
    @abstractmethod
    def save(self, mission: Mission) -> None:
        pass

    @abstractmethod
    def get_by_id(self, mission_id: str) -> Optional[Mission]:
        pass

    @abstractmethod
    def save_result(self, result: MissionResult) -> None:
        pass

    @abstractmethod
    def get_result(self, mission_id: str) -> Optional[MissionResult]:
        pass
