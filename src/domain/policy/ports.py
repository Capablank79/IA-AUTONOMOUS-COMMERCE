from abc import ABC, abstractmethod
from typing import Optional, Sequence
from .models import (
    PolicyEvaluationContext,
    PolicyEvaluation,
    RuleEvaluationResult,
)


class PolicyRule(ABC):
    """
    Contrato abstracto para una regla individual de política.
    Debe ser determinista, auditable, componible e independiente de infraestructura.
    """
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def category(self) -> str:
        pass

    @abstractmethod
    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        pass


class PolicyEnginePort(ABC):
    """
    Puerto primario del motor de políticas de gobernanza.
    """
    @abstractmethod
    def evaluate(self, context: PolicyEvaluationContext) -> PolicyEvaluation:
        """
        Evalúa el conjunto de políticas aplicables contra el contexto y produce una PolicyEvaluation determinista.
        """
        pass


class PolicyAuditRepository(ABC):
    """
    Puerto secundario para registrar auditorías de evaluación de políticas.
    """
    @abstractmethod
    def save_evaluation(self, evaluation: PolicyEvaluation) -> None:
        pass

    @abstractmethod
    def get_by_id(self, evaluation_id: str) -> Optional[PolicyEvaluation]:
        pass

    @abstractmethod
    def get_by_correlation_id(self, correlation_id: str) -> Sequence[PolicyEvaluation]:
        pass
