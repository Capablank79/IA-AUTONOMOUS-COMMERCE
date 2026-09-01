from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
import uuid

from src.domain.scheduling.models import (
    Clock,
    SystemClock,
    Schedule,
    ScheduleConfig,
    ScheduleOccurrence,
    ScheduleStatus,
    ScheduleType,
    ExecutionStatus,
    MissedExecutionPolicy,
)
from src.domain.scheduling.ports import ScheduleRepository, MissionTriggerPort


class SchedulerService:
    """
    Servicio de aplicación de Scheduling (Hito J.1).
    Coordina la creación, actualización, verificación de vencimiento y ejecución de schedules.
    Garantiza:
    - Idempotencia determinista por ocurrencia (schedule_id + scheduled_at).
    - Preservación de estados UNKNOWN y registro de errores.
    - Soporte completo para reinicio/recuperación mediante persistencia hexagonal.
    - Inyección de Clock determinista para testing temporal sin sleeps.
    """

    def __init__(
        self,
        repository: ScheduleRepository,
        trigger: MissionTriggerPort,
        clock: Optional[Clock] = None,
    ):
        self.repository = repository
        self.trigger = trigger
        self.clock = clock or SystemClock()

    def create_schedule(
        self,
        schedule_id: str,
        mission_type: Any,
        mission_parameters: Optional[Dict[str, Any]] = None,
        schedule_type: ScheduleType = ScheduleType.INTERVAL,
        interval_seconds: Optional[int] = None,
        cron_expression: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        timezone_str: str = "UTC",
        missed_policy: MissedExecutionPolicy = MissedExecutionPolicy.SKIP,
        max_occurrences: Optional[int] = None,
        priority: Any = None,
        correlation_id: Optional[str] = None,
        provenance: Optional[str] = None,
    ) -> Schedule:
        now = self.clock.now()

        # Validación de parámetros
        if schedule_type == ScheduleType.INTERVAL and (interval_seconds is None or interval_seconds <= 0):
            raise ValueError("interval_seconds must be a positive integer for INTERVAL schedule type")

        config = ScheduleConfig(
            interval_seconds=interval_seconds,
            cron_expression=cron_expression,
            start_time=start_time,
            end_time=end_time,
            timezone_str=timezone_str,
            missed_policy=missed_policy,
            max_occurrences=max_occurrences,
        )

        next_run_at = start_time if start_time and start_time > now else now

        from src.domain.mission.models import MissionPriority
        prio = priority or MissionPriority.MEDIUM

        schedule = Schedule(
            schedule_id=schedule_id,
            mission_type=mission_type,
            mission_parameters=mission_parameters or {},
            schedule_type=schedule_type,
            config=config,
            status=ScheduleStatus.ACTIVE,
            priority=prio,
            next_run_at=next_run_at,
            last_run_at=None,
            total_runs=0,
            created_at=now,
            updated_at=now,
            correlation_id=correlation_id,
            provenance=provenance,
        )

        self.repository.save(schedule)
        return schedule

    def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        return self.repository.get_by_id(schedule_id)

    def enable_schedule(self, schedule_id: str) -> Optional[Schedule]:
        sched = self.repository.get_by_id(schedule_id)
        if not sched:
            return None
        now = self.clock.now()
        # Si next_run_at estaba en el pasado o nulo, recalcular
        next_run = sched.next_run_at
        if next_run is None or next_run < now:
            next_run = now

        updated = Schedule(
            schedule_id=sched.schedule_id,
            mission_type=sched.mission_type,
            mission_parameters=sched.mission_parameters,
            schedule_type=sched.schedule_type,
            config=sched.config,
            status=ScheduleStatus.ACTIVE,
            priority=sched.priority,
            next_run_at=next_run,
            last_run_at=sched.last_run_at,
            total_runs=sched.total_runs,
            created_at=sched.created_at,
            updated_at=now,
            correlation_id=sched.correlation_id,
            provenance=sched.provenance,
        )
        self.repository.save(updated)
        return updated

    def disable_schedule(self, schedule_id: str) -> Optional[Schedule]:
        sched = self.repository.get_by_id(schedule_id)
        if not sched:
            return None
        now = self.clock.now()
        updated = Schedule(
            schedule_id=sched.schedule_id,
            mission_type=sched.mission_type,
            mission_parameters=sched.mission_parameters,
            schedule_type=sched.schedule_type,
            config=sched.config,
            status=ScheduleStatus.DISABLED,
            priority=sched.priority,
            next_run_at=sched.next_run_at,
            last_run_at=sched.last_run_at,
            total_runs=sched.total_runs,
            created_at=sched.created_at,
            updated_at=now,
            correlation_id=sched.correlation_id,
            provenance=sched.provenance,
        )
        self.repository.save(updated)
        return updated

    def tick(self) -> List[ScheduleOccurrence]:
        """
        Evalúa el estado temporal en el instante actual (según self.clock.now()).
        Ejecuta todos los schedules activos que hayan alcanzado su next_run_at.
        Retorna la lista de ocurrencias procesadas.
        """
        now = self.clock.now()
        due_schedules = self.repository.list_due(now)
        executed_occurrences = []

        for schedule in due_schedules:
            occurrence = self._process_due_schedule(schedule, now)
            if occurrence:
                executed_occurrences.append(occurrence)

        return executed_occurrences

    def _generate_idempotency_key(self, schedule_id: str, scheduled_at: datetime) -> str:
        # Clave determinista basada en el schedule y el instante planificado
        return f"occ_{schedule_id}_{scheduled_at.isoformat()}"

    def _process_due_schedule(self, schedule: Schedule, current_time: datetime) -> Optional[ScheduleOccurrence]:
        scheduled_at = schedule.next_run_at or current_time
        idempotency_key = self._generate_idempotency_key(schedule.schedule_id, scheduled_at)

        # Verificar si la ocurrencia ya fue ejecutada o registrada previamente
        existing_occ = self.repository.get_occurrence_by_idempotency_key(idempotency_key)
        if existing_occ is not None:
            # Ya procesada: no duplicar la ejecución
            # Solo actualizar el next_run_at del schedule si está trabado
            next_run = schedule.compute_next_run(current_time)
            updated_sched = Schedule(
                schedule_id=schedule.schedule_id,
                mission_type=schedule.mission_type,
                mission_parameters=schedule.mission_parameters,
                schedule_type=schedule.schedule_type,
                config=schedule.config,
                status=schedule.status if next_run is not None else ScheduleStatus.COMPLETED,
                priority=schedule.priority,
                next_run_at=next_run,
                last_run_at=schedule.last_run_at,
                total_runs=schedule.total_runs,
                created_at=schedule.created_at,
                updated_at=current_time,
                correlation_id=schedule.correlation_id,
                provenance=schedule.provenance,
            )
            self.repository.save(updated_sched)
            return existing_occ

        occurrence_id = f"occ_{uuid.uuid4().hex[:12]}"
        occurrence = ScheduleOccurrence(
            occurrence_id=occurrence_id,
            schedule_id=schedule.schedule_id,
            scheduled_at=scheduled_at,
            idempotency_key=idempotency_key,
            triggered_at=current_time,
            status=ExecutionStatus.RUNNING,
        )
        self.repository.save_occurrence(occurrence)

        # Disparar mediante el trigger adapter desacoplado
        try:
            mission_id, exec_status, summary, error_msg = self.trigger.trigger(schedule, occurrence)
        except Exception as e:
            mission_id = None
            exec_status = ExecutionStatus.FAILED
            summary = None
            error_msg = str(e)

        # Actualizar ocurrencia con el resultado final
        final_occurrence = ScheduleOccurrence(
            occurrence_id=occurrence.occurrence_id,
            schedule_id=schedule.schedule_id,
            scheduled_at=scheduled_at,
            idempotency_key=idempotency_key,
            triggered_at=current_time,
            mission_id=mission_id,
            status=exec_status,
            result_summary=summary,
            error=error_msg,
        )
        self.repository.save_occurrence(final_occurrence)

        # Calcular próximo run y actualizar schedule
        next_run = schedule.compute_next_run(current_time)
        new_status = schedule.status
        if next_run is None:
            new_status = ScheduleStatus.COMPLETED

        updated_sched = Schedule(
            schedule_id=schedule.schedule_id,
            mission_type=schedule.mission_type,
            mission_parameters=schedule.mission_parameters,
            schedule_type=schedule.schedule_type,
            config=schedule.config,
            status=new_status,
            priority=schedule.priority,
            next_run_at=next_run,
            last_run_at=current_time,
            total_runs=schedule.total_runs + 1,
            created_at=schedule.created_at,
            updated_at=current_time,
            correlation_id=schedule.correlation_id,
            provenance=schedule.provenance,
        )
        self.repository.save(updated_sched)

        return final_occurrence
