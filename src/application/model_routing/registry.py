"""
Registro en memoria de ModelRoute (Hito M.1).
"""

from typing import Dict, List, Optional, Sequence
from src.domain.model_routing.models import ModelRoute
from src.domain.model_routing.ports import ModelRouteRegistryPort


class InMemoryModelRouteRegistry(ModelRouteRegistryPort):
    """
    Registro determinista en memoria de rutas de inferencia.
    """

    def __init__(self, initial_routes: Optional[Sequence[ModelRoute]] = None):
        self._routes: Dict[str, ModelRoute] = {}
        if initial_routes:
            for r in initial_routes:
                self.register_route(r)

    def register_route(self, route: ModelRoute) -> None:
        """Registra o actualiza una ruta en el catálogo."""
        self._routes[route.route_id] = route

    def unregister_route(self, route_id: str) -> None:
        """Elimina una ruta del catálogo."""
        self._routes.pop(route_id, None)

    def list_routes(self) -> Sequence[ModelRoute]:
        """Devuelve todas las rutas registradas ordenadas deterministamente por route_id."""
        return tuple(sorted(self._routes.values(), key=lambda r: r.route_id))

    def get_route(self, route_id: str) -> Optional[ModelRoute]:
        """Obtiene una ruta por su ID."""
        return self._routes.get(route_id)
