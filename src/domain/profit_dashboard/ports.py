"""
Domain Ports for Q.3 Profit Dashboard (Hito Q — Business Intelligence).
"""

from typing import Protocol, List, Optional, Tuple
from src.domain.tenant.models import TenantContext
from src.domain.profit_dashboard.models import (
    ProfitDashboardItem,
    ProfitDashboardSummary,
    ProfitDashboardDetail,
    ProfitDashboardQuery,
    ProfitDashboardPage,
    ProfitComparisonView,
)


class TenantProfitRepositoryPort(Protocol):
    """
    Puerto para la persistencia y consulta de hechos/ítems de rentabilidad con aislamiento estricto por Tenant.
    Layout conceptual: data_dir / "tenants" / tenant_id / "profit" / "profit_items.json" o base de datos.
    """

    def save(self, context: TenantContext, item: ProfitDashboardItem) -> None:
        """Persiste un registro de rentabilidad en el ámbito exclusivo del tenant."""
        ...

    def save_all(self, context: TenantContext, items: List[ProfitDashboardItem]) -> int:
        """Persiste una lista de registros de rentabilidad en lote para el tenant."""
        ...

    def get_by_id(self, context: TenantContext, item_id: str) -> Optional[ProfitDashboardItem]:
        """Obtiene un ítem de rentabilidad por ID garantizando que pertenece al tenant del contexto."""
        ...

    def list_all(self, context: TenantContext) -> List[ProfitDashboardItem]:
        """Lista todos los ítems de rentabilidad registrados para el tenant."""
        ...

    def delete(self, context: TenantContext, item_id: str) -> bool:
        """Elimina un ítem de rentabilidad en el ámbito del tenant."""
        ...
