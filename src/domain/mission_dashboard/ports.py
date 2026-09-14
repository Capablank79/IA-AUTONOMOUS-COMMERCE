"""
Domain Ports for Q.4 Mission Dashboard (Hito Q — Business Intelligence).
"""

from typing import Protocol, List, Optional, Tuple, Dict, Any
from src.domain.tenant.models import TenantContext
from src.domain.mission.models import Mission, MissionResult
from src.domain.mission_dashboard.models import (
    MissionDashboardItem,
    MissionDashboardSummary,
    MissionDashboardDetail,
    MissionDashboardQuery,
    MissionDashboardPage,
)


class TenantMissionRepositoryPort(Protocol):
    """
    Puerto para la persistencia y consulta de Misiones y sus Resultados con aislamiento estricto por Tenant.
    Layout conceptual: data_dir / "tenants" / tenant_id / "missions" / ...
    """

    def save(self, context: TenantContext, mission: Mission) -> None:
        """Persiste una misión en el ámbito exclusivo del tenant."""
        ...

    def save_all(self, context: TenantContext, missions: List[Mission]) -> int:
        """Persiste una lista de misiones en lote para el tenant."""
        ...

    def get_by_id(self, context: TenantContext, mission_id: str) -> Optional[Mission]:
        """Obtiene una misión por ID garantizando que pertenece al tenant del contexto."""
        ...

    def list_all(self, context: TenantContext) -> List[Mission]:
        """Lista todas las misiones registradas para el tenant."""
        ...

    def save_result(self, context: TenantContext, result: MissionResult) -> None:
        """Persiste el resultado de una misión en el ámbito exclusivo del tenant."""
        ...

    def get_result(self, context: TenantContext, mission_id: str) -> Optional[MissionResult]:
        """Obtiene el resultado de una misión por ID para el tenant."""
        ...

    def delete(self, context: TenantContext, mission_id: str) -> bool:
        """Elimina una misión y su resultado asociado en el ámbito del tenant."""
        ...
