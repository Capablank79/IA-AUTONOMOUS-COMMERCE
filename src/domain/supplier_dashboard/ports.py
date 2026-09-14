"""
Domain Ports for Q.2 Supplier Dashboard (Hito Q — Business Intelligence).
"""

from typing import Protocol, List, Optional, Tuple
from src.domain.tenant.models import TenantContext
from src.domain.supplier_intelligence.models import Supplier
from src.domain.supplier_dashboard.models import (
    SupplierDashboardItem,
    SupplierDashboardSummary,
    SupplierDashboardDetail,
    SupplierDashboardQuery,
    SupplierDashboardPage,
    SupplierComparisonView,
)


class TenantSupplierRepositoryPort(Protocol):
    """
    Puerto para la persistencia y consulta de Proveedores con aislamiento estricto por Tenant.
    Layout conceptual: data_dir / "tenants" / tenant_id / "suppliers" / "suppliers.json" o base de datos.
    """

    def save(self, context: TenantContext, supplier: Supplier) -> None:
        """Persiste un registro de proveedor en el ámbito exclusivo del tenant."""
        ...

    def save_all(self, context: TenantContext, suppliers: List[Supplier]) -> int:
        """Persiste una lista de proveedores en lote para el tenant."""
        ...

    def get_by_id(self, context: TenantContext, supplier_id: str) -> Optional[Supplier]:
        """Obtiene un proveedor por ID garantizando que pertenece al tenant del contexto."""
        ...

    def list_all(self, context: TenantContext) -> List[Supplier]:
        """Lista todos los proveedores registrados para el tenant."""
        ...

    def delete(self, context: TenantContext, supplier_id: str) -> bool:
        """Elimina un proveedor en el ámbito del tenant."""
        ...
