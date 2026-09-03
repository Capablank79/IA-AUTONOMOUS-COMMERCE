"""Puertos de dominio para Confidence Model (Hito L.4)."""

from typing import Optional, Protocol, Sequence
from .models import ConfidencePolicy, ConfidenceAssessment


class ConfidencePolicyRepositoryPort(Protocol):
    """Contrato de persistencia para políticas de confianza versionadas."""

    def save_policy(self, policy: ConfidencePolicy) -> ConfidencePolicy:
        ...

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[ConfidencePolicy]:
        ...

    def list_policies(self) -> Sequence[ConfidencePolicy]:
        ...


class ConfidenceAssessmentRepositoryPort(Protocol):
    """Contrato de persistencia y consulta de evaluaciones de confianza."""

    def save_assessment(self, assessment: ConfidenceAssessment) -> ConfidenceAssessment:
        ...

    def get_assessment(self, assessment_id: str) -> Optional[ConfidenceAssessment]:
        ...

    def find_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
        field_path: Optional[str] = None,
    ) -> Sequence[ConfidenceAssessment]:
        ...

    def get_latest_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
        field_path: Optional[str] = None,
    ) -> Optional[ConfidenceAssessment]:
        ...
