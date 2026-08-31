from abc import ABC, abstractmethod
from typing import Optional, Sequence, Mapping, Any, Tuple
from .models import (
    ToolDescriptor,
    ToolInvocationRequest,
    ToolInvocationResult,
    ToolLifecycleStatus,
    ToolExecutionChannel,
    ToolSideEffectLevel,
)


class ToolRegistryPort(ABC):
    """
    Puerto primario del Tool Registry en el dominio.
    Permite registrar, consultar, descubrir por capability/tags y filtrar herramientas.
    """
    @abstractmethod
    def register(self, descriptor: ToolDescriptor) -> None:
        """
        Registra un descriptor de herramienta. Si ya existe la misma versión del tool_id,
        debe arrojar error o gestionar según política de duplicados.
        """
        pass

    @abstractmethod
    def get(self, tool_id: str, version: Optional[str] = None) -> Optional[ToolDescriptor]:
        """
        Obtiene el descriptor de una herramienta por id y versión opcional.
        Si la versión no se especifica, retorna la versión más reciente / activa registrada.
        """
        pass

    @abstractmethod
    def list_all(
        self,
        include_disabled: bool = False,
        include_deprecated: bool = False,
    ) -> Sequence[ToolDescriptor]:
        """
        Lista todos los descriptores registrados según filtros de ciclo de vida.
        """
        pass

    @abstractmethod
    def find_by_capability(
        self,
        capability: str,
        channel: Optional[ToolExecutionChannel] = None,
        max_side_effect: Optional[ToolSideEffectLevel] = None,
    ) -> Sequence[ToolDescriptor]:
        """
        Descubre herramientas disponibles que ofrecen una capacidad específica.
        """
        pass

    @abstractmethod
    def update_status(self, tool_id: str, version: str, new_status: ToolLifecycleStatus) -> bool:
        """
        Actualiza el estado del ciclo de vida de una herramienta (AVAILABLE, DISABLED, DEPRECATED, UNKNOWN).
        """
        pass


class ToolInvokerPort(ABC):
    """
    Puerto secundario/puente para invocar la ejecución de una herramienta a través
    de su adaptador de infraestructura correspondiente, una vez autorizada por Policy.
    """
    @abstractmethod
    def invoke(self, request: ToolInvocationRequest, descriptor: ToolDescriptor) -> ToolInvocationResult:
        """
        Invoca la herramienta tipada a través de los adaptadores correspondientes.
        """
        pass
