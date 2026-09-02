"""
Servicio de Aplicación para Agent Trace (Hito K.2).

Orquesta el registro estructurado, seguro y auditable de la ejecución observable de agentes y servicios autónomos:
- BasicMissionOrchestrator
- AutonomousLoop
- ContinuousMissionService
- Interacciones con PolicyEngine / ActionExecutor / ToolRegistry / Servicios J.2-J.6

Principios K.2:
- Failure isolation: Los fallos en el registro de trazas no abortan el flujo del agente (salvo configuración estricta).
- Prohibición estricta de Chain-of-Thought (CoT) y prompts privados completos.
- Generación y vinculación de referencias operacionales (input_reference, output_reference).
- Registro determinista de timing (started_at, completed_at, duration_seconds derivada).
- Integración y correlación con K.1 Audit Trail mediante correlation_id, causation_id y mission_id.
- NO implementa Cost Tracking K.3.
"""

from datetime import datetime, timezone
import logging
from typing import Optional, List, Dict, Any, Union
import uuid

from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
    ExecutionTraceTimeline,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort

logger = logging.getLogger(__name__)


class AgentTraceService:
    """
    Servicio de aplicación para registrar pasos operacionales observables de agentes.
    """

    def __init__(self, trace_repository: AgentTraceRepositoryPort, isolate_failures: bool = True):
        self.trace_repository = trace_repository
        self.isolate_failures = isolate_failures

    def record_step(
        self,
        component_name: str,
        execution_id: str,
        step_number: int,
        step_type: Union[StepType, str],
        operation: str,
        status: Union[TraceStatus, str],
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        tool_or_service: Optional[str] = None,
        input_reference: Optional[str] = None,
        output_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        provenance: str = "AGENT",
        metadata: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> Optional[AgentTraceRecord]:
        """
        Registra un paso observable con aislamiento de fallos y validación de tipos.
        """
        try:
            st = step_type if isinstance(step_type, StepType) else StepType(step_type)
            ts = status if isinstance(status, TraceStatus) else TraceStatus(status)
            
            now = datetime.now(timezone.utc)
            start = started_at or now
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)

            comp = completed_at
            if comp is not None and comp.tzinfo is None:
                comp = comp.replace(tzinfo=timezone.utc)
            elif comp is None and ts in (TraceStatus.SUCCESS, TraceStatus.FAILED, TraceStatus.UNKNOWN, TraceStatus.SKIPPED):
                comp = now

            tid = trace_id or f"trc-{execution_id[:8]}-{step_number:03d}-{uuid.uuid4().hex[:6]}"

            record = AgentTraceRecord(
                trace_id=tid,
                component_name=component_name,
                execution_id=execution_id,
                step_number=step_number,
                step_type=st,
                operation=operation,
                started_at=start,
                completed_at=comp,
                status=ts,
                tool_or_service=tool_or_service,
                input_reference=input_reference,
                output_reference=output_reference,
                correlation_id=correlation_id or execution_id,
                causation_id=causation_id,
                mission_id=mission_id,
                cycle_id=cycle_id,
                provenance=provenance,
                metadata=metadata or {},
            )
            return self.trace_repository.append(record)
        except Exception as e:
            logger.warning(f"Error in AgentTraceService.record_step: {e}")
            if not self.isolate_failures:
                raise
            return None

    def start_execution(
        self,
        component_name: str,
        execution_id: str,
        operation: str = "START_EXECUTION",
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        input_reference: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[AgentTraceRecord]:
        """Registra el inicio de una ejecución de agente (Step 0 / START)."""
        return self.record_step(
            component_name=component_name,
            execution_id=execution_id,
            step_number=0,
            step_type=StepType.START,
            operation=operation,
            status=TraceStatus.SUCCESS,
            input_reference=input_reference,
            correlation_id=correlation_id or execution_id,
            mission_id=mission_id,
            cycle_id=cycle_id,
            metadata=metadata,
        )

    def complete_execution(
        self,
        component_name: str,
        execution_id: str,
        step_number: int,
        operation: str = "COMPLETE_EXECUTION",
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        output_reference: Optional[str] = None,
        status: TraceStatus = TraceStatus.SUCCESS,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[AgentTraceRecord]:
        """Registra la finalización de una ejecución de agente (COMPLETE)."""
        return self.record_step(
            component_name=component_name,
            execution_id=execution_id,
            step_number=step_number,
            step_type=StepType.COMPLETE if status == TraceStatus.SUCCESS else StepType.FAILURE,
            operation=operation,
            status=status,
            output_reference=output_reference,
            correlation_id=correlation_id or execution_id,
            mission_id=mission_id,
            cycle_id=cycle_id,
            metadata=metadata,
        )

    def get_execution_timeline(self, execution_id: str) -> ExecutionTraceTimeline:
        """Recupera la timeline ordenada de una ejecución."""
        return self.trace_repository.get_execution_timeline(execution_id)

    def list_records(
        self,
        execution_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        component_name: Optional[str] = None,
        step_type: Optional[StepType] = None,
        status: Optional[TraceStatus] = None,
        limit: int = 1000,
    ) -> List[AgentTraceRecord]:
        """Consulta trazas filtradas."""
        return self.trace_repository.list_records(
            execution_id=execution_id,
            mission_id=mission_id,
            cycle_id=cycle_id,
            correlation_id=correlation_id,
            component_name=component_name,
            step_type=step_type,
            status=status,
            limit=limit,
        )
