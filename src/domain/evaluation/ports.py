"""
Puertos de dominio para el Arnés de Evaluación (Evaluation Harness - Hito K.4).

Define:
- EvaluatorPort: Interfaz para evaluadores deterministas que procesan un caso y resultado observado.
- EvaluationTargetPort: Interfaz desacoplada para ejecutar o consultar el sistema bajo evaluación.
- EvaluationRepositoryPort: Interfaz para almacenamiento y recuperación inmutable de EvaluationCase y EvaluationResult.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Sequence

from src.domain.evaluation.models import (
    EvaluationCase,
    EvaluationResult,
    EvaluationMetric,
    EvaluationStatus,
    EvaluationType,
)


class EvaluatorPort(ABC):
    """
    Puerto para un evaluador determinista.
    """

    @property
    @abstractmethod
    def evaluation_type(self) -> EvaluationType:
        """Tipo de evaluación que este evaluador atiende."""
        pass

    @property
    @abstractmethod
    def version(self) -> str:
        """Versión del evaluador para trazabilidad y reproducibilidad."""
        pass

    @abstractmethod
    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        """
        Evalúa el resultado real contra los criterios del caso y retorna un EvaluationResult.
        """
        pass


class EvaluationTargetPort(ABC):
    """
    Puerto para el sistema o componente bajo evaluación.
    Desacopla el arnés del código concreto a evaluar.
    """

    @property
    @abstractmethod
    def component_name(self) -> str:
        """Nombre identificativo del componente bajo evaluación."""
        pass

    @abstractmethod
    def execute(self, case: EvaluationCase) -> Dict[str, Any]:
        """
        Ejecuta el caso sobre el sistema bajo evaluación y retorna un dict con:
        - "output": Resultado o estado producido por el sistema
        - "execution_id": ID de ejecución
        - "trace_reference": Referencia opcional a traza (K.2)
        - "audit_reference": Referencia opcional a auditoría (K.1)
        - "cost_reference": Referencia opcional a coste (K.3)
        - "correlation_id": Correlación transversal
        - "metadata": Metadatos adicionales
        """
        pass


class EvaluationRepositoryPort(ABC):
    """
    Puerto para la persistencia y consulta inmutable de casos y resultados de evaluación.
    """

    @abstractmethod
    def save_case(self, case: EvaluationCase) -> EvaluationCase:
        """Guarda un caso de evaluación de forma idempotente."""
        pass

    @abstractmethod
    def get_case(self, case_id: str) -> Optional[EvaluationCase]:
        """Obtiene un caso por su case_id."""
        pass

    @abstractmethod
    def list_cases(
        self,
        evaluation_type: Optional[EvaluationType] = None,
        tag: Optional[str] = None,
        limit: int = 100,
    ) -> List[EvaluationCase]:
        """Lista casos con filtros opcionales."""
        pass

    @abstractmethod
    def save_result(self, result: EvaluationResult) -> EvaluationResult:
        """Guarda un resultado de evaluación de forma idempotente."""
        pass

    @abstractmethod
    def get_result(self, result_id: str) -> Optional[EvaluationResult]:
        """Obtiene un resultado por su result_id."""
        pass

    @abstractmethod
    def list_results(
        self,
        case_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        evaluated_component: Optional[str] = None,
        status: Optional[EvaluationStatus] = None,
        limit: int = 100,
    ) -> List[EvaluationResult]:
        """Lista resultados con filtros opcionales."""
        pass
