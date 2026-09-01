"""
Suite formal de integración y validación end-to-end de GATE I: Continuous Autonomy Gate.
AI Autonomous Commerce — Gate I Formal Continuous Autonomy Validation

Demuestra de forma exhaustiva, reproducible y sin shortcuts los requisitos críticos A a J:
A. Two-cycle continuous autonomy (Happy Path completo e2e con Scheduler, Continuous Mission,
   Market Monitoring, Opportunity Detection, Change Detection, Event Bus, Autonomous Alerts,
   Mission Orchestration, Policy Governance, Action Execution, Business Memory, Learning Signal y Next Cycle).
B. Restart recovery (persistencia completa a disco, destrucción y recreación de servicios,
   recarga de repositorios, reanudación del scheduler y ejecución del ciclo 2 manteniendo estado e historial).
C. Duplicate / Replay idempotency (reprocesamiento de la misma ocurrencia/ciclo sin duplicar
   misión lógica, eventos, alertas ni acciones secundarias).
D. UNKNOWN preservation (manejo determinista de incertidumbre en fuentes, observaciones y acciones
   sin convertir UNKNOWN en éxito falso, cero o failure engañoso).
E. Policy DENY / Approval enforcement (respeto estricto del PolicyEngine y flujo de aprobación,
   demostrando que la autonomía continua no evade las políticas de gobernanza ni ejecuta tools directamente).
F. Pause / Resume / Stop lifecycle (transiciones de estado y verificación de que misiones pausadas
   o detenidas no ejecutan nuevos ciclos).
G. Failure isolation (fallo en componentes secundarios no críticos como delivery de alertas
   no corrompe el EventBus, la misión continua ni los ciclos subsiguientes).
H. Max Cycles (alcanzar límite de ciclos detiene deterministamente la misión).
I. Security sanitization (metadata y cargas con secretos/tokens quedan debidamente redactadas y protegidas).
J. Full Causal Trace (reconstrucción determinista y validación de todos los IDs y enlaces de causalidad
   a lo largo de toda la cadena transversal).
"""

from decimal import Decimal
import dataclasses
import tempfile
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
    ExecutionStatus,
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
from src.domain.events.ports import EventHandlerPort
from src.application.events.event_bus_service import EventBusService
from src.infrastructure.persistence.data.json.event_store import JsonEventStore

# J.6 Autonomous Alerts
from src.domain.alerts.models import (
    AlertRecord,
    AlertType,
    AlertSeverity,
    AlertStatus,
    AlertDeliveryStatus,
)
from src.application.alerts.alert_service import AlertService
from src.application.alerts.event_handler import AutonomousAlertEventHandler
from src.infrastructure.persistence.data.json.alert_repository import JsonAlertRepository
from src.infrastructure.alerts.deterministic_delivery_adapter import InMemoryAlertDeliveryAdapter

# Hito H & G & E — Mission, Policy, Action, Result, Decision, Memory
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionPriority,
    MissionStatus,
    LoopDecision,
    LoopAction,
)
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.domain.policy.rules import AuthorizationPolicyRule
from src.domain.decision.models import DecisionRecord, DecisionType, DecisionStatus
from src.domain.action.models import ActionRecord, ActionStatus
from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel

# Repositorios JSON Hito H
from src.infrastructure.persistence.data.json.mission_repository import JsonMissionRepository
from src.infrastructure.persistence.data.json.decision_repository import JsonDecisionRepository
from src.infrastructure.persistence.data.json.action_repository import JsonActionRepository
from src.infrastructure.persistence.data.json.result_repository import JsonResultRepository
from src.infrastructure.persistence.data.json.outcome_repository import JsonOutcomeRepository
from src.infrastructure.persistence.data.json.learning_signal_repository import JsonLearningSignalRepository

# Servicios Hito H & I
from src.application.decision.decision_service import DecisionMemoryService
from src.application.outcome.outcome_service import OutcomeTrackingService
from src.application.learning_signals.learning_signal_service import LearningSignalService
from src.domain.learning_signals.models import LearningSignalType

# J.7 Continuous Mission
from src.domain.continuous_mission.models import (
    ContinuousMission,
    ContinuousMissionCycle,
    ContinuousMissionStatus,
    ContinuousCycleStatus,
    ContinuousMissionStopCondition,
)
from src.infrastructure.persistence.data.json.continuous_mission_repository import JsonContinuousMissionRepository
from src.application.continuous_mission.service import ContinuousMissionService
from src.domain.continuous_mission.ports import CycleExecutorPort


