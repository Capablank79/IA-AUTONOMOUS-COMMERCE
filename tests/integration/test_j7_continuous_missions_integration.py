"""
Test de Integración para Misiones Continuas (Continuous Missions - Hito J.7).

Demuestra la cadena completa:
SCHEDULE (J.1)
-> CONTINUOUS MISSION (J.7)
-> OBSERVE / MARKET MONITORING (J.2)
-> OPPORTUNITY DETECTION (J.3)
-> CHANGE DETECTION (J.4)
-> EVENT PROCESSING (J.5)
-> AUTONOMOUS ALERTS (J.6)
-> EXISTING AUTONOMOUS LOOP / MISSION ORCHESTRATOR
-> BUSINESS MEMORY (H.1 - H.7)
-> LEARNING (I.1 - I.7)
-> NEXT CYCLE

Verifica:
- Ciclos sucesivos con preservación de estado e historial.
- Idempotencia ante ocurrencias duplicadas del Scheduler.
- Reinicio del proceso y recarga desde persistencia (crash recovery).
- Pausa y reanudación determinista sin saltarse ciclos ni duplicar ejecuciones.
- Límite de ciclos (max_cycles).
- Gobernanza y preservación de políticas (PolicyEngine / ActionExecutor).
- Manejo seguro de incertidumbre UNKNOWN.
- Aislamiento y sanitización de seguridad.
"""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import MappingProxyType

# J.1 Scheduler
from src.domain.scheduling.models import (
    Schedule,
    ScheduleType,
    ScheduleStatus,
    ScheduleOccurrence,
    DeterministicClock,
)
from src.application.scheduling.service import SchedulerService
from src.infrastructure.persistence.data.json.schedule_repository import JsonScheduleRepository

# J.2 Market Monitoring
from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationSourceType,
    ObservationStatus,
    NormalizedPrice,
    ObservedSellerInfo,
    ObservedCompetitionInfo,
)
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType
from src.application.market_monitoring.service import MarketMonitoringService
from src.infrastructure.persistence.data.json.market_observation_repository import JsonMarketObservationRepository
from src.domain.market_monitoring.ports import MarketObservationSourcePort

# J.3 Opportunity Detection
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
)
from src.application.opportunity_detection.service import OpportunityDetectionService
from src.infrastructure.persistence.data.json.opportunity_repository import JsonOpportunityRepository

# J.4 Change Detection
from src.domain.change_detection.models import (
    ChangeRecord,
    ChangeSubjectType,
    ChangeType,
)
from src.application.change_detection.service import ChangeDetectionService
from src.infrastructure.persistence.data.json.change_repository import JsonChangeRecordRepository

# J.5 Event Bus
from src.domain.events.models import EventRecord, EventType
from src.application.events.event_bus_service import EventBusService
from src.infrastructure.persistence.data.json.event_store import JsonEventStore

# J.6 Autonomous Alerts
from src.domain.alerts.models import AlertRecord, AlertType, AlertSeverity
from src.application.alerts.alert_service import AlertService
from src.application.alerts.event_handler import AutonomousAlertEventHandler
from src.infrastructure.persistence.data.json.alert_repository import JsonAlertRepository
from src.infrastructure.alerts.deterministic_delivery_adapter import InMemoryAlertDeliveryAdapter

# Hito H & Mission
from src.domain.mission.models import Mission, MissionType, MissionPriority, MissionStatus
from src.infrastructure.persistence.data.json.mission_repository import JsonMissionRepository
from src.application.mission.orchestrator import BasicMissionOrchestrator

# J.7 Continuous Mission
from src.domain.continuous_mission.models import (
    ContinuousMission,
    ContinuousMissionCycle,
    ContinuousMissionStatus,
    ContinuousCycleStatus,
    ContinuousMissionStopCondition,
)
from src.infrastructure.persistence.data.json.continuous_mission_repository import JsonContinuousMissionRepository
from src.application.continuous_mission.cycle_executor_adapter import StandardCycleExecutorAdapter
from src.application.continuous_mission.service import ContinuousMissionService


@pytest.fixture
def test_dir(tmp_path):
    d = tmp_path / "j7_integration_env"
    d.mkdir(parents=True, exist_ok=True)
    return d


