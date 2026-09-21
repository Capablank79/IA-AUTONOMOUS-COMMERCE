"""
Puertos del dominio para Agent Coordination (Hito R.4 — Advanced Autonomy).

Define:
- CoordinationSessionRepositoryPort: Almacenamiento y recuperación atómica de CoordinationSession.
- AgentCoordinatorPort: Interfaz del servicio de coordinación de agentes.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Tuple, Sequence, Mapping, Any

from src.domain.agent_coordination.models import (
    CoordinationSession,
    CoordinationTask,
    TaskClaim,
    AgentHandoff,
    MergeResult,
    MergeStrategy,
    SharedCoordinationContext,
    CoordinationPolicy,
)
from src.domain.agent_coordination.delegation_models import (
    DelegationRequest,
    DelegationDecision,
    DelegationRecord,
)
from src.domain.tenant.models import TenantContext
from src.domain.planning.models import ExecutionPlan
from src.domain.mission.models import Mission


class CoordinationSessionRepositoryPort(ABC):
    """
    Puerto para la persistencia y recuperación de CoordinationSessions con aislamiento multi-tenant.
    """

    @abstractmethod
    def save_session(
        self,
        session: CoordinationSession,
        tenant_context: TenantContext,
    ) -> CoordinationSession:
        """Guarda o actualiza una CoordinationSession de manera atómica."""
        raise NotImplementedError

    @abstractmethod
    def get_session(
        self,
        session_id: str,
        tenant_context: TenantContext,
    ) -> Optional[CoordinationSession]:
        """Obtiene una sesión por su ID si pertenece al tenant del contexto."""
        raise NotImplementedError

    @abstractmethod
    def get_session_by_mission(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[CoordinationSession]:
        """Obtiene la sesión de coordinación activa para una misión dada."""
        raise NotImplementedError

    @abstractmethod
    def list_sessions_for_tenant(
        self,
        tenant_context: TenantContext,
    ) -> List[CoordinationSession]:
        """Lista todas las sesiones de coordinación del tenant."""
        raise NotImplementedError


class AgentCoordinatorPort(ABC):
    """
    Puerto principal para orquestar la coordinación multi-agente en R.4.
    """

    @abstractmethod
    def create_session(
        self,
        tenant_context: TenantContext,
        mission_id: str,
        plan: Optional[ExecutionPlan] = None,
        sub_missions: Sequence[Mission] = (),
        policy: Optional[CoordinationPolicy] = None,
        correlation_id: Optional[str] = None,
    ) -> CoordinationSession:
        """Crea e inicializa una nueva CoordinationSession ligada a un plan o submisiones."""
        raise NotImplementedError

    @abstractmethod
    def get_ready_tasks(
        self,
        session_id: str,
        tenant_context: TenantContext,
    ) -> Tuple[CoordinationTask, ...]:
        """Calcula de forma determinista qué tareas están READY para ser reclamadas y ejecutadas."""
        raise NotImplementedError

    @abstractmethod
    def claim_task(
        self,
        session_id: str,
        task_id: str,
        agent_id: str,
        tenant_context: TenantContext,
    ) -> Tuple[CoordinationSession, TaskClaim]:
        """Adquiere el lease exclusivo sobre una tarea mutable (Single-Owner Invariant)."""
        raise NotImplementedError

    @abstractmethod
    def handoff(
        self,
        session_id: str,
        source_agent_id: str,
        target_task_id: str,
        payload: Mapping[str, Any],
        tenant_context: TenantContext,
        evidence_refs: Sequence[str] = (),
        target_agent_id: Optional[str] = None,
    ) -> Tuple[CoordinationSession, AgentHandoff]:
        """Transfiere datos estructurados y evidencia entre agentes (Zero-CoT)."""
        raise NotImplementedError

    @abstractmethod
    def merge_task_results(
        self,
        session_id: str,
        source_task_ids: Sequence[str],
        target_task_id: str,
        tenant_context: TenantContext,
        strategy: Optional[MergeStrategy] = None,
        precedence_order: Sequence[str] = (),
    ) -> MergeResult:
        """Fusiona deterministamente los outputs de varias tareas upstream hacia una downstream."""
        raise NotImplementedError

    @abstractmethod
    def get_session(
        self,
        session_id: str,
        tenant_context: TenantContext,
    ) -> Optional[CoordinationSession]:
        """Obtiene una sesión de coordinación por su ID."""
        raise NotImplementedError

    @abstractmethod
    def cancel_coordination(
        self,
        session_id: str,
        tenant_context: TenantContext,
        reason: str = "Mission cancelled",
    ) -> CoordinationSession:
        """Cancela en cascada todas las tareas no terminales y libera leases/locks."""
        raise NotImplementedError

    @abstractmethod
    def delegate_task(
        self,
        session_id: str,
        request: DelegationRequest,
        tenant_context: TenantContext,
    ) -> Tuple[CoordinationSession, DelegationRecord]:
        """
        Reasigna dinámicamente la tarea a un nuevo especialista compatible (R.5).
        Efectúa transferencia atómica de lease/claim, incrementa assignment_version y audita.
        """
        raise NotImplementedError
