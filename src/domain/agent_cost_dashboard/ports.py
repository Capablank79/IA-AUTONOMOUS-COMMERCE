"""
Puertos de dominio para Q.5 — Agent Cost Dashboard (Hito Q — Business Intelligence).
"""

from typing import Protocol, List, Optional
from src.domain.tenant.models import TenantContext
from src.domain.agent_cost_dashboard.models import (
    AgentCostDashboardItem,
    AgentCostDashboardSummary,
    AgentCostDashboardQuery,
    AgentCostDashboardPage,
    MissionCostSummaryItem,
)


class AgentCostDashboardServicePort(Protocol):
    """
    Puerto primario del servicio consultivo de Agent Cost Dashboard.
    """

    def get_summary(
        self,
        tenant_id: str,
        session_id: str,
        date_from: Optional[object] = None,
        date_to: Optional[object] = None,
    ) -> AgentCostDashboardSummary:
        """Obtiene el resumen financiero y operacional de costos de agentes para el tenant."""
        ...

    def list_agent_costs(
        self,
        tenant_id: str,
        query: AgentCostDashboardQuery,
        session_id: str,
    ) -> AgentCostDashboardPage:
        """Lista registros de costo/uso paginados y filtrados para el tenant."""
        ...

    def get_cost_detail(
        self,
        tenant_id: str,
        item_id: str,
        session_id: str,
    ) -> AgentCostDashboardItem:
        """Obtiene el detalle explicable y sanitizado de un registro de costo específico."""
        ...

    def get_mission_cost_summary(
        self,
        tenant_id: str,
        mission_id: str,
        session_id: str,
    ) -> MissionCostSummaryItem:
        """Obtiene el resumen de costo atribuido a una misión específica."""
        ...