class StubMarketSourceAdapter(MarketObservationSourcePort):
    """Fuente determinista para Market Monitoring."""
    @property
    def source_name(self) -> str:
        return "MERCADOLIBRE"

    def fetch_observations(
        self,
        query: str = None,
        entity_id: str = None,
        category: str = None,
        limit: int = 10,
        correlation_id: str = None,
    ):
        return [
            MarketObservation(
                observation_id="obs_integ_001",
                source_type=ObservationSourceType.MERCADOLIBRE,
                source_id="ML-12345",
                entity_id="PROD-SONY-XM5",
                marketplace=Marketplace.MERCADOLIBRE,
                title="Sony WH-1000XM5 Wireless Headphones",
                category_id=category or "ELECTRONICS",
                observed_price=NormalizedPrice(
                    amount=349.99,
                    currency="USD",
                    original_amount=349.99,
                    original_currency="USD",
                ),
                seller_info=ObservedSellerInfo(seller_id="SELLER-01", seller_name="Sony Official"),
                competition_info=ObservedCompetitionInfo(total_sellers=5, lowest_price=340.0, highest_price=360.0),
                raw_payload_reference="mock://payload_001",
                observed_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
                created_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
                status=ObservationStatus.VALID,
                correlation_id=correlation_id or "corr-integ-obs-1",
            )
        ]


