"""
Servicio de Aplicación para Misiones Continuas (Continuous Mission Service - Hito J.7).

Coordina:
- Creación, arranque, pausa, reanudación y detención determinista de misiones continuas.
- Integración con SchedulerService (J.1) para la ejecución periódica basada en ocurrencias.
- Despacho de ciclos individuales delegados a `CycleExecutorPort`.
- Idempotencia estricta por ciclo / ocurrencia / replay.
- Preservación y actualización atómica del estado durable de la misión y sus ciclos.
- Verificación rigurosa de condiciones de parada (max_cycles, fallas consecutivas, manual stop).
- Preservación determinista de incertidumbre UNKNOWN sin inventar éxitos falsos ni reintentos ciegos.
- Resiliencia ante reinicios y recuperaciones tras fallos de proceso.
- Aislamiento y sanitización de credenciales.

Límites:
- NO implementa un nuevo AutonomousLoop ni un nuevo Scheduler.
- NO contiene bucles infinitos (`while True`), timers ni `time.sleep()`.
- NO salta PolicyEngine ni DecisionProvider.
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

from src.domain.mission.models import MissionType, MissionPriority
from src.domain.scheduling.models import Clock, SystemClock, Schedule, ScheduleOccurrence, ExecutionStatus
from src.domain.scheduling.ports import MissionTriggerPort
from src.application.scheduling.service import SchedulerService
from src.domain.continuous_mission.models import (
    ContinuousMission,
    ContinuousMissionCycle,
    ContinuousMissionStatus,
    ContinuousCycleStatus,
    ContinuousMissionStopCondition,
    StopConditionType,
)
from src.domain.continuous_mission.ports import (
    ContinuousMissionRepositoryPort,
    CycleExecutorPort,
)
from src.application.continuous_mission.cycle_executor_adapter import StandardCycleExecutorAdapter
from src.domain.agent_trace.models import StepType, TraceStatus
from src.application.agent_trace.agent_trace_service import AgentTraceService

logger = logging.getLogger(__name__)


class ContinuousMissionService(MissionTriggerPort):
    """
    Servicio de Aplicación de Misiones Continuas.
    Implementa también `MissionTriggerPort` para conectarse de forma nativa al `SchedulerService` (J.1).
    """

    def __init__(
        self,
        repository: ContinuousMissionRepositoryPort,
        cycle_executor: Optional[CycleExecutorPort] = None,
        scheduler_service: Optional[SchedulerService] = None,
        clock: Optional[Clock] = None,
        agent_trace_service: Optional[AgentTraceService] = None,
    ):
        self.repository = repository
        self.cycle_executor = cycle_executor or StandardCycleExecutorAdapter()
        self.scheduler_service = scheduler_service
        self.clock = clock or SystemClock()
        self.agent_trace_service = agent_trace_service

    def create_continuous_mission(
        self,
        schedule_id: str,
        mission_type: MissionType,
        goal: str,
        continuous_mission_id: Optional[str] = None,
        priority: MissionPriority = MissionPriority.MEDIUM,
        mission_parameters: Optional[Dict[str, Any]] = None,
        stop_condition: Optional[ContinuousMissionStopCondition] = None,
        correlation_id: Optional[str] = None,
        provenance: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ContinuousMission:
        """
        Crea y persiste una nueva entidad ContinuousMission en estado CREATED.
        """
        now = self.clock.now()
        cm = ContinuousMission.create(
            continuous_mission_id=continuous_mission_id,
            schedule_id=schedule_id,
            mission_type=mission_type,
            goal=goal,
            priority=priority,
            mission_parameters=mission_parameters,
            stop_condition=stop_condition,
            correlation_id=correlation_id,
            provenance=provenance,
            metadata=metadata,
            created_at=now,
        )
        self.repository.save(cm)
        return cm

    def get_continuous_mission(self, continuous_mission_id: str) -> Optional[ContinuousMission]:
        """Obtiene una misión continua por su ID."""
        return self.repository.get_by_id(continuous_mission_id)

    def get_by_schedule_id(self, schedule_id: str) -> Optional[ContinuousMission]:
        """Obtiene una misión continua vinculada a un schedule."""
        return self.repository.get_by_schedule_id(schedule_id)

    def start_mission(self, continuous_mission_id: str) -> ContinuousMission:
        """
        Transiciona la misión continua de CREATED o PAUSED a ACTIVE.
        """
        cm = self.repository.get_by_id(continuous_mission_id)
        if not cm:
            raise ValueError(f"ContinuousMission '{continuous_mission_id}' not found")

        if cm.status not in (ContinuousMissionStatus.CREATED, ContinuousMissionStatus.PAUSED):
            raise ValueError(f"Cannot start ContinuousMission from state {cm.status.value}")

        now = self.clock.now()
        updated = ContinuousMission(
            continuous_mission_id=cm.continuous_mission_id,
            schedule_id=cm.schedule_id,
            mission_type=cm.mission_type,
            goal=cm.goal,
            status=ContinuousMissionStatus.ACTIVE,
            priority=cm.priority,
            mission_parameters=cm.mission_parameters,
            stop_condition=cm.stop_condition,
            created_at=cm.created_at,
            started_at=cm.started_at or now,
            last_cycle_at=cm.last_cycle_at,
            next_cycle_at=cm.next_cycle_at,
            cycle_count=cm.cycle_count,
            consecutive_failures=cm.consecutive_failures,
            total_failures=cm.total_failures,
            last_result_status=cm.last_result_status,
            last_cycle_id=cm.last_cycle_id,
            last_mission_id=cm.last_mission_id,
            correlation_id=cm.correlation_id,
            provenance=cm.provenance,
            metadata=cm.metadata,
        )
        self.repository.save(updated)

        # Habilitar el schedule en el scheduler si está disponible
        if self.scheduler_service:
            self.scheduler_service.enable_schedule(cm.schedule_id)

        return updated

    def pause_mission(self, continuous_mission_id: str) -> ContinuousMission:
        """
        Pausa una misión continua ACTIVE transicionándola a PAUSED.
        """
        cm = self.repository.get_by_id(continuous_mission_id)
        if not cm:
            raise ValueError(f"ContinuousMission '{continuous_mission_id}' not found")

        if cm.status != ContinuousMissionStatus.ACTIVE:
            raise ValueError(f"Cannot pause ContinuousMission in state {cm.status.value}")

        updated = ContinuousMission(
            continuous_mission_id=cm.continuous_mission_id,
            schedule_id=cm.schedule_id,
            mission_type=cm.mission_type,
            goal=cm.goal,
            status=ContinuousMissionStatus.PAUSED,
            priority=cm.priority,
            mission_parameters=cm.mission_parameters,
            stop_condition=cm.stop_condition,
            created_at=cm.created_at,
            started_at=cm.started_at,
            last_cycle_at=cm.last_cycle_at,
            next_cycle_at=cm.next_cycle_at,
            cycle_count=cm.cycle_count,
            consecutive_failures=cm.consecutive_failures,
            total_failures=cm.total_failures,
            last_result_status=cm.last_result_status,
            last_cycle_id=cm.last_cycle_id,
            last_mission_id=cm.last_mission_id,
            correlation_id=cm.correlation_id,
            provenance=cm.provenance,
            metadata=cm.metadata,
        )
        self.repository.save(updated)

        # Pausar / Deshabilitar schedule en el scheduler si corresponde
        if self.scheduler_service:
            self.scheduler_service.disable_schedule(cm.schedule_id)

        return updated

    def resume_mission(self, continuous_mission_id: str) -> ContinuousMission:
        """
        Reanuda una misión PAUSED a ACTIVE.
        """
        return self.start_mission(continuous_mission_id)

    def stop_mission(
        self, continuous_mission_id: str, reason: str = "Manual stop"
    ) -> ContinuousMission:
        """
        Detiene permanentemente una misión continua transicionándola a STOPPED.
        """
        cm = self.repository.get_by_id(continuous_mission_id)
        if not cm:
            raise ValueError(f"ContinuousMission '{continuous_mission_id}' not found")

        if cm.status in (ContinuousMissionStatus.STOPPED, ContinuousMissionStatus.COMPLETED):
            return cm

        meta = dict(cm.metadata)
        meta["stop_reason"] = reason

        updated = ContinuousMission(
            continuous_mission_id=cm.continuous_mission_id,
            schedule_id=cm.schedule_id,
            mission_type=cm.mission_type,
            goal=cm.goal,
            status=ContinuousMissionStatus.STOPPED,
            priority=cm.priority,
            mission_parameters=cm.mission_parameters,
            stop_condition=cm.stop_condition,
            created_at=cm.created_at,
            started_at=cm.started_at,
            last_cycle_at=cm.last_cycle_at,
            next_cycle_at=None,
            cycle_count=cm.cycle_count,
            consecutive_failures=cm.consecutive_failures,
            total_failures=cm.total_failures,
            last_result_status=cm.last_result_status,
            last_cycle_id=cm.last_cycle_id,
            last_mission_id=cm.last_mission_id,
            correlation_id=cm.correlation_id,
            provenance=cm.provenance,
            metadata=meta,
        )
        self.repository.save(updated)

        if self.scheduler_service:
            self.scheduler_service.disable_schedule(cm.schedule_id)

        return updated

    def execute_next_cycle(
        self,
        continuous_mission_id: str,
        occurrence: Optional[ScheduleOccurrence] = None,
    ) -> ContinuousMissionCycle:
        """
        Ejecuta el siguiente ciclo de la misión continua de forma segura e idempotente.
        """
        cm = self.repository.get_by_id(continuous_mission_id)
        if not cm:
            raise ValueError(f"ContinuousMission '{continuous_mission_id}' not found")

        now = self.clock.now()
        scheduled_at = occurrence.scheduled_at if occurrence else (cm.next_cycle_at or now)

        # Si la misión no está ACTIVA, no ejecutamos ciclo nuevo
        if cm.status != ContinuousMissionStatus.ACTIVE:
            cycle_id = f"cyc_{cm.continuous_mission_id}_skipped_{uuid.uuid4().hex[:6]}"
            skipped_cycle = ContinuousMissionCycle(
                cycle_id=cycle_id,
                continuous_mission_id=cm.continuous_mission_id,
                cycle_number=cm.cycle_count,
                scheduled_at=scheduled_at,
                started_at=now,
                completed_at=now,
                status=ContinuousCycleStatus.SKIPPED,
                occurrence_id=occurrence.occurrence_id if occurrence else None,
                correlation_id=cm.correlation_id,
                provenance=cm.provenance,
                error_message=f"Cycle skipped because mission status is {cm.status.value}",
            )
            return skipped_cycle

        # Evaluar idempotencia por ciclo / occurrence
        # Si se proporciona una ocurrencia, la clave de idempotencia se ancla a la ocurrencia.
        # Si no, se ancla al scheduled_at (que avanza a now si no hay ocurrencia explícita)
        if occurrence and occurrence.occurrence_id:
            idempotency_key = f"cmc_{cm.continuous_mission_id}_occ_{occurrence.occurrence_id}"
        else:
            scheduled_at = now
            idempotency_key = f"cmc_{cm.continuous_mission_id}_cycle_{cm.cycle_count + 1}_{now.isoformat()}"

        existing_cycle = self.repository.get_cycle_by_idempotency_key(idempotency_key)
        if existing_cycle:
            return existing_cycle

        next_cycle_number = cm.cycle_count + 1

        # Crear registro de ciclo PENDING / RUNNING
        cycle_id = f"cyc_{cm.continuous_mission_id}_c{next_cycle_number}_{uuid.uuid4().hex[:6]}"
        initial_cycle = ContinuousMissionCycle(
            cycle_id=cycle_id,
            continuous_mission_id=cm.continuous_mission_id,
            cycle_number=next_cycle_number,
            scheduled_at=scheduled_at,
            started_at=now,
            completed_at=None,
            status=ContinuousCycleStatus.RUNNING,
            occurrence_id=occurrence.occurrence_id if occurrence else None,
            idempotency_key=idempotency_key,
            correlation_id=cm.correlation_id,
            causation_id=occurrence.occurrence_id if occurrence else cm.continuous_mission_id,
            provenance=cm.provenance,
        )
        self.repository.save_cycle(initial_cycle)

        # Delegar ejecución al adapter de ciclos
        exec_id = f"exec-cmc-{initial_cycle.cycle_id}"
        if self.agent_trace_service:
            self.agent_trace_service.start_execution(
                component_name="ContinuousMissionService",
                execution_id=exec_id,
                mission_id=cm.continuous_mission_id,
                cycle_id=initial_cycle.cycle_id,
                correlation_id=cm.correlation_id,
                input_reference=f"cycle_number:{initial_cycle.cycle_number},schedule_id:{cm.schedule_id}",
                metadata={"continuous_mission_id": cm.continuous_mission_id}
            )

        cycle_status, mission_id, summary, error_msg = self.cycle_executor.execute_cycle(cm, initial_cycle)
        completed_time = self.clock.now()

        if self.agent_trace_service:
            if cycle_status == ContinuousCycleStatus.SUCCESS:
                cm_trace_status = TraceStatus.SUCCESS
            elif cycle_status == ContinuousCycleStatus.UNKNOWN:
                cm_trace_status = TraceStatus.UNKNOWN
            elif cycle_status == ContinuousCycleStatus.SKIPPED:
                cm_trace_status = TraceStatus.SKIPPED
            else:
                cm_trace_status = TraceStatus.FAILED
            self.agent_trace_service.complete_execution(
                component_name="ContinuousMissionService",
                execution_id=exec_id,
                step_number=10,
                operation="CYCLE_EXECUTION_COMPLETED",
                mission_id=cm.continuous_mission_id,
                cycle_id=initial_cycle.cycle_id,
                correlation_id=cm.correlation_id,
                output_reference=f"status:{cycle_status.value},mission_id:{mission_id or ''}",
                status=cm_trace_status,
                metadata={"error_message": error_msg or ""}
            )

        completed_cycle = ContinuousMissionCycle(
            cycle_id=initial_cycle.cycle_id,
            continuous_mission_id=cm.continuous_mission_id,
            cycle_number=initial_cycle.cycle_number,
            scheduled_at=initial_cycle.scheduled_at,
            started_at=initial_cycle.started_at,
            completed_at=completed_time,
            status=cycle_status,
            mission_id=mission_id,
            occurrence_id=initial_cycle.occurrence_id,
            idempotency_key=initial_cycle.idempotency_key,
            correlation_id=initial_cycle.correlation_id,
            causation_id=initial_cycle.causation_id,
            provenance=initial_cycle.provenance,
            result_summary=summary or {},
            error_message=error_msg,
        )
        self.repository.save_cycle(completed_cycle)

        # Actualizar métricas y estado de la misión continua
        new_cycle_count = cm.cycle_count + 1
        new_consecutive_failures = (
            cm.consecutive_failures + 1 if cycle_status == ContinuousCycleStatus.FAILED else 0
        )
        new_total_failures = (
            cm.total_failures + 1 if cycle_status == ContinuousCycleStatus.FAILED else cm.total_failures
        )

        # Evaluar condiciones de parada automáticas
        new_status = cm.status
        meta = dict(cm.metadata)

        # 1. Max cycles
        if cm.stop_condition.max_cycles is not None and new_cycle_count >= cm.stop_condition.max_cycles:
            new_status = ContinuousMissionStatus.COMPLETED
            meta["stop_reason"] = f"Max cycles reached ({cm.stop_condition.max_cycles})"
            if self.scheduler_service:
                self.scheduler_service.disable_schedule(cm.schedule_id)

        # 2. Max consecutive failures
        elif new_consecutive_failures >= cm.stop_condition.max_consecutive_failures:
            new_status = ContinuousMissionStatus.FAILED
            meta["stop_reason"] = f"Max consecutive failures reached ({cm.stop_condition.max_consecutive_failures})"
            if self.scheduler_service:
                self.scheduler_service.disable_schedule(cm.schedule_id)

        # 3. Stop on unknown if configured
        elif cycle_status == ContinuousCycleStatus.UNKNOWN and cm.stop_condition.stop_on_unknown:
            new_status = ContinuousMissionStatus.UNKNOWN
            meta["stop_reason"] = "Stopped due to UNKNOWN cycle outcome"
            if self.scheduler_service:
                self.scheduler_service.disable_schedule(cm.schedule_id)

        updated_mission = ContinuousMission(
            continuous_mission_id=cm.continuous_mission_id,
            schedule_id=cm.schedule_id,
            mission_type=cm.mission_type,
            goal=cm.goal,
            status=new_status,
            priority=cm.priority,
            mission_parameters=cm.mission_parameters,
            stop_condition=cm.stop_condition,
            created_at=cm.created_at,
            started_at=cm.started_at or now,
            last_cycle_at=completed_time,
            next_cycle_at=None if new_status in (ContinuousMissionStatus.COMPLETED, ContinuousMissionStatus.FAILED, ContinuousMissionStatus.STOPPED) else cm.next_cycle_at,
            cycle_count=new_cycle_count,
            consecutive_failures=new_consecutive_failures,
            total_failures=new_total_failures,
            last_result_status=cycle_status.value,
            last_cycle_id=completed_cycle.cycle_id,
            last_mission_id=mission_id,
            correlation_id=cm.correlation_id,
            provenance=cm.provenance,
            metadata=meta,
        )
        self.repository.save(updated_mission)

        return completed_cycle

    # --- Implementación de MissionTriggerPort para SchedulerService (J.1) ---
    def trigger(
        self, schedule: Schedule, occurrence: ScheduleOccurrence
    ) -> Tuple[str, ExecutionStatus, Optional[Dict[str, Any]], Optional[str]]:
        """
        Conecta el SchedulerService con la Misión Continua.
        Cuando el scheduler produce una ocurrencia vencida, invoca execute_next_cycle().
        """
        cm = self.repository.get_by_schedule_id(schedule.schedule_id)
        if not cm:
            return (
                "",
                ExecutionStatus.FAILED,
                None,
                f"No ContinuousMission found for schedule_id {schedule.schedule_id}",
            )

        cycle = self.execute_next_cycle(cm.continuous_mission_id, occurrence=occurrence)

        if cycle.status == ContinuousCycleStatus.SUCCESS:
            return cycle.cycle_id, ExecutionStatus.SUCCESS, dict(cycle.result_summary), None
        elif cycle.status == ContinuousCycleStatus.FAILED:
            return cycle.cycle_id, ExecutionStatus.FAILED, dict(cycle.result_summary), cycle.error_message
        elif cycle.status == ContinuousCycleStatus.SKIPPED:
            return cycle.cycle_id, ExecutionStatus.SKIPPED, dict(cycle.result_summary), cycle.error_message
        elif cycle.status == ContinuousCycleStatus.UNKNOWN:
            return cycle.cycle_id, ExecutionStatus.UNKNOWN, dict(cycle.result_summary), cycle.error_message
        else:
            return cycle.cycle_id, ExecutionStatus.UNKNOWN, dict(cycle.result_summary), f"Cycle status: {cycle.status.value}"