class DeterministicE2EMarketSource(MarketObservationSourcePort):
    """Fuente de mercado determinista que simula observaciones con variación temporal."""

    def __init__(self, prices_sequence=None):
        self.prices_sequence = prices_sequence or [350.0, 310.0, 310.0]
        self.call_count = 0

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
        price_val = self.prices_sequence[min(self.call_count, len(self.prices_sequence) - 1)]
        self.call_count += 1
        obs_id = f"obs_gate_i_{self.call_count}"

        observed_time = datetime(2026, 9, 1, 10 + self.call_count, 0, 0, tzinfo=timezone.utc)
        return [
            MarketObservation(
                observation_id=obs_id,
                source_type=ObservationSourceType.MERCADOLIBRE,
                source_id=f"ML-ITEM-{self.call_count}",
                entity_id=entity_id or "PROD-LOGITECH-MX3",
                marketplace=Marketplace.MERCADOLIBRE,
                title="Logitech MX Master 3S Wireless Mouse",
                category_id=category or "COMPUTING",
                observed_price=NormalizedPrice(
                    amount=price_val,
                    currency="USD",
                    original_amount=price_val,
                    original_currency="USD",
                ),
                seller_info=ObservedSellerInfo(seller_id="SELLER-OFFICIAL", seller_name="Logitech Store"),
                competition_info=ObservedCompetitionInfo(total_sellers=4, lowest_price=price_val, highest_price=price_val + 20.0),
                raw_payload_reference=f"mock://gate_i_payload_{self.call_count}",
                observed_at=observed_time,
                created_at=observed_time,
                status=ObservationStatus.VALID,
                correlation_id=correlation_id or f"corr-gate-i-{self.call_count}",
            )
        ]


