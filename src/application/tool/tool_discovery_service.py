from typing import Sequence, Optional, Mapping, Any, Dict
from src.domain.tool.models import (
    ToolDescriptor,
    ToolExecutionChannel,
    ToolSideEffectLevel,
    ToolLifecycleStatus,
)
from src.domain.tool.ports import ToolRegistryPort


class ToolDiscoveryService:
    """
    Servicio de aplicación para el descubrimiento y selección de herramientas por parte del agente autónomo.
    Permite al agente resolver capacidades requeridas de forma determinista y tipada,
    sin acoplarse a detalles de infraestructura ni decidir la estrategia de negocio por sí mismo.
    """

    def __init__(self, registry: ToolRegistryPort):
        if registry is None:
            raise ValueError("registry cannot be None")
        self.registry = registry

    def discover_tools_for_capability(
        self,
        capability: str,
        channel: Optional[ToolExecutionChannel] = None,
        max_safety_level: Optional[ToolSideEffectLevel] = None,
    ) -> Sequence[ToolDescriptor]:
        """
        Descubre todas las herramientas registradas y ejecutables que satisfacen una capacidad específica.
        """
        return self.registry.find_by_capability(
            capability=capability,
            channel=channel,
            max_side_effect=max_safety_level,
        )

    def get_tool_catalog(
        self,
        include_disabled: bool = False,
        include_deprecated: bool = False,
    ) -> Sequence[ToolDescriptor]:
        """
        Obtiene el catálogo completo de herramientas según los filtros de estado.
        """
        return self.registry.list_all(
            include_disabled=include_disabled,
            include_deprecated=include_deprecated,
        )

    def get_tool(self, tool_id: str, version: Optional[str] = None) -> Optional[ToolDescriptor]:
        """
        Obtiene el descriptor de una herramienta específica.
        """
        return self.registry.get(tool_id=tool_id, version=version)
