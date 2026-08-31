from typing import Protocol, Optional, Sequence, runtime_checkable
from src.domain.publication.models import SalesChannel
from .models import (
    PricingDecision,
    PricingAction,
    PricingRequest,
    PricingResult,
)


@runtime_checkable
class PricingPort(Protocol):
    """
    Puerto primario de dominio para operaciones de precio en canales comerciales (G.4 / TASK 07.4).
    Desacoplado de marketplaces concretos, HTTP, OAuth y DTOs externos.
    """
    def update_price(self, request: PricingRequest) -> PricingResult:
        """
        Ejecuta la actualización del precio de un listing en el canal de venta.
        Devuelve PricingResult con status APPLIED, FAILED o UNKNOWN.
        """
        ...

    def get_current_price(self, channel: SalesChannel, listing_id: str) -> PricingResult:
        """
        Consulta o verifica el precio actual publicado en el canal externo.
        Permite recuperar/reconciliar operaciones en estado UNKNOWN antes de cualquier reintento.
        """
        ...


@runtime_checkable
class PricingRepository(Protocol):
    """
    Puerto secundario para la persistencia y auditoría de decisiones y resultados de pricing.
    """
    def save_decision(self, decision: PricingDecision) -> None:
        """Guarda o actualiza una decisión de pricing estructurada."""
        ...

    def get_decision(self, decision_id: str) -> Optional[PricingDecision]:
        """Obtiene una decisión de pricing por su ID."""
        ...

    def save_result(self, result: PricingResult) -> None:
        """Guarda o actualiza el resultado de una acción de pricing."""
        ...

    def get_result_by_id(self, pricing_id: str) -> Optional[PricingResult]:
        """Obtiene el resultado de pricing por su ID interno."""
        ...

    def get_results_by_listing_id(self, listing_id: str) -> Sequence[PricingResult]:
        """Obtiene el historial de resultados de pricing para un listing específico."""
        ...