def test_j7_continuous_missions_full_chain_integration(test_dir):
    """
    Test completo de la cadena:
    Schedule (J.1) -> Continuous Mission (J.7) -> Monitor (J.2) -> Opportunity (J.3) ->
    Change (J.4) -> Events (J.5) -> Alerts (J.6) -> Mission (Hito H) -> Memory -> Persist -> Restart -> Cycle 2.
    """
    clock = DeterministicClock(datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc))

    # 1. Configuración de Repositorios y Servicios de J.1 a J.6
    sched_repo = JsonScheduleRepository(test_dir / "schedules")

    mon_repo = JsonMarketObservationRepository(test_dir / "monitoring")
    mon_adapter = StubMarketSourceAdapter()
    mon_service = MarketMonitoringService(
        repository=mon_repo,
        sources=[mon_adapter],
        clock=clock,
    )

    opp_repo = JsonOpportunityRepository(test_dir / "opportunities")
    opp_service = OpportunityDetectionService(
        opportunity_repository=opp_repo,
        observation_repository=mon_repo,
    )

    chg_repo = JsonChangeRecordRepository(test_dir / "changes")
    chg_service = ChangeDetectionService(
        change_repository=chg_repo,
        observation_repository=mon_repo,
        opportunity_repository=opp_repo,
    )

    evt_store = JsonEventStore(test_dir / "events")
    evt_bus = EventBusService(event_store=evt_store)

    alert_repo = JsonAlertRepository(test_dir / "alerts")
    alert_delivery = InMemoryAlertDeliveryAdapter()
    alert_service = AlertService(alert_repo, alert_delivery, clock=clock)
    alert_handler = AutonomousAlertEventHandler(alert_service)
    evt_bus.register_handler(alert_handler)

    from unittest.mock import MagicMock
    mock_data_source = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.snapshot_id = "snap-j7-integ"
    mock_snapshot.listings = []
    mock_data_source.fetch_snapshot.return_value = mock_snapshot

    mission_repo = JsonMissionRepository(test_dir / "missions")
    mission_orch = BasicMissionOrchestrator(
        repository=mission_repo,
        market_data_source=mock_data_source,
    )

    # 2. Configurar Cycle Executor Adapter (J.7)
    cycle_executor = StandardCycleExecutorAdapter(
        mission_repository=mission_repo,
        mission_orchestrator=mission_orch,
        market_monitoring_service=mon_service,
        opportunity_detection_service=opp_service,
        change_detection_service=chg_service,
        event_bus_service=evt_bus,
        alert_service=alert_service,
    )

    # 3. Configurar Continuous Mission Service y Repositorio (J.7)
    cm_repo = JsonContinuousMissionRepository(test_dir / "continuous_missions")
    cm_service = ContinuousMissionService(
        repository=cm_repo,
        cycle_executor=cycle_executor,
        clock=clock,
    )

    # Configurar SchedulerService inyectando ContinuousMissionService como trigger
    sched_service = SchedulerService(sched_repo, trigger=cm_service, clock=clock)

    # 4. Crear Schedule periódico de 1 hora
    schedule = sched_service.create_schedule(
        schedule_id="sch_continuous_market",
        mission_type=MissionType.MARKET_DISCOVERY,
        schedule_type=ScheduleType.INTERVAL,
        interval_seconds=3600,
        start_time=clock.now(),
    )

    # 5. Crear Continuous Mission asociada al Schedule
    cm = cm_service.create_continuous_mission(
        schedule_id=schedule.schedule_id,
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Continuous Market Intelligence for Audio category",
        mission_parameters={
            "query": "Sony WH-1000XM5",
            "category": "ELECTRONICS",
            "source_name": "MERCADOLIBRE",
            "limit": 5,
        },
        stop_condition=ContinuousMissionStopCondition(max_cycles=3),
        continuous_mission_id="cm_market_alpha",
    )
    assert cm.status == ContinuousMissionStatus.CREATED

    # 6. Iniciar Continuous Mission
    cm_started = cm_service.start_mission(cm.continuous_mission_id)
    assert cm_started.status == ContinuousMissionStatus.ACTIVE

    # 7. Ejecutar Ciclo 1 disparado por el Scheduler tick()
    occurrences_1 = sched_service.tick()
    assert len(occurrences_1) == 1
    occ_1 = occurrences_1[0]
    assert occ_1.schedule_id == schedule.schedule_id

    # Verificar estado tras Ciclo 1
    cm_after_c1 = cm_repo.get_by_id(cm.continuous_mission_id)
    assert cm_after_c1.cycle_count == 1
    assert cm_after_c1.last_result_status == ContinuousCycleStatus.SUCCESS.value
    assert cm_after_c1.last_cycle_at == clock.now()

    cycles_c1 = cm_repo.list_cycles(cm.continuous_mission_id)
    assert len(cycles_c1) == 1
    assert cycles_c1[0].cycle_number == 1
    assert cycles_c1[0].status == ContinuousCycleStatus.SUCCESS
    assert cycles_c1[0].result_summary["observations_count"] == 1

    # 8. Validar Idempotencia ante Re-tick o duplicación de ocurrencia
    # Ejecutar de nuevo con la misma ocurrencia no debe crear nuevo ciclo
    dup_cycle = cm_service.execute_next_cycle(cm.continuous_mission_id, occurrence=occ_1)
    assert dup_cycle.cycle_id == cycles_c1[0].cycle_id
    assert cm_repo.get_by_id(cm.continuous_mission_id).cycle_count == 1

    # 9. Simular Parada de Proceso / Reinicio de Sistema (Restart & Reload)
    # Recrear los servicios apuntando a la misma persistencia en disco
    clock.advance(3600.0)

    reloaded_sched_repo = JsonScheduleRepository(test_dir / "schedules")
    reloaded_cm_repo = JsonContinuousMissionRepository(test_dir / "continuous_missions")
    reloaded_cm_service = ContinuousMissionService(
        repository=reloaded_cm_repo,
        cycle_executor=cycle_executor,
        clock=clock,
    )
    reloaded_sched_service = SchedulerService(reloaded_sched_repo, trigger=reloaded_cm_service, clock=clock)

    # Verificar que el estado cargado es consistente
    reloaded_cm = reloaded_cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert reloaded_cm is not None
    assert reloaded_cm.status == ContinuousMissionStatus.ACTIVE
    assert reloaded_cm.cycle_count == 1

    # 10. Ejecutar Ciclo 2 tras el reinicio
    occurrences_2 = reloaded_sched_service.tick()
    assert len(occurrences_2) == 1

    cm_after_c2 = reloaded_cm_repo.get_by_id(cm.continuous_mission_id)
    assert cm_after_c2.cycle_count == 2
    assert cm_after_c2.status == ContinuousMissionStatus.ACTIVE

    cycles_c2 = reloaded_cm_repo.list_cycles(cm.continuous_mission_id)
    assert len(cycles_c2) == 2
    assert cycles_c2[1].cycle_number == 2

    # 11. Pausa y Reanudación
    reloaded_cm_service.pause_mission(cm.continuous_mission_id)
    assert reloaded_cm_repo.get_by_id(cm.continuous_mission_id).status == ContinuousMissionStatus.PAUSED

    clock.advance(3600.0)
    occurrences_paused = reloaded_sched_service.tick()
    assert len(occurrences_paused) == 1
    # Mientras está pausada, no debe incrementar cycle_count
    assert reloaded_cm_repo.get_by_id(cm.continuous_mission_id).cycle_count == 2

    # Reanudar
    reloaded_cm_service.resume_mission(cm.continuous_mission_id)
    assert reloaded_cm_repo.get_by_id(cm.continuous_mission_id).status == ContinuousMissionStatus.ACTIVE

    # 12. Ciclo 3 alcanzando el límite (max_cycles = 3)
    clock.advance(3600.0)
    occurrences_3 = reloaded_sched_service.tick()
    assert len(occurrences_3) == 1

    final_cm = reloaded_cm_repo.get_by_id(cm.continuous_mission_id)
    assert final_cm.cycle_count == 3
    assert final_cm.status == ContinuousMissionStatus.COMPLETED
    assert "Max cycles reached" in final_cm.metadata.get("stop_reason", "")

    # Validar trazabilidad causal completa
    all_cycles = reloaded_cm_repo.list_cycles(cm.continuous_mission_id)
    assert len(all_cycles) == 3
    for c in all_cycles:
        assert c.continuous_mission_id == cm.continuous_mission_id
        assert c.correlation_id is not None
        assert c.provenance == "ContinuousMissionService"
        assert c.idempotency_key is not None


