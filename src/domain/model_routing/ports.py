"""
Puertos de dominio para la Estrategia de Enrutamiento de Modelos (Hito M.1).
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Sequence
from src.domain.model_routing.models import (
    ModelRoute,
    RoutingRequest,
    RoutingDecision,
    RoutingPolicy,
)


class ModelRouteRegistryPort(ABC):
    """
    Puerto para consultar el catálogo/registro de rutas de modelos disponibles.
    """
    @abstractmethod
    def list_routes(self) -> Sequence[ModelRoute]:
        """Devuelve todas las rutas configuradas en el sistema."""
        pass

    @abstractmethod
    def get_route(self, route_id: str) -> Optional[ModelRoute]:
        """Obtiene una ruta específica por su identificador."""
        pass


class ModelRoutingStrategyPort(ABC):
    """
    Puerto primario para ejecutar la estrategia de enrutamiento de modelos.
    """
    @abstractmethod
    def route(
        self,
        request: RoutingRequest,
        available_routes: Optional[Sequence[ModelRoute]] = None,
        policy: Optional[RoutingPolicy] = None,
    ) -> RoutingDecision:
        """
        Evalúa un RoutingRequest frente a las rutas disponibles según una RoutingPolicy
        y produce una RoutingDecision determinista.
        """
        pass
