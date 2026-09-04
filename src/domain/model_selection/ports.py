"""
Puertos de dominio para la Selección de Modelos por Tarea (M.5).

Define los contratos abstractos para:
- TaskSelectionPolicyPort: Acceso a políticas y perfiles de selección de modelos.
- ModelSelectionByTaskServicePort: Servicio primario de resolución de perfiles y selección de rutas.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence
from src.domain.model_routing.models import ModelRoute, RoutingPolicy
from src.domain.model_selection.models import (
    TaskModelProfile,
    TaskSelectionPolicy,
    TaskSelectionRequest,
    TaskSelectionRequirements,
    ModelSelectionResult,
)


class TaskSelectionPolicyPort(ABC):
    """
    Puerto para obtener y consultar políticas y perfiles de selección por tarea.
    """
    @abstractmethod
    def get_policy(self) -> TaskSelectionPolicy:
        """Devuelve la política activa de selección de modelos."""
        pass

    @abstractmethod
    def get_profile(self, task_type: str) -> Optional[TaskModelProfile]:
        """Obtiene el perfil configurado para un tipo de tarea."""
        pass


class ModelSelectionByTaskServicePort(ABC):
    """
    Puerto primario de la aplicación para resolver requerimientos de modelo según el tipo de tarea
    y delegar el enrutamiento a la estrategia de routing M.1.
    """
    @abstractmethod
    def resolve_requirements(
        self,
        request: TaskSelectionRequest,
        policy: Optional[TaskSelectionPolicy] = None,
    ) -> TaskSelectionRequirements:
        """
        Determina y consolida los requerimientos de modelo (complejidad, criticidad, capacidades,
        calidad mínima, latencia) para la tarea dada.
        """
        pass

    @abstractmethod
    def select_model_for_task(
        self,
        request: TaskSelectionRequest,
        available_routes: Optional[Sequence[ModelRoute]] = None,
        task_policy: Optional[TaskSelectionPolicy] = None,
        routing_policy: Optional[RoutingPolicy] = None,
    ) -> ModelSelectionResult:
        """
        Flujo completo M.5:
        1. Resuelve el perfil y requerimientos de la tarea.
        2. Si la tarea es desconocida o no tiene perfil, retorna resultado estructurado UNKNOWN_TASK / NO_PROFILE.
        3. Construye el RoutingRequest de M.1.
        4. Invoca la estrategia de routing M.1 (DeterministicModelRoutingStrategy).
        5. Retorna ModelSelectionResult con la trazabilidad completa.
        """
        pass
