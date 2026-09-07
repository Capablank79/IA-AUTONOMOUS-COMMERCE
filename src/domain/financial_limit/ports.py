"""
Ports and interfaces for N.7 — Financial Limits (Transversal N — Security, Governance & Safety).
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Sequence

from .models import (
    FinancialLimitPolicy,
    FinancialLimitRequest,
    FinancialLimitDecision,
)


class FinancialLimitPolicyRepositoryPort(ABC):
    """
    Puerto de persistencia de políticas de límites financieros.
    """

    @abstractmethod
    def save_policy(self, policy: FinancialLimitPolicy) -> None:
        """Persiste una política de límites financieros de forma atómica y crash-safe."""
        pass

    @abstractmethod
    def get_policy(self, policy_name: str) -> Optional[FinancialLimitPolicy]:
        """Obtiene una política por su nombre."""
        pass

    @abstractmethod
    def list_policies(self) -> Sequence[FinancialLimitPolicy]:
        """Lista todas las políticas de límites financieros registradas."""
        pass


class FinancialLimitServicePort(ABC):
    """
    Puerto de servicio para la evaluación y gobernanza de límites económicos.
    """

    @abstractmethod
    def evaluate(self, request: FinancialLimitRequest) -> FinancialLimitDecision:
        """
        Evalúa si la solicitud financiera está dentro de los límites permitidos.
        """
        pass
