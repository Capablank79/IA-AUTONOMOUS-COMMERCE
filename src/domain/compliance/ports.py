"""
Ports for N.10 — Audit / Compliance (Transversal N — Security, Governance & Safety).

Define interfaces abstractas para recolección de evidencia, persistencia de políticas y evaluación.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, List, Dict, Any

from src.domain.compliance.models import (
    ComplianceEvidenceReference,
    CompliancePolicy,
    ComplianceAssessment,
    ComplianceReport,
)


class ComplianceEvidenceCollectorPort(ABC):
    """
    Puerto para la recolección de referencias de evidencia desde K.1 (Audit), K.2 (Trace) y N.1-N.9.
    Es de sólo lectura. No muta ni crea eventos.
    """

    @abstractmethod
    def collect_by_correlation_id(
        self,
        correlation_id: str,
        mission_id: Optional[str] = None,
    ) -> Sequence[ComplianceEvidenceReference]:
        """Recolecta todas las referencias de evidencia vinculadas a un correlation_id / mission_id."""
        pass


class CompliancePolicyRepositoryPort(ABC):
    """
    Puerto para la persistencia y consulta de políticas de cumplimiento (CompliancePolicy).
    """

    @abstractmethod
    def save(self, policy: CompliancePolicy) -> None:
        """Guarda o actualiza una política de cumplimiento."""
        pass

    @abstractmethod
    def get_by_name(self, policy_name: str, version: Optional[str] = None) -> Optional[CompliancePolicy]:
        """Recupera una política por nombre y versión (o la última si version=None)."""
        pass

    @abstractmethod
    def list_all(self) -> Sequence[CompliancePolicy]:
        """Lista todas las políticas disponibles."""
        pass


class ComplianceAssessmentServicePort(ABC):
    """
    Puerto principal del servicio de evaluación de cumplimiento (N.10).
    """

    @abstractmethod
    def assess_operation(
        self,
        correlation_id: str,
        mission_id: Optional[str] = None,
        action_or_operation: Optional[str] = None,
        resource: Optional[str] = None,
        policy_name: Optional[str] = None,
        policy_version: Optional[str] = None,
        injected_evidences: Optional[Sequence[ComplianceEvidenceReference]] = None,
    ) -> ComplianceAssessment:
        """
        Evalúa retrospectivamente una operación y determina su estado de cumplimiento.
        """
        pass

    @abstractmethod
    def generate_report(self, assessment: ComplianceAssessment) -> ComplianceReport:
        """Genera un reporte estructurado y exportable para un assessment."""
        pass
