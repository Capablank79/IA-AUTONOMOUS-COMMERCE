"""
Puertos de dominio para Misiones Continuas (Continuous Missions - Hito J.7).

Define contratos secundarios para persistencia y ejecución de ciclos sin acoplarse
a implementaciones concretas de almacenamiento, orquestadores o frameworks.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Tuple

from .models import (
    ContinuousMission,
    ContinuousMissionCycle,
    ContinuousCycleStatus,
)


class ContinuousMissionRepositoryPort(ABC):
    """
    Puerto secundario para la persistencia durable, atómica e idempotente
    de Misiones Continuas y sus Ciclos de Ejecución.
    """

    @abstractmethod
    def save(self, continuous_mission: ContinuousMission) -> None:
        """Guarda o actualiza de forma atómica una misión continua."""
        pass

    @abstractmethod
    def get_by_id(self, continuous_mission_id: str) -> Optional[ContinuousMission]:
        """Obtiene una misión continua por su identificador único."""
        pass

    @abstractmethod
    def get_by_schedule_id(self, schedule_id: str) -> Optional[ContinuousMission]:
        """Obtiene la misión continua asociada a un schedule_id específico."""
        pass

    @abstractmethod
    def list_all(self) -> List[ContinuousMission]:
        """Lista todas las misiones continuas persistidas."""
        pass

    @abstractmethod
    def list_active(self) -> List[ContinuousMission]:
        """Lista todas las misiones continuas en estado ACTIVE."""
        pass

    @abstractmethod
    def save_cycle(self, cycle: ContinuousMissionCycle) -> None:
        """Guarda o actualiza de forma atómica un ciclo de misión continua."""
        pass

    @abstractmethod
    def get_cycle(self, cycle_id: str) -> Optional[ContinuousMissionCycle]:
        """Obtiene un ciclo por su identificador único."""
        pass

    @abstractmethod
    def get_cycle_by_idempotency_key(self, idempotency_key: str) -> Optional[ContinuousMissionCycle]:
        """Obtiene un ciclo por su clave de idempotencia."""
        pass

    @abstractmethod
    def list_cycles(self, continuous_mission_id: Optional[str] = None) -> List[ContinuousMissionCycle]:
        """Lista ciclos históricos, opcionalmente filtrados por misión continua."""
        pass


class CycleExecutorPort(ABC):
    """
    Puerto desacoplado para la ejecución de un ciclo individual de misión continua.
    Coordina la invocación de capacidades existentes (J.2-J.6 / AutonomousLoop / MissionOrchestrator)
    preservando PolicyEngine, Memory, Learning y estado UNKNOWN.
    """

    @abstractmethod
    def execute_cycle(
        self,
        continuous_mission: ContinuousMission,
        cycle: ContinuousMissionCycle,
    ) -> Tuple[ContinuousCycleStatus, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
        """
        Ejecuta el ciclo de misión continua.
        Retorna:
        - ContinuousCycleStatus: Estado determinista del ciclo (SUCCESS, FAILED, UNKNOWN, SKIPPED).
        - Optional[str]: mission_id creado o coordinado.
        - Optional[Dict[str, Any]]: Resumen seguro y sanitizado de resultados.
        - Optional[str]: Mensaje de error o razón de fallo / bloqueo / UNKNOWN.
        """
        pass
