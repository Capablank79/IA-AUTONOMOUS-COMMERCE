"""
Ports and interfaces for N.8 — Tool Allowlist / Denylist (Transversal N — Security, Governance & Safety).
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from .models import (
    ToolPolicy,
    ToolAccessRequest,
    ToolAccessDecision,
)


class ToolPolicyRepositoryPort(ABC):
    """
    Puerto de persistencia de políticas de acceso y control de herramientas (N.8).
    """

    @abstractmethod
    def save_policy(self, policy: ToolPolicy) -> None:
        """Persiste una política de herramientas de forma atómica y crash-safe."""
        pass

    @abstractmethod
    def get_policy(self, policy_name: str) -> Optional[ToolPolicy]:
        """Obtiene una política por su nombre."""
        pass

    @abstractmethod
    def list_policies(self) -> Sequence[ToolPolicy]:
        """Lista todas las políticas de herramientas registradas."""
        pass


class ToolAccessPolicyServicePort(ABC):
    """
    Puerto de servicio para la evaluación determinista de acceso a herramientas (N.8).
    """

    @abstractmethod
    def evaluate(self, request: ToolAccessRequest) -> ToolAccessDecision:
        """
        Evalúa si la invocación a la herramienta/operación está permitida en el contexto dado.
        """
        pass