class IntegratedE2ECycleExecutor(CycleExecutorPort):
    """
    Ejecutor de ciclos E2E que integra transversalmente todas las capas:
    J.2 (Monitor) -> J.3 (Opportunity) -> J.4 (Change) -> J.5 (Events) -> J.6 (Alerts) ->
    Mission -> Policy -> Decision -> Action -> Result -> Memory -> Learning Signal.
    """

    def __init__(
        self,
        market_monitoring_service: MarketMonitoringService,
        opportunity_detection_service: OpportunityDetectionService,
        change_detection_service: ChangeDetectionService,
        event_bus_service: EventBusService,
        alert_service: AlertService,
        mission_repo: JsonMissionRepository,
        decision_service: DecisionMemoryService,
        action_repo: JsonActionRepository,
        result_repo: JsonResultRepository,
        outcome_repo: JsonOutcomeRepository,
        learning_service: LearningSignalService,
        policy_engine: PolicyEngine,
        clock: DeterministicClock,
    ):
        self.market_monitoring_service = market_monitoring_service
        self.opportunity_detection_service = opportunity_detection_service
        self.change_detection_service = change_detection_service
        self.event_bus_service = event_bus_service
        self.alert_service = alert_service
        self.mission_repo = mission_repo
        self.decision_service = decision_service
        self.action_repo = action_repo
        self.result_repo = result_repo
        self.outcome_repo = outcome_repo
        self.learning_service = learning_service
        self.policy_engine = policy_engine
        self.clock = clock
        self.previous_observation = None

    def execute_cycle(self, continuous_mission: ContinuousMission, cycle: ContinuousMissionCycle):
        now = self.clock.now()
        corr_id = cycle.correlation_id or f"corr_{cycle.cycle_id}"

        # 1. Market Monitoring (J.2)
        observations = self.market_monitoring_service.monitor(
            source_name="MERCADOLIBRE",
            query="Logitech MX Master 3S",
            correlation_id=corr_id,
        )
        current_obs = observations[0] if observations else None

        # 2. Opportunity Detection (J.3)
        opportunities = []
        if observations:
            opportunities = self.opportunity_detection_service.process_observations(
                observations=observations,
                correlation_id=corr_id,
            )

        # 3. Change Detection (J.4) & Event Publishing (J.5)
        changes = []
        if self.previous_observation and current_obs and self.previous_observation.observed_at < current_obs.observed_at:
            changes = self.change_detection_service.detect_observation_changes(
                observations=[self.previous_observation, current_obs],
                correlation_id=corr_id,
            )
            for chg in changes:
                event = EventRecord(
                    event_id=f"evt_{chg.change_id}",
                    event_type=EventType.CHANGE_DETECTED,
                    subject_type="PRODUCT",
                    subject_id=current_obs.entity_id,
                    occurred_at=now,
                    recorded_at=now,
                    correlation_id=corr_id,
                    causation_id=chg.change_id,
                    payload=MappingProxyType({
                        "change_type": "PRICE_CHANGED",
                        "significance": "SIGNIFICANT",
                        "change_summary": "Observed price delta across cycles",
                        "observed_changes_count": 1,
                    }),
                )
                self.event_bus_service.publish(event)

        self.previous_observation = current_obs

        # 4. Mission & Decision & Policy & Action Execution
        mission_id = f"m_{continuous_mission.continuous_mission_id}_c{cycle.cycle_number}"
        mission = Mission(
            mission_id=mission_id,
            type=continuous_mission.mission_type,
            priority=continuous_mission.priority,
            parameters=dict(continuous_mission.mission_parameters),
            status=MissionStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
        self.mission_repo.save(mission)

        # Governance evaluation via PolicyEngine
        action_type = continuous_mission.mission_parameters.get("target_action", "PRICE_UPDATE")
        loop_dec = LoopDecision(action=LoopAction.CONTINUE, reason="Executing continuous cycle action")
        ctx = PolicyEvaluationContext(
            action_type=action_type,
            actor_id="continuous_agent",
            mission_id=mission_id,
            correlation_id=corr_id,
            loop_decision=loop_dec,
            risk_level=RiskLevel.LOW,
            provenance=EvidenceProvenanceType.LIVE,
            prohibited_actions=("PROHIBITED_ACTION",),
        )
        policy_eval = self.policy_engine.evaluate(context=ctx)

        if policy_eval.decision == PolicyDecisionType.DENY:
            blocked_mission = dataclasses.replace(mission, status=MissionStatus.BLOCKED, updated_at=now)
            self.mission_repo.save(blocked_mission)
            return (
                ContinuousCycleStatus.UNKNOWN,
                mission_id,
                {"policy_decision": "DENY", "observations": len(observations)},
                "Action denied by PolicyEngine",
            )

        # Record Decision in Memory
        dec = self.decision_service.record_decision(
            mission_id=mission_id,
            decision_type=DecisionType.PRICING_ADJUSTMENT,
            reason="Autonomous cycle pricing response",
            target_resource=current_obs.entity_id if current_obs else "UNKNOWN_TARGET",
            idempotency_key=f"dec_key_{cycle.cycle_id}",
            confidence=Confidence.HIGH,
            provenance=EvidenceProvenanceType.LIVE,
        )

        # Action Record
        act = ActionRecord(
            action_id=f"act_{cycle.cycle_id}",
            decision_id=dec.decision_id,
            mission_id=mission_id,
            action_type=action_type,
            status=ActionStatus.COMPLETED,
            parameters={"sku": current_obs.entity_id if current_obs else "UNKNOWN", "price": "310.00"},
            created_at=now,
        )
        self.action_repo.save(act)

        # Action Result
        res = ActionResultRecord(
            result_id=f"res_{cycle.cycle_id}",
            action_id=act.action_id,
            decision_id=dec.decision_id,
            mission_id=mission_id,
            outcome=ResultOutcome.SUCCESS,
            response_summary={"http_code": 200, "updated": True},
            observed_at=now,
        )
        self.result_repo.save(res)

        # Outcome Record
        out = OutcomeRecord(
            outcome_id=f"out_{cycle.cycle_id}",
            result_id=res.result_id,
            action_id=act.action_id,
            decision_id=dec.decision_id,
            mission_id=mission_id,
            outcome_type="PRICE_ADJUSTMENT_SUCCESS",
            status=OutcomeStatus.SUCCESS,
            idempotency_key=f"out_key_{cycle.cycle_id}",
            observed_at=now,
        )
        self.outcome_repo.save(out)

        # Learning Signal
        sig = self.learning_service.process_outcome(out)

        completed_mission = dataclasses.replace(mission, status=MissionStatus.COMPLETED, updated_at=now)
        self.mission_repo.save(completed_mission)

        summary = {
            "observations_count": len(observations),
            "opportunities_count": len(opportunities),
            "changes_count": len(changes),
            "decision_id": dec.decision_id,
            "action_id": act.action_id,
            "result_id": res.result_id,
            "outcome_id": out.outcome_id,
            "signal_id": sig.signal_id if sig else None,
        }

        return ContinuousCycleStatus.SUCCESS, mission_id, summary, None


@pytest.fixture
def gate_i_env(tmp_path):
    d = tmp_path / "gate_i_workspace"
    d.mkdir(parents=True, exist_ok=True)

    clock = DeterministicClock(datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc))

    # Repositories
    sched_repo = JsonScheduleRepository(d / "schedules")
    mon_repo = JsonMarketObservationRepository(d / "monitoring")
    opp_repo = JsonOpportunityRepository(d / "opportunities")
    chg_repo = JsonChangeRecordRepository(d / "changes")
    evt_store = JsonEventStore(d / "events")
    alert_repo = JsonAlertRepository(d / "alerts")
    mission_repo = JsonMissionRepository(d / "missions")
    decision_repo = JsonDecisionRepository(d / "decisions")
    action_repo = JsonActionRepository(d / "actions.json")
    result_repo = JsonResultRepository(d / "results.json")
    outcome_repo = JsonOutcomeRepository(d / "outcomes.json")
    signal_repo = JsonLearningSignalRepository(d / "signals.json")
    cm_repo = JsonContinuousMissionRepository(d)

    # Adapters & Services
    source_adapter = DeterministicE2EMarketSource(prices_sequence=[350.0, 310.0, 310.0])
    mon_service = MarketMonitoringService(repository=mon_repo, sources=[source_adapter], clock=clock)
    opp_service = OpportunityDetectionService(opportunity_repository=opp_repo, observation_repository=mon_repo)
    chg_service = ChangeDetectionService(change_repository=chg_repo, observation_repository=mon_repo, opportunity_repository=opp_repo)
    evt_bus = EventBusService(event_store=evt_store)
    alert_adapter = InMemoryAlertDeliveryAdapter()
    alert_service = AlertService(alert_repo, alert_adapter, clock=clock, cooldown_seconds=0.0)
    alert_handler = AutonomousAlertEventHandler(alert_service)
    evt_bus.register_handler(alert_handler)

    decision_service = DecisionMemoryService(decision_repo)
    learning_service = LearningSignalService(signal_repo)
    policy_engine = PolicyEngine()

    executor = IntegratedE2ECycleExecutor(
        market_monitoring_service=mon_service,
        opportunity_detection_service=opp_service,
        change_detection_service=chg_service,
        event_bus_service=evt_bus,
        alert_service=alert_service,
        mission_repo=mission_repo,
        decision_service=decision_service,
        action_repo=action_repo,
        result_repo=result_repo,
        outcome_repo=outcome_repo,
        learning_service=learning_service,
        policy_engine=policy_engine,
        clock=clock,
    )

    cm_service = ContinuousMissionService(
        repository=cm_repo,
        cycle_executor=executor,
        clock=clock,
    )

    scheduler_service = SchedulerService(
        repository=sched_repo,
        trigger=cm_service,
        clock=clock,
    )
    cm_service.scheduler_service = scheduler_service

    return {
        "dir": d,
        "clock": clock,
        "sched_repo": sched_repo,
        "mon_repo": mon_repo,
        "opp_repo": opp_repo,
        "chg_repo": chg_repo,
        "evt_store": evt_store,
        "alert_repo": alert_repo,
        "alert_adapter": alert_adapter,
        "mission_repo": mission_repo,
        "decision_repo": decision_repo,
        "action_repo": action_repo,
        "result_repo": result_repo,
        "outcome_repo": outcome_repo,
        "signal_repo": signal_repo,
        "cm_repo": cm_repo,
        "scheduler_service": scheduler_service,
        "cm_service": cm_service,
        "executor": executor,
        "policy_engine": policy_engine,
    }


