"""
Puertos de Dominio para Sub-missions (Hito R.2 — Advanced Autonomy).

Define contratos para:
- SubMissionRepositoryPort: Persistencia y consulta de jerarquías de submisiones y resultados.
- SubMissionServicePort: Servicio de aplicación para creación, ejecución, propagación y control de ciclo de vida.
"""

from typing import Protocol, List, Optional, Tuple, Sequence, Dict, Any
from src.domain.tenant.models import TenantContext
from src.domain.mission.models import Mission, MissionResult, MissionStatus
from src.domain.sub_mission.models import (
    SubMissionCreationContract,
    SubMissionResultContract,
    SubMissionNode,
)


class SubMissionRepositoryPort(Protocol):
    """
    Puerto para la persistencia y consulta de relaciones jerárquicas de submisiones.
    """

    def get_children(self, context: TenantContext, parent_mission_id: str) -> List[Mission]:
        """Obtiene todas las misiones hijas directas de una misión padre para un tenant."""
        ...

    def get_descendants(self, context: TenantContext, root_mission_id: str) -> List[Mission]:
        """Obtiene todos los descendientes en el subárbol con raíz en root_mission_id."""
        ...

    def get_by_delegation_key(
        self, context: TenantContext, parent_mission_id: str, delegation_key: str
    ) -> Optional[Mission]:
        """Obtiene una submisión por su clave de delegación determinista (idempotencia)."""
        ...

    def save_sub_mission_result(
        self, context: TenantContext, result: SubMissionResultContract
    ) -> None:
        """Persiste el contrato de resultado estructurado de una submisión."""
        ...

    def get_sub_mission_result(
        self, context: TenantContext, mission_id: str
    ) -> Optional[SubMissionResultContract]:
        """Recupera el contrato de resultado estructurado de una submisión."""
        ...


class SubMissionServicePort(Protocol):
    """
    Puerto primario del servicio de orquestación y ciclo de vida de Sub-missions.
    """

    def create_sub_mission(
        self, context: TenantContext, contract: SubMissionCreationContract
    ) -> Mission:
        """Crea y registra una submisión validando todos los invariantes y límites de jerarquía."""
        ...

    def propagate_result(
        self, context: TenantContext, result_contract: SubMissionResultContract
    ) -> bool:
        """Propaga el resultado estructurado de una submisión a su padre y actualiza dependencias."""
        ...

    def cancel_hierarchy(
        self, context: TenantContext, parent_mission_id: str, reason: str = "Parent cancelled"
    ) -> int:
        """Cancela en cascada todas las submisiones activas no terminales bajo una misión padre."""
        ...

    def get_hierarchy_tree(
        self, context: TenantContext, root_mission_id: str
    ) -> Optional[SubMissionNode]:
        """Construye y devuelve el árbol jerárquico completo para auditoría o visualización Q.4."""
        ...
