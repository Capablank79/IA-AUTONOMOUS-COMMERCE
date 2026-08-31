from typing import Protocol, Optional, Sequence, runtime_checkable
from src.domain.publication.models import SalesChannel
from .models import (
    InventoryDecision,
    InventoryAction,
    InventoryRequest,
    InventoryResult,
    StockLevel,
)


@runtime_checkable
class InventoryPort(Protocol):
    """
    Puerto primario de dominio para operaciones de inventario/stock en canales comerciales (G.5 / TASK 07.5).
    Desacoplado de marketplaces concretos, HTTP, OAuth y DTOs externos.
    """
    def update_inventory(self, request: InventoryRequest) -> InventoryResult:
        """
        Ejecuta la actualización del stock disponible de un listing en el canal de venta.
        Devuelve InventoryResult con status APPLIED, FAILED o UNKNOWN.
        """
        ...

    def get_current_stock(self, channel: SalesChannel, listing_id: str) -> InventoryResult:
        """
        Consulta o verifica la cantidad actual publicada en el canal externo.
        Permite recuperar/reconciliar operaciones en estado UNKNOWN antes de cualquier reintento.
        """
        ...


@runtime_checkable
class InventoryRepository(Protocol):
    """
    Puerto secundario para la persistencia y auditoría de decisiones, niveles de stock y resultados de inventario.
    """
    def save_decision(self, decision: InventoryDecision) -> None:
        """Guarda o actualiza una decisión de stock estructurada."""
        ...

    def get_decision(self, decision_id: str) -> Optional[InventoryDecision]:
        """Obtiene una decisión de stock por su ID."""
        ...

    def save_result(self, result: InventoryResult) -> None:
        """Guarda o actualiza el resultado de una acción de stock."""
        ...

    def get_result_by_id(self, inventory_id: str) -> Optional[InventoryResult]:
        """Obtiene el resultado de inventario por su ID interno."""
        ...

    def get_results_by_listing_id(self, listing_id: str) -> Sequence[InventoryResult]:
        """Obtiene el historial de resultados de inventario para un listing específico."""
        ...