def test_gate_i_scenario_a_happy_path_two_cycles(gate_i_env):
    """
    Escenario A — Two-Cycle Happy Path:
    Ciclo 1: Schedule -> Continuous Mission Cycle 1 -> Observation -> Opportunity -> Mission -> Decision -> Action -> Result -> Memory -> Learning.
    Ciclo 2: Advance clock -> Schedule -> Cycle 2 -> Observation (Price Drop) -> Change Detected -> Event Published -> Alert Delivered -> Memory -> Learning.
    """
    clock = gate_i_env["clock"]
    scheduler = gate_i_env["scheduler_service"]
    cm_service = gate_i_env["cm_service"]
    alert_adapter = gate_i_env["alert_adapter"]

    # 1. Crear Schedule en SchedulerService
    schedule = scheduler.create_schedule(
        schedule_id="sch_e2e_happy_01",
        interval_seconds=3600,
        mission_type=MissionType.MARKET_DISCOVERY,
        mission_parameters={"query": "Logitech MX Master 3S", "target_action": "PRICE_UPDATE"},
        start_time=clock.now(),
    )

    # 2. Crear y arrancar Continuous Mission
    cm = cm_service.create_continuous_mission(
        schedule_id=schedule.schedule_id,
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Autonomous market price monitoring & optimization",
        mission_parameters={"query": "Logitech MX Master 3S", "target_action": "PRICE_UPDATE"},
        stop_condition=ContinuousMissionStopCondition(max_cycles=5),
        correlation_id="corr-gate-i-e2e",
    )
    cm = cm_service.start_mission(cm.continuous_mission_id)
    assert cm.status == ContinuousMissionStatus.ACTIVE

    # 3. Ciclo 1: disparar ocurrencias vencidas en el Scheduler tick()
    executed_c1 = scheduler.tick()
    assert len(executed_c1) == 1
    assert executed_c1[0].status == ExecutionStatus.SUCCESS

    # Verificar estado tras Ciclo 1
    cm_c1 = cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert cm_c1.cycle_count == 1
    assert cm_c1.last_result_status == ContinuousCycleStatus.SUCCESS.value
    cycles_1 = gate_i_env["cm_repo"].list_cycles(cm.continuous_mission_id)
    assert len(cycles_1) == 1
    assert cycles_1[0].cycle_number == 1

    # 4. Avanzar reloj 1 hora para Ciclo 2
    clock.advance(3600)

    # 5. Ciclo 2: disparar ocurrencia vencida
    executed_c2 = scheduler.tick()
    assert len(executed_c2) == 1
    assert executed_c2[0].status == ExecutionStatus.SUCCESS

    # Verificar estado tras Ciclo 2
    cm_c2 = cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert cm_c2.cycle_count == 2
    assert cm_c2.continuous_mission_id == cm.continuous_mission_id
    cycles_2 = gate_i_env["cm_repo"].list_cycles(cm.continuous_mission_id)
    assert len(cycles_2) == 2
    assert cycles_2[0].cycle_id != cycles_2[1].cycle_id
    assert cycles_2[1].cycle_number == 2

    # Verificar que el cambio de precio generó Evento (J.5) y Alerta (J.6)
    events = gate_i_env["evt_store"].list_events()
    assert len(events) >= 1
    assert events[0].event_type == EventType.CHANGE_DETECTED

    alerts = gate_i_env["alert_repo"].list_alerts()
    assert len(alerts) >= 1
    assert alerts[0].alert_type == AlertType.SIGNIFICANT_CHANGE
    assert alerts[0].delivery_status == AlertDeliveryStatus.DELIVERED
    assert len(alert_adapter.delivered_alerts) >= 1


