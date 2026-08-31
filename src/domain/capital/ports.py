from abc import ABC, abstractmethod
from typing import Optional, List
from .models import CapitalBudget, CapitalAllocation


class CapitalRepository(ABC):
    """
    Puerto para la persistencia y recuperación inmutable de presupuestos y asignaciones de capital.
    """

    @abstractmethod
    def save_budget(self, budget: CapitalBudget) -> None:
        """Guarda o actualiza un presupuesto de capital."""
        pass

    @abstractmethod
    def get_budget(self, budget_id: str) -> Optional[CapitalBudget]:
        """Obtiene un presupuesto de capital por su ID."""
        pass

    @abstractmethod
    def save_allocation(self, allocation: CapitalAllocation) -> None:
        """Guarda o actualiza una asignación de capital."""
        pass

    @abstractmethod
    def get_allocation(self, allocation_id: str) -> Optional[CapitalAllocation]:
        """Obtiene una asignación de capital por su ID."""
        pass

    @abstractmethod
    def list_allocations_for_opportunity(self, opportunity_id: str) -> List[CapitalAllocation]:
        """Lista todas las asignaciones históricas o activas para una oportunidad dada."""
        pass
