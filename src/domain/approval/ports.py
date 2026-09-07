"""
Ports and Contracts for N.6 — Approval Policies (Transversal N).
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, List
from .models import (
    ApprovalPolicy,
    ApprovalEvidence,
    ApprovalRequest,
    ApprovalDecision,
)


class ApprovalPolicyRepositoryPort(ABC):
    """
    Puerto para el almacenamiento y consulta de políticas de aprobación.
    """
    @abstractmethod
    def save_policy(self, policy: ApprovalPolicy) -> None:
        pass

    @abstractmethod
    def get_policy(self, policy_name: str) -> Optional[ApprovalPolicy]:
        pass

    @abstractmethod
    def list_policies(self) -> Sequence[ApprovalPolicy]:
        pass


class ApprovalEvidenceRepositoryPort(ABC):
    """
    Puerto persistente para evidencias de aprobación (append/record, retrieve, query).
    """
    @abstractmethod
    def save_evidence(self, evidence: ApprovalEvidence) -> None:
        """Persiste una evidencia de aprobación."""
        pass

    @abstractmethod
    def get_by_id(self, approval_id: str) -> Optional[ApprovalEvidence]:
        """Obtiene una evidencia por su approval_id."""
        pass

    @abstractmethod
    def list_by_correlation_id(self, correlation_id: str) -> Sequence[ApprovalEvidence]:
        """Lista evidencias asociadas a un correlation_id."""
        pass

    @abstractmethod
    def list_by_target(self, action: str, resource: str) -> Sequence[ApprovalEvidence]:
        """Lista evidencias para una combinación acción/recurso."""
        pass