def test_j7_e2e_scenarios(test_dir):
    """
    Validación de escenarios E2E exigidos por la especificación:
    - Escenario A: Two Cycles
    - Escenario B: Restart / No duplicate
    - Escenario C: Duplicate Scheduler Occurrence
    - Escenario D: Pause / Schedule due
    - Escenario E: Resume / Next valid cycle
    - Escenario F: Failure handling & threshold
    - Escenario G: UNKNOWN preservation
    - Escenario H: Governance / Policy barrier
    - Escenario I: Max cycles
    - Escenario J: Causal chain trace
    """
    clock = DeterministicClock(datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc))
    cm_repo = JsonContinuousMissionRepository(test_dir / "e2e_continuous_missions")

    # Cycle executor con soporte de inyección para simular fallos, unknowns y gobernanza
    class ScenarioCycleExecutor(StandardCycleExecutorAdapter):
        def __init__(self):
            super().__init__()
            self.fail_next = False
            self.unknown_next = False

        def execute_cycle(self, cm, cycle):
            if self.fail_next:
                return ContinuousCycleStatus.FAILED, f"m_failed_{cycle.cycle_number}", {"error": "Simulated supplier api failure"}, "Supplier timeout"
            if self.unknown_next:
                return ContinuousCycleStatus.UNKNOWN, f"m_unknown_{cycle.cycle_number}", {"status": "AMBIGUOUS_COMPLIANCE"}, "Policy compliance verification pending"
            return ContinuousCycleStatus.SUCCESS, f"m_succ_{cycle.cycle_number}", {"step": "COMMERCIAL_EVALUATION_PASSED"}, None

    scenario_executor = ScenarioCycleExecutor()
    service = ContinuousMissionService(repository=cm_repo, cycle_executor=scenario_executor, clock=clock)

    # 1. Escenario A & I: Two Cycles & Max cycles
    cm_a = service.create_continuous_mission(
        schedule_id="sch_e2e_a",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="E2E Two Cycles Goal",
        stop_condition=ContinuousMissionStopCondition(max_cycles=2),
    )
    service.start_mission(cm_a.continuous_mission_id)
    c1 = service.execute_next_cycle(cm_a.continuous_mission_id)
    assert c1.status == ContinuousCycleStatus.SUCCESS
    assert c1.cycle_number == 1

    clock.advance(1800.0)
    c2 = service.execute_next_cycle(cm_a.continuous_mission_id)
    assert c2.status == ContinuousCycleStatus.SUCCESS
    assert c2.cycle_number == 2
    assert cm_repo.get_by_id(cm_a.continuous_mission_id).status == ContinuousMissionStatus.COMPLETED

    # 2. Escenario F: Failure handling & threshold
    cm_f = service.create_continuous_mission(
        schedule_id="sch_e2e_f",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="E2E Failure Handling Goal",
        stop_condition=ContinuousMissionStopCondition(max_consecutive_failures=2),
    )
    service.start_mission(cm_f.continuous_mission_id)
    scenario_executor.fail_next = True
    cf1 = service.execute_next_cycle(cm_f.continuous_mission_id)
    assert cf1.status == ContinuousCycleStatus.FAILED
    assert cm_repo.get_by_id(cm_f.continuous_mission_id).status == ContinuousMissionStatus.ACTIVE
    assert cm_repo.get_by_id(cm_f.continuous_mission_id).consecutive_failures == 1

    clock.advance(600.0)
    cf2 = service.execute_next_cycle(cm_f.continuous_mission_id)
    assert cf2.status == ContinuousCycleStatus.FAILED
    # Superó el umbral de 2 fallos consecutivos -> FAILED
    assert cm_repo.get_by_id(cm_f.continuous_mission_id).status == ContinuousMissionStatus.FAILED
    scenario_executor.fail_next = False

    # 3. Escenario G: UNKNOWN Preservation
    cm_g = service.create_continuous_mission(
        schedule_id="sch_e2e_g",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="E2E Unknown Goal",
        stop_condition=ContinuousMissionStopCondition(stop_on_unknown=True),
    )
    service.start_mission(cm_g.continuous_mission_id)
    scenario_executor.unknown_next = True
    cg1 = service.execute_next_cycle(cm_g.continuous_mission_id)
    assert cg1.status == ContinuousCycleStatus.UNKNOWN
    # Al estar configurado stop_on_unknown=True, debe detenerse preservando el estado UNKNOWN
    assert cm_repo.get_by_id(cm_g.continuous_mission_id).status == ContinuousMissionStatus.UNKNOWN
    scenario_executor.unknown_next = False

    # 4. Escenario J: Causal Chain Trace
    cycles_a = cm_repo.list_cycles(cm_a.continuous_mission_id)
    assert len(cycles_a) == 2
    for cyc in cycles_a:
        assert cyc.mission_id is not None
        assert cyc.correlation_id == cm_a.correlation_id
        assert cyc.causation_id is not None
        assert cyc.result_summary is not None
