"""
Puertos de dominio para Context Budgeting (Hito M.2).

Transversal M — Control de Coste e Inferencia.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, Mapping, Any

from src.domain.context_budget.models import (
    ContextBudgetRequest,
    ContextBudgetDecision,
    ContextBudgetPolicy,
    InputTokensBreakdown,
)


class TokenEstimatorPort(ABC):
    """
    Puerto para estimación determinista de tokens a partir de textos, estructuras o desglose.
    Desacoplado de SDKs específicos de proveedores externos.
    """

    @abstractmethod
    def estimate_text_tokens(self, text: str, model_id: Optional[str] = None) -> int:
        """Estima la cantidad de tokens para una cadena de texto (entero no negativo)."""
        pass

    @abstractmethod
    def estimate_breakdown(
        self,
        system_instructions: Optional[str] = None,
        user_input: Optional[str] = None,
        memory_context: Optional[str] = None,
        tool_schemas: Optional[Sequence[Any]] = None,
        retrieved_evidence: Optional[Sequence[Any]] = None,
        conversation_history: Optional[Sequence[Any]] = None,
        other: Optional[Any] = None,
        model_id: Optional[str] = None,
    ) -> InputTokensBreakdown:
        """Calcula un InputTokensBreakdown determinista a partir de los artefactos de entrada."""
        pass


class ContextBudgetServicePort(ABC):
    """
    Puerto primario del servicio de evaluación de presupuesto de contexto M.2.
    """

    @abstractmethod
    def assess_budget(
        self,
        request: ContextBudgetRequest,
        policy: Optional[ContextBudgetPolicy] = None,
    ) -> ContextBudgetDecision:
        """Evalúa si una inferencia está dentro del presupuesto de contexto del modelo seleccionado."""
        pass
