from typing import Dict, List, Optional, Sequence, Mapping, Any
from .models import (
    ToolDescriptor,
    ToolLifecycleStatus,
    ToolExecutionChannel,
    ToolSideEffectLevel,
)
from .ports import ToolRegistryPort


class ToolRegistry(ToolRegistryPort):
    """
    Implementación en el dominio del Tool Registry.
    Almacena, versiona, consulta y descubre capacidades de herramientas de manera determinista.
    """

    def __init__(self):
        # Almacenamiento indexado: tool_id -> {version_str: ToolDescriptor}
        self._tools: Dict[str, Dict[str, ToolDescriptor]] = {}
        # Historial de versiones ordenadas por inserción para resolución por defecto
        self._version_order: Dict[str, List[str]] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        """
        Registra un descriptor de herramienta.
        Lanza ValueError si ya existe un descriptor con el mismo tool_id y versión exacta.
        """
        if descriptor is None:
            raise ValueError("ToolDescriptor cannot be None")

        tool_id = descriptor.tool_id
        version_str = descriptor.version.version_str

        if tool_id not in self._tools:
            self._tools[tool_id] = {}
            self._version_order[tool_id] = []

        if version_str in self._tools[tool_id]:
            raise ValueError(f"Tool '{tool_id}' with version '{version_str}' is already registered")

        self._tools[tool_id][version_str] = descriptor
        self._version_order[tool_id].append(version_str)

    def get(self, tool_id: str, version: Optional[str] = None) -> Optional[ToolDescriptor]:
        """
        Obtiene el descriptor de una herramienta por id y versión.
        Si la versión no se especifica, retorna la última versión registrada.
        """
        if not tool_id or tool_id not in self._tools:
            return None

        tool_versions = self._tools[tool_id]
        if not tool_versions:
            return None

        if version is not None:
            return tool_versions.get(version)

        # Si no se especifica versión, obtener la última registrada en orden
        latest_version = self._version_order[tool_id][-1]
        return tool_versions.get(latest_version)

    def list_all(
        self,
        include_disabled: bool = False,
        include_deprecated: bool = False,
    ) -> Sequence[ToolDescriptor]:
        """
        Lista todos los descriptores registrados según filtros de ciclo de vida.
        """
        result: List[ToolDescriptor] = []
        for tool_id, versions in self._tools.items():
            for version_str, desc in versions.items():
                if desc.status == ToolLifecycleStatus.DISABLED and not include_disabled:
                    continue
                if desc.status == ToolLifecycleStatus.DEPRECATED and not include_deprecated:
                    continue
                result.append(desc)
        return tuple(result)

    def find_by_capability(
        self,
        capability: str,
        channel: Optional[ToolExecutionChannel] = None,
        max_side_effect: Optional[ToolSideEffectLevel] = None,
    ) -> Sequence[ToolDescriptor]:
        """
        Descubre herramientas activas/ejecutables que ofrecen una capacidad específica.
        Filtra automáticamente herramientas DISABLED, DEPRECATED y UNKNOWN a menos que estén explícitamente activas.
        """
        if not capability:
            return ()

        # Jerarquía de severidad de side_effects para comparación
        side_effect_severity = {
            ToolSideEffectLevel.READ_ONLY: 1,
            ToolSideEffectLevel.ANALYSIS: 2,
            ToolSideEffectLevel.WRITE: 3,
            ToolSideEffectLevel.EXTERNAL_SIDE_EFFECT: 4,
            ToolSideEffectLevel.IRREVERSIBLE: 5,
        }

        matches: List[ToolDescriptor] = []
        for desc in self.list_all(include_disabled=False, include_deprecated=False):
            # Solo herramientas ejecutables (REGISTERED o AVAILABLE)
            if not desc.is_executable:
                continue

            if desc.capability != capability:
                continue

            if channel is not None and desc.supported_channels and channel not in desc.supported_channels:
                continue

            if max_side_effect is not None:
                max_sev = side_effect_severity.get(max_side_effect, 5)
                desc_sev = side_effect_severity.get(desc.side_effect_level, 5)
                if desc_sev > max_sev:
                    continue

            matches.append(desc)

        return tuple(matches)

    def update_status(self, tool_id: str, version: str, new_status: ToolLifecycleStatus) -> bool:
        """
        Actualiza el estado del ciclo de vida de una herramienta existente creando un nuevo descriptor inmutable.
        """
        desc = self.get(tool_id, version)
        if desc is None:
            return False

        updated_desc = ToolDescriptor(
            tool_id=desc.tool_id,
            name=desc.name,
            version=desc.version,
            description=desc.description,
            capability=desc.capability,
            input_contract=desc.input_contract,
            output_contract=desc.output_contract,
            side_effect_level=desc.side_effect_level,
            required_permissions=desc.required_permissions,
            supported_channels=desc.supported_channels,
            status=new_status,
            provenance=desc.provenance,
            requires_approval=desc.requires_approval,
            requires_idempotency=desc.requires_idempotency,
            timeout_ms=desc.timeout_ms,
            tags=desc.tags,
            metadata=dict(desc.metadata),
        )

        self._tools[tool_id][version] = updated_desc
        return True