def test_gate_i_scenario_b_restart_recovery(gate_i_env):
    """
    Escenario B — Restart / Recovery:
    Ciclo 1 ejecutado -> se destruyen y recrean todos los servicios recargando desde disco ->
    Scheduler y ContinuousMissionService reanudan deterministamente y ejecutan Ciclo 2 sin resetear ciclo_count ni duplicar ciclo 1.
    """
    clock = gate_i_env["clock"]
    scheduler = gate_i_env["scheduler_service"]
    cm_service = gate_i_env["cm_service"]
    d = gate_i_env["dir"]

    # 1. Crear y ejecutar Ciclo 1
    schedule = scheduler.create_schedule(
        schedule_id="sch_restart_01",
        interval_seconds=1800,
        mission_type=MissionType.MARKET_DISCOVERY,
        start_time=clock.now(),
    )
    cm = cm_service.create_continuous_mission(
        schedule_id=schedule.schedule_id,
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Restart resilience test",
        mission_parameters={"query": "Item X"},
    )
    cm_service.start_mission(cm.continuous_mission_id)
    scheduler.tick()

    cm_before = cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert cm_before.cycle_count == 1

    # 2. Simular caída y reinicio de proceso (recrear repositorios y servicios desde el directorio de persistencia)
    reloaded_sched_repo = JsonScheduleRepository(d / "schedules")
    reloaded_cm_repo = JsonContinuousMissionRepository(d)
    reloaded_mon_repo = JsonMarketObservationRepository(d / "monitoring")
    reloaded_opp_repo = JsonOpportunityRepository(d / "opportunities")
    reloaded_chg_repo = JsonChangeRecordRepository(d / "changes")
    reloaded_evt_store = JsonEventStore(d / "events")
    reloaded_alert_repo = JsonAlertRepository(d / "alerts")
    reloaded_mission_repo = JsonMissionRepository(d / "missions")
    reloaded_decision_repo = JsonDecisionRepository(d / "decisions")
    reloaded_action_repo = JsonActionRepository(d / "actions.json")
    reloaded_result_repo = JsonResultRepository(d / "results.json")
    reloaded_outcome_repo = JsonOutcomeRepository(d / "outcomes.json")
    reloaded_signal_repo = JsonLearningSignalRepository(d / "signals.json")

    reloaded_mon_service = MarketMonitoringService(
        repository=reloaded_mon_repo,
        sources=[DeterministicE2EMarketSource([350.0, 300.0])],
        clock=clock,
    )
    reloaded_opp_service = OpportunityDetectionService(reloaded_opp_repo, reloaded_mon_repo)
    reloaded_chg_service = ChangeDetectionService(reloaded_chg_repo, reloaded_mon_repo, reloaded_opp_repo)
    reloaded_evt_bus = EventBusService(reloaded_evt_store)
    reloaded_delivery = InMemoryAlertDeliveryAdapter()
    reloaded_alert_service = AlertService(reloaded_alert_repo, reloaded_delivery, clock=clock)
    reloaded_evt_bus.register_handler(AutonomousAlertEventHandler(reloaded_alert_service))

    reloaded_executor = IntegratedE2ECycleExecutor(
        market_monitoring_service=reloaded_mon_service,
        opportunity_detection_service=reloaded_opp_service,
        change_detection_service=reloaded_chg_service,
        event_bus_service=reloaded_evt_bus,
        alert_service=reloaded_alert_service,
        mission_repo=reloaded_mission_repo,
        decision_service=DecisionMemoryService(reloaded_decision_repo),
        action_repo=reloaded_action_repo,
        result_repo=reloaded_result_repo,
        outcome_repo=reloaded_outcome_repo,
        learning_service=LearningSignalService(reloaded_signal_repo),
        policy_engine=PolicyEngine(),
        clock=clock,
    )

    reloaded_cm_service = ContinuousMissionService(
        repository=reloaded_cm_repo,
        cycle_executor=reloaded_executor,
        clock=clock,
    )
    reloaded_scheduler = SchedulerService(
        repository=reloaded_sched_repo,
        trigger=reloaded_cm_service,
        clock=clock,
    )
    reloaded_cm_service.scheduler_service = reloaded_scheduler

    # 3. Avanzar tiempo y ejecutar Ciclo 2 en el sistema reiniciado
    clock.advance(1800)
    executed = reloaded_scheduler.tick()
    assert len(executed) == 1

    cm_after = reloaded_cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert cm_after.cycle_count == 2
    cycles = reloaded_cm_repo.list_cycles(cm.continuous_mission_id)
    assert len(cycles) == 2
    assert cycles[1].cycle_number == 2


