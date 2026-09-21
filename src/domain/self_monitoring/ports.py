"""
Puertos de dominio para Self-monitoring (Hito R.7 — Advanced Autonomy).

Define:
- SelfMonitoringRepositoryPort: Almacén y recuperación de Snapshots y Assessments de salud.
- SignalCollectorPort: Recolección y agregación de señales reales desde subsistemas R.1–R.6, K.3, P.7, P.11, O.7, etc.
- SelfMonitoringAuditPort: Emisión no acoplada hacia Audit Trail K.1 y Agent Trace K.2.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, List

from src.domain.tenant.models import TenantContext
from src.domain.self_monitoring.models import (
    MissionHealthSnapshot,
    HealthAssessment,
    HealthSignal,
    SelfMonitoringDecision,
    MissionHealthStatus,
)


class SelfMonitoringRepositoryPort(ABC):
    """
    Puerto para almacenamiento y consulta de snapshots y evaluaciones de salud.
    Aislado por tenant.
    """

    @abstractmethod
    def save_snapshot(
        self,
        snapshot: MissionHealthSnapshot,
        context: TenantContext,
    ) -> None:
        """Guarda un snapshot de salud de misión."""
        pass

    @abstractmethod
    def get_latest_snapshot(
        self,
        mission_id: str,
        context: TenantContext,
    ) -> Optional[MissionHealthSnapshot]:
        """Obtiene el snapshot más reciente para una misión."""
        pass

    @abstractmethod
    def save_assessment(
        self,
        assessment: HealthAssessment,
        context: TenantContext,
    ) -> None:
        """Guarda una evaluación completa de salud."""
        pass

    @abstractmethod
    def get_latest_assessment(
        self,
        mission_id: str,
        context: TenantContext,
    ) -> Optional[HealthAssessment]:
        """Obtiene la evaluación más reciente de una misión."""
        pass

    @abstractmethod
    def list_assessments(
        self,
        mission_id: str,
        context: TenantContext,
        limit: int = 50,
    ) -> Sequence[HealthAssessment]:
        """Lista el historial de evaluaciones para una misión."""
        pass


class SignalCollectorPort(ABC):
    """
    Puerto para recolección agregada de señales de salud desde subsistemas existentes.
    """

    @abstractmethod
    def collect_signals(
        self,
        mission_id: str,
        context: TenantContext,
    ) -> Sequence[HealthSignal]:
        """Recolecta señales normalizadas para una misión."""
        pass


class SelfMonitoringAuditPort(ABC):
    """
    Puerto para auditoría canónica K.1 / K.2 de decisiones y cambios de salud.
    """

    @abstractmethod
    def record_assessment_audited(
        self,
        assessment: HealthAssessment,
        context: TenantContext,
    ) -> None:
        """Emite evento de auditoría de evaluación de salud."""
        pass

    @abstractmethod
    def record_action_requested(
        self,
        assessment: HealthAssessment,
        decision: SelfMonitoringDecision,
        context: TenantContext,
    ) -> None:
        """Emite evento de auditoría de acción solicitada."""
        pass

    @abstractmethod
    def record_escalation(
        self,
        assessment: HealthAssessment,
        decision: SelfMonitoringDecision,
        context: TenantContext,
    ) -> None:
        """Emite evento de auditoría de escalación P.8."""
        pass

    @abstractmethod
    def record_recovery(
        self,
        assessment: HealthAssessment,
        previous_status: MissionHealthStatus,
        context: TenantContext,
    ) -> None:
        """Emite evento de auditoría de recuperación de salud."""
        pass
