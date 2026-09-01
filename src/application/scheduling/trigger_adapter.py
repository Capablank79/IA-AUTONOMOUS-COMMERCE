from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
import uuid

from src.domain.mission.models import Mission, MissionStatus, MissionResult
from src.domain.mission.ports import MissionOrchestrator, MissionRepository
from src.domain.scheduling.models import Schedule, ScheduleOccurrence, ExecutionStatus
from src.domain.scheduling.ports import MissionTriggerPort


class MissionOrchestratorTriggerAdapter(MissionTriggerPort):
    """
    Adaptador de MissionTriggerPort que conecta con el MissionOrchestrator y MissionRepository existentes.
    Preserva el estado UNKNOWN de manera determinista ante incertidumbre o fallos.
    No implementa lógica de marketplace ni políticas.
    """

    def __init__(self, orchestrator: MissionOrchestrator, mission_repository: MissionRepository):
        self.orchestrator = orchestrator
        self.mission_repository = mission_repository

    def trigger(
        self, schedule: Schedule, occurrence: ScheduleOccurrence
    ) -> Tuple[str, ExecutionStatus, Optional[Dict[str, Any]], Optional[str]]:
        mission_id = f"m_{schedule.schedule_id}_{uuid.uuid4().hex[:8]}"

        mission = Mission(
            mission_id=mission_id,
            type=schedule.mission_type,
            priority=schedule.priority,
            parameters=dict(schedule.mission_parameters),
            status=MissionStatus.PENDING,
            created_at=occurrence.triggered_at or datetime.now(timezone.utc),
            updated_at=occurrence.triggered_at or datetime.now(timezone.utc),
        )

        try:
            self.orchestrator.submit(mission)

            # Obtener resultado si está disponible
            result = self.mission_repository.get_result(mission_id)
            if result is None:
                # Si el orquestador no devolvió un resultado inmediato, consultar el estado de la misión
                saved_mission = self.mission_repository.get_by_id(mission_id)
                if saved_mission and saved_mission.status == MissionStatus.RUNNING:
                    return mission_id, ExecutionStatus.RUNNING, None, None
                elif saved_mission and saved_mission.status == MissionStatus.COMPLETED:
                    return mission_id, ExecutionStatus.SUCCESS, None, None
                elif saved_mission and saved_mission.status == MissionStatus.BLOCKED:
                    return mission_id, ExecutionStatus.UNKNOWN, None, "Mission blocked / uncertain"
                elif saved_mission and saved_mission.status == MissionStatus.FAILED:
                    return mission_id, ExecutionStatus.FAILED, None, "Mission failed"
                return mission_id, ExecutionStatus.UNKNOWN, None, "No result available (UNKNOWN)"

            summary = {
                "status": result.status.value,
                "evidences_count": len(result.evidences),
                "errors_count": len(result.errors),
                "blocks_count": len(result.blocks),
            }

            if result.status == MissionStatus.COMPLETED:
                return mission_id, ExecutionStatus.SUCCESS, summary, None
            elif result.status == MissionStatus.BLOCKED:
                blocks_str = ", ".join(str(b) for b in result.blocks)
                return mission_id, ExecutionStatus.UNKNOWN, summary, f"Mission blocked: {blocks_str}"
            elif result.status == MissionStatus.FAILED:
                errors_str = ", ".join(str(e) for e in result.errors)
                return mission_id, ExecutionStatus.FAILED, summary, f"Mission failed: {errors_str}"
            else:
                return mission_id, ExecutionStatus.UNKNOWN, summary, f"Unknown status: {result.status.value}"

        except Exception as e:
            return mission_id, ExecutionStatus.FAILED, None, str(e)