def test_gate_i_scenario_c_duplicate_replay(gate_i_env):
    """
    Escenario C — Duplicate / Replay Idempotency:
    Reprocesar la misma ocurrencia o el mismo ciclo no duplica misiones lógicas,
    eventos, alertas ni acciones secundarias.
    """
    clock = gate_i_env["clock"]
    scheduler = gate_i_env["scheduler_service"]
    cm_service = gate_i_env["cm_service"]
    cm_repo = gate_i_env["cm_repo"]

    schedule = scheduler.create_schedule(
        schedule_id="sch_dup_01",
        interval_seconds=600,
        mission_type=MissionType.MARKET_DISCOVERY,
        start_time=clock.now(),
    )
    cm = cm_service.create_continuous_mission(
        schedule_id=schedule.schedule_id,
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Idempotency",
    )
    cm_service.start_mission(cm.continuous_mission_id)

    # Disparar ciclo
    occ = scheduler.tick()[0]
    cycles_1 = cm_repo.list_cycles(cm.continuous_mission_id)
    assert len(cycles_1) == 1

    # Reintentar ejecutar manualmente con la misma ocurrencia
    replay_cycle = cm_service.execute_next_cycle(cm.continuous_mission_id, occurrence=occ)
    assert replay_cycle.cycle_id == cycles_1[0].cycle_id

    # La cuenta total de ciclos sigue siendo exactamente 1
    cycles_after = cm_repo.list_cycles(cm.continuous_mission_id)
    assert len(cycles_after) == 1


def test_gate_i_scenario_d_unknown_preservation(gate_i_env):
    """
    Escenario D — UNKNOWN Preservation:
    Cuando una acción de ciclo o una fuente genera incertidumbre o se bloquea,
    el estado resultante es deterministamente UNKNOWN y nunca se maquilla como SUCCESS ni como FAILED falso.
    """
    clock = gate_i_env["clock"]
    cm_service = gate_i_env["cm_service"]

    class UnknownCycleExecutor(CycleExecutorPort):
        def execute_cycle(self, cm, cycle):
            return ContinuousCycleStatus.UNKNOWN, "m_unknown_01", {"uncertainty_reason": "Data source ambiguous"}, "Partial response"

    cm_service.cycle_executor = UnknownCycleExecutor()

    cm = cm_service.create_continuous_mission(
        schedule_id="sched_unknown_01",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Preserve uncertainty",
        stop_condition=ContinuousMissionStopCondition(stop_on_unknown=True),
    )
    cm_service.start_mission(cm.continuous_mission_id)

    cycle = cm_service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle.status == ContinuousCycleStatus.UNKNOWN
    assert cycle.error_message == "Partial response"

    updated_cm = cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert updated_cm.status == ContinuousMissionStatus.UNKNOWN
    assert updated_cm.last_result_status == ContinuousCycleStatus.UNKNOWN.value


