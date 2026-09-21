"""
Puertos e interfaces para Long-running Missions (Hito R.6 — Advanced Autonomy).

Define:
- MissionCheckpointRepositoryPort: Almacenamiento y consulta de checkpoints por tenant y versión.
- LeaseManagerPort: Gestión determinista de leases y exclusión mutua de workers.
- HeartbeatPort: Emisión y verificación de latidos de actividad.
- LongRunningMissionServicePort: Orquestación de checkpointing, safe pause y safe resume.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Sequence, Tuple, Dict, Any

from src.domain.tenant.models import TenantContext
from src.domain.long_running_mission.models import (
    MissionCheckpoint,
    LeaseState,
    HeartbeatRecord,
    ResumeDecision,
)


class MissionCheckpointRepositoryPort(ABC):
    """
    Puerto de repositorio para la persistencia atómica y versionada de Checkpoints de Misiones.
    """

    @abstractmethod
    def save_checkpoint(
        self,
        checkpoint: MissionCheckpoint,
        tenant_context: TenantContext,
    ) -> MissionCheckpoint:
        """Guarda un checkpoint de forma atómica. Falla si existe un checkpoint más reciente (Optimistic Concurrency)."""
        pass

    @abstractmethod
    def get_latest_checkpoint(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[MissionCheckpoint]:
        """Obtiene el último checkpoint válido para la misión dada dentro del tenant."""
        pass

    @abstractmethod
    def get_checkpoint_by_version(
        self,
        mission_id: str,
        version: int,
        tenant_context: TenantContext,
    ) -> Optional[MissionCheckpoint]:
        """Obtiene una versión específica del checkpoint."""
        pass

    @abstractmethod
    def list_checkpoints(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> List[MissionCheckpoint]:
        """Lista el historial de checkpoints de una misión en orden cronológico."""
        pass


class LeaseManagerPort(ABC):
    """
    Puerto para la adquisición, renovación y liberación de leases de ejecución de misiones.
    """

    @abstractmethod
    def acquire_lease(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        ttl_seconds: int = 60,
        assignment_version: int = 1,
    ) -> LeaseState:
        """Intenta adquirir el lease exclusivo para un worker."""
        pass

    @abstractmethod
    def renew_lease(
        self,
        lease_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        ttl_seconds: int = 60,
        assignment_version: int = 1,
    ) -> LeaseState:
        """Renueva un lease activo si el worker y assignment_version son válidos."""
        pass

    @abstractmethod
    def release_lease(
        self,
        lease_id: str,
        worker_id: str,
        tenant_context: TenantContext,
    ) -> bool:
        """Libera voluntariamente el lease."""
        pass

    @abstractmethod
    def get_lease(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[LeaseState]:
        """Consulta el estado del lease actual para una misión."""
        pass


class HeartbeatPort(ABC):
    """
    Puerto para la emisión y verificación de latidos de actividad de workers.
    """

    @abstractmethod
    def record_heartbeat(
        self,
        heartbeat: HeartbeatRecord,
        tenant_context: TenantContext,
    ) -> HeartbeatRecord:
        """Registra un latido para el worker activo."""
        pass

    @abstractmethod
    def get_last_heartbeat(
        self,
        mission_id: str,
        tenant_context: TenantContext,
    ) -> Optional[HeartbeatRecord]:
        """Obtiene el último latido registrado para la misión."""
        pass


class LongRunningMissionServicePort(ABC):
    """
    Puerto del servicio de aplicación para misiones de larga duración.
    """

    @abstractmethod
    def create_checkpoint(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MissionCheckpoint:
        """Crea y persiste un checkpoint atómico y durable del estado actual de la misión."""
        pass

    @abstractmethod
    def pause_mission(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        reason: str = "User requested pause",
    ) -> MissionCheckpoint:
        """Pausa una misión activa de forma segura, alcanzando boundary y liberando el lease."""
        pass

    @abstractmethod
    def resume_mission(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
    ) -> ResumeDecision:
        """Evalúa políticas, adquiere lease y reanuda una misión pausada o interrumpida."""
        pass

    @abstractmethod
    def record_heartbeat(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HeartbeatRecord:
        """Emite un latido periódico para el worker activo."""
        pass
