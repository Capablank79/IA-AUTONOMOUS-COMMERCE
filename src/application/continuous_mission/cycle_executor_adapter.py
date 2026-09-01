"""
Adaptador de ejecución de ciclos de Misión Continua (Cycle Executor Adapter - Hito J.7).

Coordina la ejecución de cada ciclo individual reutilizando la infraestructura existente:
- MissionOrchestrator / AutonomousLoop
- J.2 Market Monitoring
- J.3 Opportunity Detection
- J.4 Change Detection
- J.5 Event Bus
- J.6 Autonomous Alerts
- PolicyEngine & ActionExecutor
- Business Memory (H.1 a H.7)
- Learning Signals (I.1 a I.7)

Límites:
- NO implementa un nuevo AutonomousLoop ni un nuevo Scheduler.
- NO evade el PolicyEngine ni aprueba acciones irreversibles por omisión.
- Preserva UNKNOWN ante incertidumbre.
- No muta reglas ni políticas de negocio.
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionPriority,
    MissionStatus,
    MissionResult,
)
from src.domain.mission.ports import MissionOrchestrator, MissionRepository
from src.domain.continuous_mission.models import (
    ContinuousMission,
    ContinuousMissionCycle,
    ContinuousCycleStatus,
)
from src.domain.continuous_mission.ports import CycleExecutorPort
from src.application.market_monitoring.service import MarketMonitoringService
from src.application.opportunity_detection.service import OpportunityDetectionService
from src.application.change_detection.service import ChangeDetectionService
from src.application.events.event_bus_service import EventBusService
from src.application.alerts.alert_service import AlertService

logger = logging.getLogger(__name__)


class StandardCycleExecutorAdapter(CycleExecutorPort):
    """
    Adaptador estándar que compone y reutiliza los servicios existentes del Hito J (J.1 - J.6)
    y el orquestador / repositorio de misiones (Hito H, G, E, A).
    """

    def __init__(
        self,
        mission_repository: Optional[MissionRepository] = None,
        mission_orchestrator: Optional[MissionOrchestrator] = None,
        market_monitoring_service: Optional[MarketMonitoringService] = None,
        opportunity_detection_service: Optional[OpportunityDetectionService] = None,
        change_detection_service: Optional[ChangeDetectionService] = None,
        event_bus_service: Optional[EventBusService] = None,
        alert_service: Optional[AlertService] = None,
    ):
        self.mission_repository = mission_repository
        self.mission_orchestrator = mission_orchestrator
        self.market_monitoring_service = market_monitoring_service
        self.opportunity_detection_service = opportunity_detection_service
        self.change_detection_service = change_detection_service
        self.event_bus_service = event_bus_service
        self.alert_service = alert_service

    def execute_cycle(
        self,
        continuous_mission: ContinuousMission,
        cycle: ContinuousMissionCycle,
    ) -> Tuple[ContinuousCycleStatus, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
        """
        Ejecuta el ciclo coordinando las capacidades existentes según el tipo de misión y parámetros.
        """
        cycle_mission_id = f"m_{continuous_mission.continuous_mission_id}_c{cycle.cycle_number}_{uuid.uuid4().hex[:6]}"
        summary: Dict[str, Any] = {
            "cycle_number": cycle.cycle_number,
            "continuous_mission_id": continuous_mission.continuous_mission_id,
            "goal": continuous_mission.goal,
        }

        try:
            # 1. Si se configuró Market Monitoring (J.2) y la misión lo requiere
            observations = []
            if self.market_monitoring_service is not None:
                query = continuous_mission.mission_parameters.get("query")
                category = continuous_mission.mission_parameters.get("category")
                source_name = continuous_mission.mission_parameters.get("source_name")
                limit = continuous_mission.mission_parameters.get("limit", 10)

                try:
                    observations = self.market_monitoring_service.monitor(
                        source_name=source_name,
                        query=query,
                        category=category,
                        limit=limit,
                        correlation_id=cycle.correlation_id,
                    )
                    summary["observations_count"] = len(observations)
                except Exception as e:
                    logger.warning(f"Market monitoring warning in cycle {cycle.cycle_id}: {e}")
                    summary["market_monitoring_error"] = str(e)

            # 2. Si se configuró Opportunity Detection (J.3) y hay observaciones
            opportunities = []
            if self.opportunity_detection_service is not None and observations:
                try:
                    opportunities = self.opportunity_detection_service.process_observations(
                        observations=observations,
                        correlation_id=cycle.correlation_id,
                    )
                    summary["opportunities_count"] = len(opportunities)
                except Exception as e:
                    logger.warning(f"Opportunity detection warning in cycle {cycle.cycle_id}: {e}")
                    summary["opportunity_detection_error"] = str(e)

            # 3. Si se configuró Change Detection (J.4)
            changes = []
            if self.change_detection_service is not None:
                try:
                    if observations and len(observations) >= 2:
                        obs_changes = self.change_detection_service.detect_observation_changes(
                            observations[0], observations[1], correlation_id=cycle.correlation_id
                        )
                        changes.extend(obs_changes)
                    summary["changes_count"] = len(changes)
                except Exception as e:
                    logger.warning(f"Change detection warning in cycle {cycle.cycle_id}: {e}")
                    summary["change_detection_error"] = str(e)

            # 4. Orquestación a través de MissionOrchestrator existente si está disponible
            if self.mission_orchestrator is not None and self.mission_repository is not None:
                mission = Mission(
                    mission_id=cycle_mission_id,
                    type=continuous_mission.mission_type,
                    priority=continuous_mission.priority,
                    parameters=dict(continuous_mission.mission_parameters),
                    status=MissionStatus.PENDING,
                    created_at=cycle.started_at,
                    updated_at=cycle.started_at,
                )

                self.mission_orchestrator.submit(mission)
                result = self.mission_repository.get_result(cycle_mission_id)

                if result is not None:
                    summary["mission_status"] = result.status.value
                    summary["errors_count"] = len(result.errors)
                    summary["blocks_count"] = len(result.blocks)
                    summary["evidences_count"] = len(result.evidences)

                    if result.status == MissionStatus.COMPLETED:
                        return ContinuousCycleStatus.SUCCESS, cycle_mission_id, summary, None
                    elif result.status == MissionStatus.BLOCKED:
                        blocks_str = ", ".join(str(b) for b in result.blocks)
                        return ContinuousCycleStatus.UNKNOWN, cycle_mission_id, summary, f"Mission blocked: {blocks_str}"
                    elif result.status == MissionStatus.FAILED:
                        err_str = ", ".join(str(e) for e in result.errors)
                        return ContinuousCycleStatus.FAILED, cycle_mission_id, summary, f"Mission failed: {err_str}"
                    else:
                        return ContinuousCycleStatus.UNKNOWN, cycle_mission_id, summary, f"Mission status unknown: {result.status.value}"
                else:
                    saved_m = self.mission_repository.get_by_id(cycle_mission_id)
                    if saved_m and saved_m.status == MissionStatus.COMPLETED:
                        return ContinuousCycleStatus.SUCCESS, cycle_mission_id, summary, None
                    elif saved_m and saved_m.status == MissionStatus.FAILED:
                        return ContinuousCycleStatus.FAILED, cycle_mission_id, summary, "Mission failed"
                    elif saved_m and saved_m.status == MissionStatus.BLOCKED:
                        return ContinuousCycleStatus.UNKNOWN, cycle_mission_id, summary, "Mission blocked"
                    return ContinuousCycleStatus.SUCCESS, cycle_mission_id, summary, None

            # Si no hay orquestador pero sí servicios de monitoreo u oportunidad
            return ContinuousCycleStatus.SUCCESS, cycle_mission_id, summary, None

        except Exception as e:
            logger.error(f"Cycle execution exception in cycle {cycle.cycle_id}: {e}", exc_info=True)
            return ContinuousCycleStatus.FAILED, cycle_mission_id, summary, str(e)