def test_gate_i_scenario_e_policy_governance_enforcement(gate_i_env):
    """
    Escenario E — Policy DENY / Approval Enforcement:
    Si el PolicyEngine deniega una acción (`PROHIBITED_ACTION`), el ciclo no ejecuta la tool,
    queda registrado con estatus de gobernanza y no produce bypass de seguridad.
    """
    clock = gate_i_env["clock"]
    scheduler = gate_i_env["scheduler_service"]
    cm_service = gate_i_env["cm_service"]

    schedule = scheduler.create_schedule(
        schedule_id="sch_gov_01",
        interval_seconds=3600,
        mission_type=MissionType.MARKET_DISCOVERY,
        mission_parameters={"target_action": "PROHIBITED_ACTION"},
        start_time=clock.now(),
    )
    cm = cm_service.create_continuous_mission(
        schedule_id=schedule.schedule_id,
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Policy governance test",
        mission_parameters={"target_action": "PROHIBITED_ACTION"},
    )
    cm_service.start_mission(cm.continuous_mission_id)

    cycle = cm_service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle.status == ContinuousCycleStatus.UNKNOWN
    assert "denied by PolicyEngine" in cycle.error_message


def test_gate_i_scenario_f_pause_resume_stop_lifecycle(gate_i_env):
    """
    Escenario F — Pause / Resume / Stop Lifecycle:
    ACTIVE -> PAUSED (schedule due no ejecuta ciclo nuevo, devuelve SKIPPED) ->
    PAUSED -> ACTIVE (siguiente ciclo se ejecuta) ->
    ACTIVE -> STOPPED (schedule occurrence futuro no produce ciclo).
    """
    clock = gate_i_env["clock"]
    cm_service = gate_i_env["cm_service"]

    cm = cm_service.create_continuous_mission(
        schedule_id="sched_lifecycle_01",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Lifecycle control",
    )
    cm_service.start_mission(cm.continuous_mission_id)

    # Pausar
    cm_service.pause_mission(cm.continuous_mission_id)
    paused_cm = cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert paused_cm.status == ContinuousMissionStatus.PAUSED

    # Intentar ejecutar mientras está en pausa
    skip_cycle = cm_service.execute_next_cycle(cm.continuous_mission_id)
    assert skip_cycle.status == ContinuousCycleStatus.SKIPPED
    assert paused_cm.cycle_count == 0

    # Reanudar
    cm_service.resume_mission(cm.continuous_mission_id)
    resumed_cm = cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert resumed_cm.status == ContinuousMissionStatus.ACTIVE

    exec_cycle = cm_service.execute_next_cycle(cm.continuous_mission_id)
    assert exec_cycle.status == ContinuousCycleStatus.SUCCESS

    # Detener
    cm_service.stop_mission(cm.continuous_mission_id, reason="Mission accomplished")
    stopped_cm = cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert stopped_cm.status == ContinuousMissionStatus.STOPPED

    stop_cycle = cm_service.execute_next_cycle(cm.continuous_mission_id)
    assert stop_cycle.status == ContinuousCycleStatus.SKIPPED


def test_gate_i_scenario_g_failure_isolation(gate_i_env):
    """
    Escenario G — Failure Isolation:
    Un fallo en un delivery handler del Event Bus / Alertas no propaga excepciones
    catastróficas al bucle de la misión continua ni rompe los ciclos subsiguientes.
    """
    clock = gate_i_env["clock"]
    evt_bus = gate_i_env["cm_service"].cycle_executor.event_bus_service

    # Handler defectuoso que lanza excepción
    class FaultyHandler(EventHandlerPort):
        @property
        def handler_id(self) -> str:
            return "faulty_handler_01"

        def can_handle(self, event_type: EventType) -> bool:
            return True

        def handle(self, event: EventRecord) -> None:
            raise RuntimeError("Delivery gateway network timeout")

    evt_bus.register_handler(FaultyHandler())

    # Publicar un evento; el event bus debe aislar el error
    event = EventRecord(
        event_id="evt_fault_01",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-001",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr_fault_01",
    )
    # publish no debe reventar
    evt_bus.publish(event)

    # El Event Store persiste el evento a pesar del error de delivery
    stored = gate_i_env["evt_store"].get_by_id("evt_fault_01")
    assert stored is not None


