"""
Domain ports for N.11 — Emergency Stop (Transversal N — Security, Governance & Safety).

Define interfaces/abstracciones para:
- EmergencyStopRepositoryPort: Persistencia segura, atómica y crash-safe de registros de stop.
- EmergencyStopServicePort: Servicio principal de activación, desactivación y evaluación.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Sequence, List, Dict, Any

from src.domain.emergency_stop.models import (
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
    EmergencyStopEvaluationContext,
    EmergencyStopDecision,
)
from src.domain.authentication.models import PrincipalContext


class EmergencyStopRepositoryPort(ABC):
    """
    Puerto para almacenamiento persistente y seguro de registros de Emergency Stop.
    Debe soportar atomicidad, crash-safety y verificación de integridad.
    """

    @abstractmethod
    def save(self, record: EmergencyStopRecord) -> None:
        """Guarda o actualiza un registro de Emergency Stop de forma atómica y persistente."""
        pass

    @abstractmethod
    def get_by_id(self, stop_id: str) -> Optional[EmergencyStopRecord]:
        """Obtiene un registro por su identificador único."""
        pass

    @abstractmethod
    def list_records(
        self,
        scope: Optional[EmergencyStopScope] = None,
        target_id: Optional[str] = None,
        state: Optional[EmergencyStopState] = None,
    ) -> Sequence[EmergencyStopRecord]:
        """Lista registros de stop según filtros dados."""
        pass

    @abstractmethod
    def list_active_records(self, current_time: datetime) -> Sequence[EmergencyStopRecord]:
        """Retorna todos los registros con estado ACTIVE y no expirados al instante dado."""
        pass


class EmergencyStopServicePort(ABC):
    """
    Puerto para el servicio de dominio / aplicación de Emergency Stop.
    """

    @abstractmethod
    def activate_stop(
        self,
        scope: EmergencyStopScope,
        reason_details: str,
        principal_context: PrincipalContext,
        target_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        allow_read_only: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> EmergencyStopRecord:
        """Activa un Emergency Stop con validación de identidad y permisos."""
        pass

    @abstractmethod
    def deactivate_stop(
        self,
        stop_id: str,
        reason_details: str,
        principal_context: PrincipalContext,
        metadata: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> EmergencyStopRecord:
        """Desactiva un Emergency Stop existente con validación de identidad y permisos."""
        pass

    @abstractmethod
    def evaluate(
        self,
        context: EmergencyStopEvaluationContext,
    ) -> EmergencyStopDecision:
        """Evalúa un contexto de ejecución y retorna una decisión estructurada con fail-safe."""
        pass
