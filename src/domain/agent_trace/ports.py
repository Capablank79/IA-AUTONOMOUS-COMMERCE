"""
Puertos de dominio para el Registro de Trazas Operacionales de Agentes (Agent Trace - Hito K.2).

Define los contratos abstractos para:
- AgentTraceRepositoryPort: Persistencia atómica, append-only, consultas indexadas y reconstrucción de ejecución.
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from datetime import datetime

from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
    ExecutionTraceTimeline,
)


class AgentTraceRepositoryPort(ABC):
    """
    Puerto secundario de persistencia para registros de trazas de agentes (Append-only e idempotente).
    """

    @abstractmethod
    def append(self, record: AgentTraceRecord) -> AgentTraceRecord:
        """
        Persiste un AgentTraceRecord de forma atómica e inmutable.
        Si ya existe un registro con el mismo trace_id o idempotency_key, retorna el registro existente
        garantizando idempotencia de replay y semántica append-only.
        """
        pass

    @abstractmethod
    def get_by_id(self, trace_id: str) -> Optional[AgentTraceRecord]:
        """Obtiene un AgentTraceRecord por su identificador único."""
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AgentTraceRecord]:
        """Obtiene un AgentTraceRecord por su clave determinista de idempotencia."""
        pass

    @abstractmethod
    def list_records(
        self,
        execution_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        component_name: Optional[str] = None,
        step_type: Optional[StepType] = None,
        status: Optional[TraceStatus] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AgentTraceRecord]:
        """
        Consulta registros de trazas con filtros opcionales ordenados determinísticamente por step_number y started_at.
        """
        pass

    @abstractmethod
    def get_execution_timeline(self, execution_id: str) -> ExecutionTraceTimeline:
        """
        Reconstruye cronológica y causalmente la línea de tiempo completa para un execution_id.
        """
        pass