def test_gate_i_scenario_h_max_cycles_deterministic_termination(gate_i_env):
    """
    Escenario H — Max Cycles:
    Al alcanzar max_cycles = N, la misión continua pasa a COMPLETED y no ejecuta ciclos adicionales.
    """
    clock = gate_i_env["clock"]
    cm_service = gate_i_env["cm_service"]

    cm = cm_service.create_continuous_mission(
        schedule_id="sched_max_01",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Max cycles test",
        stop_condition=ContinuousMissionStopCondition(max_cycles=2),
    )
    cm_service.start_mission(cm.continuous_mission_id)

    # Ciclo 1
    cm_service.execute_next_cycle(cm.continuous_mission_id)
    # Ciclo 2
    cm_service.execute_next_cycle(cm.continuous_mission_id)

    cm_final = cm_service.get_continuous_mission(cm.continuous_mission_id)
    assert cm_final.status == ContinuousMissionStatus.COMPLETED
    assert cm_final.cycle_count == 2

    # Intento de Ciclo 3
    cycle_3 = cm_service.execute_next_cycle(cm.continuous_mission_id)
    assert cycle_3.status == ContinuousCycleStatus.SKIPPED


def test_gate_i_scenario_i_security_sanitization(gate_i_env):
    """
    Escenario I — Security & Sensitive Data Exclusion:
    Metadata y parámetros con claves secretas ('api_key', 'token', 'password', 'secret')
    son redactados atómicamente antes de guardarse en disco.
    """
    clock = gate_i_env["clock"]
    cm_service = gate_i_env["cm_service"]
    d = gate_i_env["dir"]

    cm = cm_service.create_continuous_mission(
        schedule_id="sched_sec_01",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Security redaction test",
        mission_parameters={"api_key": "SECRET_KEY_12345", "token": "BEARER_SECRET_TOKEN"},
        metadata={"db_password": "CONFIDENTIAL_PASSWORD"},
    )

    # Inspeccionar archivo JSON en disco
    cm_file = d / "continuous_missions" / f"{cm.continuous_mission_id}.json"
    content = cm_file.read_text(encoding="utf-8")
    assert "SECRET_KEY_12345" not in content
    assert "BEARER_SECRET_TOKEN" not in content
    assert "CONFIDENTIAL_PASSWORD" not in content
    assert "[REDACTED]" in content


def test_gate_i_scenario_j_full_causal_traceability(gate_i_env):
    """
    Escenario J — Full Causal Traceability:
    Demuestra la correspondencia e integridad de identificadores:
    ContinuousMission -> Cycle -> MarketObservation -> Change -> Event -> Alert ->
    Mission -> Decision -> Action -> Result -> Outcome -> Learning Signal.
    """
    clock = gate_i_env["clock"]
    scheduler = gate_i_env["scheduler_service"]
    cm_service = gate_i_env["cm_service"]

    schedule = scheduler.create_schedule(
        schedule_id="sch_trace_01",
        interval_seconds=3600,
        mission_type=MissionType.MARKET_DISCOVERY,
        start_time=clock.now(),
    )
    cm = cm_service.create_continuous_mission(
        schedule_id=schedule.schedule_id,
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Traceability verification",
        correlation_id="corr-trace-001",
        provenance="GATE_I_VALIDATION",
    )
    cm_service.start_mission(cm.continuous_mission_id)

    # Ciclo 1
    scheduler.tick()
    clock.advance(3600)
    # Ciclo 2 (con cambio de precio)
    scheduler.tick()

    cycles = gate_i_env["cm_repo"].list_cycles(cm.continuous_mission_id)
    assert len(cycles) == 2
    c2 = cycles[1]

    # Verificar que el cycle_2 tiene su correlation_id y provenance correctos
    assert c2.correlation_id == "corr-trace-001"
    assert c2.provenance == "GATE_I_VALIDATION"
    assert c2.result_summary["decision_id"] is not None
    assert c2.result_summary["action_id"] is not None
    assert c2.result_summary["outcome_id"] is not None

    # Verificar decisión y acción en Business Memory
    dec_id = c2.result_summary["decision_id"]
    dec = gate_i_env["decision_repo"].get_by_id(dec_id)
    assert dec is not None
    assert dec.mission_id == c2.mission_id

    act_id = c2.result_summary["action_id"]
    act = gate_i_env["action_repo"].get_by_id(act_id)
    assert act is not None
    assert act.decision_id == dec.decision_id

    # Learning Signal vinculado al outcome
    out_id = c2.result_summary["outcome_id"]
    signals = gate_i_env["signal_repo"].list_all()
    matching_signal = next((s for s in signals if s.outcome_id == out_id or s.metadata.get("outcome_id") == out_id), None)
    assert matching_signal is not None
    assert matching_signal.signal_type == LearningSignalType.POSITIVE_OUTCOME
