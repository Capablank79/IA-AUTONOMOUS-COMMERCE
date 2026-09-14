"""
Domain Ports for Q.1 Opportunity Dashboard (Hito Q — Business Intelligence).
"""

from typing import Protocol, List, Optional, Tuple
from src.domain.tenant.models import TenantContext
from src.domain.opportunity_detection.models import OpportunityRecord
from src.domain.opportunity_dashboard.models import (
    OpportunityDashboardItem,
    OpportunityDashboardSummary,
    OpportunityDashboardDetail,
    OpportunityDashboardQuery,
    OpportunityDashboardPage,
    OpportunityComparisonView,
)


class TenantOpportunityRepositoryPort(Protocol):
    """
    Puerto para la persistencia y consulta de OpportunityRecords con aislamiento estricto por Tenant.
    Layout conceptual: data_dir / "tenants" / tenant_id / "opportunities" / ... o base de datos.
    """

    def save(self, context: TenantContext, opportunity: OpportunityRecord) -> None:
        """Persiste un registro de oportunidad en el ámbito exclusivo del tenant."""
        ...

    def save_all(self, context: TenantContext, opportunities: List[OpportunityRecord]) -> int:
        """Persiste una lista de oportunidades en lote para el tenant."""
        ...

    def get_by_id(self, context: TenantContext, opportunity_id: str) -> Optional[OpportunityRecord]:
        """Obtiene una oportunidad por ID garantizando que pertenece al tenant del contexto."""
        ...

    def list_all(self, context: TenantContext) -> List[OpportunityRecord]:
        """Lista todas las oportunidades registradas para el tenant."""
        ...

    def delete(self, context: TenantContext, opportunity_id: str) -> bool:
        """Elimina una oportunidad en el ámbito del tenant."""
        ...
