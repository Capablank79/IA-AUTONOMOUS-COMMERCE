"""
Puertos de dominio para Freshness / TTL (Hito L.3).

Define:
- FreshnessPolicyRepositoryPort: Contrato para almacenamiento y consulta de políticas de frescura y TTL.
- FreshnessAssessmentRepositoryPort: Contrato para persistencia y consulta de evaluaciones de frescura.
"""

from typing import Protocol, Optional, Sequence, List
from .models import FreshnessPolicy, FreshnessAssessment, FreshnessStatus, SubjectType


class FreshnessPolicyRepositoryPort(Protocol):
    """
    Puerto de repositorio para la persistencia e indexación de políticas FreshnessPolicy.
    """

    def save_policy(self, policy: FreshnessPolicy) -> FreshnessPolicy:
        """
        Persiste una FreshnessPolicy de forma atómica e idempotente.
        Lanza excepción ante colisión/conflicto de contenido bajo la misma policy_id y version.
        """
        ...

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[FreshnessPolicy]:
        """
        Obtiene una política por su policy_id y versión específica.
        Si version es None, obtiene la versión más reciente.
        """
        ...

    def list_policies(self) -> Sequence[FreshnessPolicy]:
        """
        Lista todas las políticas registradas.
        """
        ...


class FreshnessAssessmentRepositoryPort(Protocol):
    """
    Puerto de repositorio para la persistencia y auditoría de evaluaciones de frescura.
    """

    def save_assessment(self, assessment: FreshnessAssessment) -> FreshnessAssessment:
        """
        Persiste un FreshnessAssessment de forma atómica e idempotente.
        """
        ...

    def get_assessment(self, assessment_id: str) -> Optional[FreshnessAssessment]:
        """
        Obtiene una evaluación por su assessment_id exacto.
        Verifica integridad criptográfica SHA-256 en lectura.
        """
        ...

    def find_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
        field_path: Optional[str] = None,
    ) -> Sequence[FreshnessAssessment]:
        """
        Busca evaluaciones asociadas a un sujeto y campo específico.
        """
        ...

    def get_latest_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
        field_path: Optional[str] = None,
    ) -> Optional[FreshnessAssessment]:
        """
        Obtiene la evaluación más reciente de frescura para un sujeto.
        """
        ...
