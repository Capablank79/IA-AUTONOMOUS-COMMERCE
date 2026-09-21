"""
Pruebas de Integración y Validación E2E para R.7 — Self-monitoring (Hito R — Advanced Autonomy).

Escenarios cubiertos y pruebas exhaustivas de integración arquitectónica:
A. healthy mission -> no intervention (con required signals completas y auditoría K.1/K.2).
B. stale heartbeat -> degraded/at-risk -> PAUSE.
C. technical repeated failures -> request R.1 replan.
D. agent unavailable -> request R.5 delegation.
E. policy denied -> BLOCK, no replan/delegation.
F. budget hard limit -> BLOCK.
G. recoverable degradation -> R.6 pause/checkpoint.
H. critical degradation -> P.8 escalation (ProductionAlertingService raise_or_escalate_alert dedupe & cooldown).
I. Tenant A/B isolation.
J. health recovery after signals normalize.
K. E2E Autonomous Workflow Integration (R.1 -> R.2 -> R.3 -> R.4 -> R.5 -> R.6 -> R.7).
L. Real Production Dispatcher integration with R.1 (Replan), R.5 (Delegation), R.6 (Pause), BLOCK invariant.
M. Real SelfMonitoringAuditAdapter integration with K.1 (AuditRepositoryPort), K.2 (AgentTraceService), P.8 (ProductionAlertingService).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple
import pytest

from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecordType, AuditRecord, MissionAuditTimeline
from src.domain.self_monitoring.models import (
    MissionHealthStatus,
    SignalType,
    SignalSource,
    SignalSeverity,
    SignalCompleteness,
    DegradationReasonCode,
    SelfMonitoringAction,
    HealthSignal,
    SelfMonitoringPolicy,
    MissionHealthSnapshot,
    HealthAssessment,
    SelfMonitoringDecision,
)
from src.domain.self_monitoring.ports import (
    SelfMonitoringAuditPort,
    SignalCollectorPort,
)
from src.application.self_monitoring.self_monitoring_service import (
    SelfMonitoringService,
    InMemorySelfMonitoringRepository,
)
from src.application.self_monitoring.self_monitoring_audit_adapter import SelfMonitoringAuditAdapter
from src.application.self_monitoring.self_monitoring_action_dispatcher import (
    SelfMonitoringActionDispatcher,
    DispatchStatus,
    DispatchResult,
)
from src.domain.production_alerting.models import (
    AlertRuleType,
    AlertSeverity,
    ProductionAlertScope,
    ApplicationEnvironment,
)
from src.infrastructure.persistence.data.json.production_alert_repository import (
    JsonProductionAlertRepository,
)
from src.application.production_alerting.production_alerting_service import (
    ProductionAlertingService,
)
from src.infrastructure.persistence.data.json.agent_trace_repository import (
    JsonAgentTraceRepository,
)
from src.application.agent_trace.agent_trace_service import (
    AgentTraceService,
)
from src.domain.planning.ports import ExecutionPlanRepositoryPort, MultiStepPlanningServicePort
from src.domain.planning.models import ExecutionPlan, PlanStatus, PlanStep, StepStatus, StepFailureType, PlanningResult
from src.domain.agent_coordination.ports import CoordinationSessionRepositoryPort, AgentCoordinatorPort
from src.domain.agent_coordination.models import CoordinationSession, CoordinationTask, CoordinationTaskStatus
from src.domain.agent_coordination.delegation_models import DelegationRequest, DelegationDecision, DelegationRecord, DelegationReason, DelegationDecisionStatus
from src.domain.long_running_mission.ports import LongRunningMissionServicePort, LeaseManagerPort
from src.domain.long_running_mission.models import MissionCheckpoint, CheckpointStatus, LeaseState


class MockAuditRepository(AuditRepositoryPort):
    def __init__(self):
        self.records: List[AuditRecord] = []

    def append(self, record: AuditRecord) -> AuditRecord:
        self.records.append(record)
        return record

    def get_by_id(self, audit_id: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.audit_id == audit_id:
                return r
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.idempotency_key == idempotency_key:
                return r
        return None

    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        res = list(self.records)
        if mission_id:
            res = [r for r in res if r.mission_id == mission_id]
        if correlation_id:
            res = [r for r in res if r.correlation_id == correlation_id]
        if record_type:
            res = [r for r in res if r.record_type == record_type]
        return res[:limit]

    def reconstruct_mission_timeline(self, mission_id: str) -> MissionAuditTimeline:
        matching = [r for r in self.records if r.mission_id == mission_id]
        corr_id = matching[0].correlation_id if matching else ""
        return MissionAuditTimeline(mission_id=mission_id, correlation_id=corr_id, records=tuple(matching))

    def count(self, context: TenantContext) -> int:
        return len([r for r in self.records if r.actor.details.get("tenant_id") == context.tenant_id])


class SpySelfMonitoringAuditAdapter(SelfMonitoringAuditPort):
    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def record_assessment_audited(self, assessment: HealthAssessment, context: TenantContext) -> None:
        self.events.append({
            "type": AuditRecordType.SELF_MONITORING_ASSESSED,
            "mission_id": assessment.mission_id,
            "tenant_id": context.tenant_id,
            "status": assessment.status,
        })

    def record_action_requested(self, assessment: HealthAssessment, decision: SelfMonitoringDecision, context: TenantContext) -> None:
        self.events.append({
            "type": AuditRecordType.SELF_MONITORING_ACTION_REQUESTED,
            "mission_id": assessment.mission_id,
            "tenant_id": context.tenant_id,
            "action": decision.action,
        })

    def record_escalation(self, assessment: HealthAssessment, decision: SelfMonitoringDecision, context: TenantContext) -> None:
        self.events.append({
            "type": AuditRecordType.SELF_MONITORING_ESCALATED,
            "mission_id": assessment.mission_id,
            "tenant_id": context.tenant_id,
            "severity": decision.escalation_severity,
        })

    def record_recovery(self, assessment: HealthAssessment, previous_status: MissionHealthStatus, context: TenantContext) -> None:
        self.events.append({
            "type": AuditRecordType.MISSION_HEALTH_RECOVERED,
            "mission_id": assessment.mission_id,
            "tenant_id": context.tenant_id,
            "previous_status": previous_status,
        })


@pytest.fixture
def clock():
    return VirtualClock(datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def repo():
    return InMemorySelfMonitoringRepository()


@pytest.fixture
def audit_spy():
    return SpySelfMonitoringAuditAdapter()


@pytest.fixture
def context_a():
    return TenantContext(tenant_id="tenant_a", identity_id="user_a")


@pytest.fixture
def context_b():
    return TenantContext(tenant_id="tenant_b", identity_id="user_b")


@pytest.fixture
def policy():
    return SelfMonitoringPolicy(
        stale_heartbeat_threshold_seconds=30.0,
        stall_timeout_seconds=120.0,
        max_remediation_actions_per_mission=3,
        max_replan_requests=2,
        max_delegation_requests=2,
        failure_rate_threshold=0.5,
        min_failure_samples=3,
        required_signal_types=(
            SignalType.HEARTBEAT_LIVENESS,
            SignalType.EXECUTION_PROGRESS,
            SignalType.POLICY_COMPLIANCE,
        ),
    )


@pytest.fixture
def service(repo, clock, audit_spy, policy):
    return SelfMonitoringService(
        repository=repo,
        clock=clock,
        policy=policy,
        audit_adapter=audit_spy,
    )


def test_scenario_a_healthy_mission_no_intervention(service, context_a, clock, audit_spy):
    now = clock.now()
    signals = [
        HealthSignal("s1", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, now, 1.0, {"last_seen_at": now}, tenant_id="tenant_a", mission_id="m-healthy", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("s2", SignalType.EXECUTION_PROGRESS, SignalSource.AUTONOMOUS_LOOP, SignalSeverity.INFO, now, 1.0, 50.0, tenant_id="tenant_a", mission_id="m-healthy", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("s3", SignalType.POLICY_COMPLIANCE, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, now, 1.0, True, tenant_id="tenant_a", mission_id="m-healthy", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = service.assess_mission_health("m-healthy", context_a, signals)
    assert assessment.status == MissionHealthStatus.HEALTHY
    assert assessment.decision.action == SelfMonitoringAction.NONE
    assert any(e["type"] == AuditRecordType.SELF_MONITORING_ASSESSED for e in audit_spy.events)
    assert not any(e["type"] == AuditRecordType.SELF_MONITORING_ACTION_REQUESTED for e in audit_spy.events)


def test_scenario_b_stale_heartbeat_at_risk(service, context_a, clock, audit_spy):
    now = clock.now()
    stale_time = now - timedelta(seconds=45)
    signals = [
        HealthSignal("s1", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.HIGH, now, 1.0, {"last_seen_at": stale_time}, tenant_id="tenant_a", mission_id="m-stale", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = service.assess_mission_health("m-stale", context_a, signals)
    assert assessment.status == MissionHealthStatus.AT_RISK
    assert assessment.decision.action == SelfMonitoringAction.PAUSE
    assert any(e["type"] == AuditRecordType.SELF_MONITORING_ACTION_REQUESTED for e in audit_spy.events)


def test_scenario_c_technical_failures_request_replan(service, context_a, clock, audit_spy):
    now = clock.now()
    signals = [
        HealthSignal("s1", SignalType.TECHNICAL_FAILURE_RATE, SignalSource.R1_PLANNING, SignalSeverity.HIGH, now, 1.0, {"failures": 3, "total": 4}, tenant_id="tenant_a", mission_id="m-replan", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = service.assess_mission_health("m-replan", context_a, signals)
    assert assessment.decision.action == SelfMonitoringAction.REQUEST_REPLAN
    assert assessment.decision.suggested_target == "dag_replan"


def test_scenario_d_agent_unavailable_request_delegation(service, context_a, clock, audit_spy):
    now = clock.now()
    signals = [
        HealthSignal("s1", SignalType.AGENT_AVAILABILITY, SignalSource.R3_SPECIALIST_REGISTRY, SignalSeverity.HIGH, now, 1.0, "UNAVAILABLE", tenant_id="tenant_a", mission_id="m-deleg", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = service.assess_mission_health("m-deleg", context_a, signals)
    assert assessment.decision.action == SelfMonitoringAction.REQUEST_DELEGATION
    assert assessment.decision.suggested_target == "specialist_delegation"


def test_scenario_e_policy_denied_block_no_replan(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal("s1", SignalType.POLICY_COMPLIANCE, SignalSource.N_GOVERNANCE, SignalSeverity.CRITICAL, now, 1.0, "POLICY_DENIED", tenant_id="tenant_a", mission_id="m-pol-block", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("s2", SignalType.TECHNICAL_FAILURE_RATE, SignalSource.R1_PLANNING, SignalSeverity.HIGH, now, 1.0, {"failures": 3, "total": 4}, tenant_id="tenant_a", mission_id="m-pol-block", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = service.assess_mission_health("m-pol-block", context_a, signals)
    assert assessment.status == MissionHealthStatus.BLOCKED
    assert assessment.decision.action == SelfMonitoringAction.BLOCK
    assert assessment.decision.action != SelfMonitoringAction.REQUEST_REPLAN


def test_scenario_f_budget_hard_limit_block(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal("s1", SignalType.BUDGET_PRESSURE, SignalSource.K3_COST_TRACKING, SignalSeverity.CRITICAL, now, 1.0, {"consumed": Decimal("105"), "allocated": Decimal("100")}, tenant_id="tenant_a", mission_id="m-bud-block", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = service.assess_mission_health("m-bud-block", context_a, signals)
    assert assessment.status == MissionHealthStatus.BLOCKED
    assert assessment.decision.action == SelfMonitoringAction.BLOCK


def test_scenario_g_recoverable_degradation_pause_checkpoint(service, context_a, clock):
    now = clock.now()
    signals = [
        HealthSignal("s1", SignalType.STALL_TEMPORAL, SignalSource.AUTONOMOUS_LOOP, SignalSeverity.HIGH, now, 1.0, {"last_progress_at": now - timedelta(seconds=200), "active_nonterminal_work": True}, tenant_id="tenant_a", mission_id="m-pause-chk", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = service.assess_mission_health("m-pause-chk", context_a, signals)
    assert assessment.decision.action == SelfMonitoringAction.PAUSE


def test_scenario_h_critical_degradation_escalation(service, context_a, clock, audit_spy):
    now = clock.now()
    signals = [
        HealthSignal("s1", SignalType.QUOTA_SATURATION, SignalSource.O7_QUOTA, SignalSeverity.CRITICAL, now, 1.0, "EXHAUSTED", tenant_id="tenant_a", mission_id="m-crit-esc", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = service.assess_mission_health("m-crit-esc", context_a, signals)
    assert assessment.status == MissionHealthStatus.BLOCKED
    assert assessment.decision.requires_escalation is True
    assert any(e["type"] == AuditRecordType.SELF_MONITORING_ESCALATED for e in audit_spy.events)


def test_scenario_i_tenant_isolation(service, repo, context_a, context_b, clock):
    now = clock.now()
    signals = [
        HealthSignal("s1", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, now, 1.0, {"last_seen_at": now}, tenant_id="tenant_a", mission_id="m-isolated", completeness=SignalCompleteness.COMPLETE),
    ]
    service.assess_mission_health("m-isolated", context_a, signals)

    # Tenant B query returns None
    assert repo.get_latest_assessment("m-isolated", context_b) is None
    assert repo.get_latest_snapshot("m-isolated", context_b) is None


def test_scenario_j_health_recovery_after_normalization(service, context_a, clock, audit_spy):
    now = clock.now()
    # 1. Degradation
    signals_bad = [
        HealthSignal("s1", SignalType.RATE_LIMIT_PRESSURE, SignalSource.P11_RATE_LIMIT, SignalSeverity.HIGH, now, 1.0, "SATURATED", tenant_id="tenant_a", mission_id="m-recovery", completeness=SignalCompleteness.COMPLETE),
    ]
    service.assess_mission_health("m-recovery", context_a, signals_bad)

    # 2. Recovery
    clock.advance(60)
    now2 = clock.now()
    signals_good = [
        HealthSignal("s1", SignalType.RATE_LIMIT_PRESSURE, SignalSource.P11_RATE_LIMIT, SignalSeverity.INFO, now2, 1.0, "NORMAL", tenant_id="tenant_a", mission_id="m-recovery", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("s2", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, now2, 1.0, {"last_seen_at": now2}, tenant_id="tenant_a", mission_id="m-recovery", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("s3", SignalType.EXECUTION_PROGRESS, SignalSource.AUTONOMOUS_LOOP, SignalSeverity.INFO, now2, 1.0, 60.0, tenant_id="tenant_a", mission_id="m-recovery", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("s4", SignalType.POLICY_COMPLIANCE, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, now2, 1.0, True, tenant_id="tenant_a", mission_id="m-recovery", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment2 = service.assess_mission_health("m-recovery", context_a, signals_good)
    assert assessment2.status == MissionHealthStatus.HEALTHY
    assert any(e["type"] == AuditRecordType.MISSION_HEALTH_RECOVERED for e in audit_spy.events)


def test_scenario_k_e2e_autonomous_workflow_integration(service, context_a, clock, audit_spy):
    now = clock.now()

    # Paso 1: Misión activa con ejecución normal y señales requeridas completas
    s1 = [
        HealthSignal("sig-hb-1", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, now, 1.0, {"last_seen_at": now}, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-plan-1", SignalType.EXECUTION_PROGRESS, SignalSource.R1_PLANNING, SignalSeverity.INFO, now, 1.0, 20.0, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-pol-1", SignalType.POLICY_COMPLIANCE, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, now, 1.0, True, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
    ]
    a1 = service.assess_mission_health("m-e2e-1", context_a, s1)
    assert a1.status == MissionHealthStatus.HEALTHY
    assert a1.decision.action == SelfMonitoringAction.NONE

    # Paso 2: Especialista se degrada -> R.7 solicita Dynamic Delegation (R.5)
    clock.advance(30)
    now = clock.now()
    s2 = [
        HealthSignal("sig-hb-2", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, now, 1.0, {"last_seen_at": now}, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-agent-2", SignalType.AGENT_AVAILABILITY, SignalSource.R3_SPECIALIST_REGISTRY, SignalSeverity.HIGH, now, 1.0, "UNAVAILABLE", tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
    ]
    a2 = service.assess_mission_health("m-e2e-1", context_a, s2)
    assert a2.status == MissionHealthStatus.AT_RISK
    assert a2.decision.action == SelfMonitoringAction.REQUEST_DELEGATION
    assert a2.remediation_counter == 1

    # Paso 3: Fallos técnicos repetidos en DAG -> R.7 solicita Replan (R.1)
    clock.advance(30)
    now = clock.now()
    s3 = [
        HealthSignal("sig-hb-3", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, now, 1.0, {"last_seen_at": now}, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-fail-3", SignalType.TECHNICAL_FAILURE_RATE, SignalSource.R1_PLANNING, SignalSeverity.HIGH, now, 1.0, {"failures": 4, "total": 5}, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
    ]
    a3 = service.assess_mission_health("m-e2e-1", context_a, s3)
    assert a3.status == MissionHealthStatus.AT_RISK
    assert a3.decision.action == SelfMonitoringAction.REQUEST_REPLAN
    assert a3.remediation_counter == 2

    # Paso 4: Replan aplicado y ejecución normalizada -> Salud recuperada
    clock.advance(60)
    now = clock.now()
    s4 = [
        HealthSignal("sig-hb-4", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, now, 1.0, {"last_seen_at": now}, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-plan-4", SignalType.EXECUTION_PROGRESS, SignalSource.R1_PLANNING, SignalSeverity.INFO, now, 1.0, 100.0, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-pol-4", SignalType.POLICY_COMPLIANCE, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, now, 1.0, True, tenant_id="tenant_a", mission_id="m-e2e-1", completeness=SignalCompleteness.COMPLETE),
    ]
    a4 = service.assess_mission_health("m-e2e-1", context_a, s4)
    assert a4.status == MissionHealthStatus.HEALTHY
    assert a4.previous_status == MissionHealthStatus.AT_RISK
    assert any(e["type"] == AuditRecordType.MISSION_HEALTH_RECOVERED for e in audit_spy.events)


# =========================================================================
# Escenario L: Despacho real y verificable a R.1 / R.5 / R.6 y BLOCKS invariantes
# =========================================================================

class MockPlanningService(MultiStepPlanningServicePort):
    def __init__(self):
        self.replan_calls = []

    def create_plan(self, mission_id, goal, tenant_context, initial_steps, budget=None, metadata=None):
        pass

    def get_ready_steps(self, plan, tenant_context):
        return ()

    def replan(self, plan_id: str, tenant_context: TenantContext, failed_step_id: str, failure_type: StepFailureType, failure_reason: str, new_subgraph_steps=None) -> PlanningResult:
        self.replan_calls.append((plan_id, failed_step_id, failure_reason, tenant_context.tenant_id))
        plan = ExecutionPlan(
            plan_id="plan-1",
            mission_id="m-1",
            tenant_id=tenant_context.tenant_id,
            goal="Test Goal",
            steps=(),
            status=PlanStatus.READY,
        )
        return PlanningResult(success=True, plan=plan, status=PlanStatus.READY)


class MockCoordinatorService(AgentCoordinatorPort):
    def __init__(self):
        self.delegate_calls = []

    def create_session(self, tenant_context, mission_id, plan=None, sub_missions=(), policy=None, correlation_id=None):
        pass

    def get_ready_tasks(self, session_id, tenant_context):
        return ()

    def claim_task(self, session_id, task_id, agent_id, tenant_context):
        pass

    def handoff(self, session_id, source_agent_id, target_task_id, payload, tenant_context, evidence_refs=(), target_agent_id=None):
        pass

    def merge_task_results(self, session_id, source_task_ids, target_task_id, tenant_context, strategy=None, precedence_order=()):
        pass

    def get_session(self, session_id, tenant_context):
        return None

    def cancel_coordination(self, session_id, tenant_context, reason="Mission cancelled"):
        pass

    def delegate_task(
        self,
        session_id: str,
        request: DelegationRequest,
        tenant_context: TenantContext,
    ) -> Tuple[CoordinationSession, DelegationRecord]:
        self.delegate_calls.append((session_id, request.task_id, request.from_agent_id, tenant_context.tenant_id))
        session = CoordinationSession(
            session_id=session_id,
            tenant_id=tenant_context.tenant_id,
            mission_id=request.mission_id,
            correlation_id="corr-1",
        )
        decision = DelegationDecision(
            request=request,
            status=DelegationDecisionStatus.APPROVED,
            to_agent_id="agent-backup",
        )
        record = DelegationRecord(
            delegation_id="del-1",
            decision=decision,
            assignment_version=2,
        )
        return session, record


class MockMissionService(LongRunningMissionServicePort):
    def __init__(self):
        self.pause_calls = []

    def create_checkpoint(self, mission_id, worker_id, tenant_context, metadata=None):
        pass

    def pause_mission(
        self,
        mission_id: str,
        worker_id: str,
        tenant_context: TenantContext,
        reason: str = "User requested pause",
    ) -> MissionCheckpoint:
        from src.domain.mission.models import MissionStatus
        self.pause_calls.append((mission_id, worker_id, reason, tenant_context.tenant_id))
        return MissionCheckpoint(
            checkpoint_id="chk-1",
            mission_id=mission_id,
            tenant_id=tenant_context.tenant_id,
            mission_type="MARKET_DISCOVERY",
            mission_status=MissionStatus.RUNNING,
            checkpoint_version=1,
            created_at=datetime.now(timezone.utc),
            plan_id="plan-1",
            plan_version=1,
            current_worker_id=worker_id,
            status=CheckpointStatus.VALID,
        )

    def resume_mission(self, mission_id, worker_id, tenant_context):
        pass

    def record_heartbeat(self, mission_id, worker_id, tenant_context, metadata=None):
        pass


class MockPlanningRepo(ExecutionPlanRepositoryPort):
    def __init__(self, plan: ExecutionPlan):
        self.plan = plan

    def save_plan(self, plan: ExecutionPlan, tenant_context: TenantContext) -> ExecutionPlan:
        self.plan = plan
        return plan

    def get_plan_by_id(self, plan_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        return self.plan if self.plan and self.plan.plan_id == plan_id else None

    def get_plan_by_mission_id(self, mission_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        return self.plan if self.plan and self.plan.mission_id == mission_id else None

    def list_plans_for_mission(self, mission_id: str, tenant_context: TenantContext) -> List[ExecutionPlan]:
        return [self.plan] if self.plan and self.plan.mission_id == mission_id else []

    def list_plans_by_mission(self, mission_id: str, tenant_context: TenantContext):
        return [self.plan] if self.plan and self.plan.mission_id == mission_id else []

    def get_latest_plan_version(self, mission_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        return self.plan if self.plan and self.plan.mission_id == mission_id else None


class MockCoordinationRepo(CoordinationSessionRepositoryPort):
    def __init__(self, session: CoordinationSession):
        self.session = session

    def save_session(self, session: CoordinationSession, tenant_context: TenantContext) -> CoordinationSession:
        self.session = session
        return session

    def get_session(self, session_id: str, tenant_context: TenantContext) -> Optional[CoordinationSession]:
        return self.session if self.session and self.session.session_id == session_id else None

    def get_session_by_mission(self, mission_id: str, tenant_context: TenantContext) -> Optional[CoordinationSession]:
        return self.session if self.session and self.session.mission_id == mission_id else None

    def list_sessions_for_tenant(self, tenant_context: TenantContext) -> List[CoordinationSession]:
        return [self.session] if self.session else []


def test_scenario_l_real_dispatcher_replan_delegation_pause_and_block_invariants(context_a, clock):
    plan = ExecutionPlan(
        plan_id="plan-100",
        mission_id="m-1",
        tenant_id=context_a.tenant_id,
        goal="Test Goal",
        steps=(
            PlanStep(step_id="step_9", objective="Execute step 9", action_type="action", status=StepStatus.FAILED),
        ),
        status=PlanStatus.IN_PROGRESS,
    )
    coord_task = CoordinationTask(
        task_id="task-5",
        session_id="sess-200",
        tenant_id=context_a.tenant_id,
        required_capability="general",
        action_type="action",
        assigned_agent_id="agent-failing",
        status=CoordinationTaskStatus.RUNNING,
    )
    session = CoordinationSession(
        session_id="sess-200",
        tenant_id=context_a.tenant_id,
        mission_id="m-1",
        correlation_id="corr-1",
        tasks={"task-5": coord_task},
    )

    planning_repo = MockPlanningRepo(plan)
    planning_srv = MockPlanningService()
    coord_repo = MockCoordinationRepo(session)
    coord_srv = MockCoordinatorService()
    mission_srv = MockMissionService()

    dispatcher = SelfMonitoringActionDispatcher(
        planning_repo=planning_repo,
        planning_service=planning_srv,
        coordination_repo=coord_repo,
        coordinator_service=coord_srv,
        mission_service=mission_srv,
    )

    now = clock.now()

    # 1. Dispatch REPLAN
    dec_replan = SelfMonitoringDecision(
        action=SelfMonitoringAction.REQUEST_REPLAN,
        health_status=MissionHealthStatus.AT_RISK,
        reason_codes=(DegradationReasonCode.REPEATED_TECHNICAL_FAILURES,),
        rationale="Technical failure in DAG",
        evaluated_at=now,
        suggested_target="step_9",
        suggested_payload={"plan_id": "plan-100", "step_id": "step_9"},
    )
    snap = MissionHealthSnapshot(
        snapshot_id="snap-1",
        mission_id="m-1",
        tenant_id=context_a.tenant_id,
        health_status=MissionHealthStatus.AT_RISK,
        signals=(),
        reason_codes=(DegradationReasonCode.REPEATED_TECHNICAL_FAILURES,),
        captured_at=now,
    )
    ass_replan = HealthAssessment(
        assessment_id="ass-1",
        mission_id="m-1",
        tenant_id=context_a.tenant_id,
        status=MissionHealthStatus.AT_RISK,
        snapshot=snap,
        decision=dec_replan,
        evaluated_at=now,
        previous_status=MissionHealthStatus.HEALTHY,
        remediation_counter=1,
    )

    res_replan = dispatcher.dispatch(ass_replan, context_a)
    assert res_replan.status == DispatchStatus.EXECUTED
    assert len(planning_srv.replan_calls) == 1
    assert planning_srv.replan_calls[0][0] == "plan-100"

    # 2. Dispatch DELEGATION
    dec_deleg = SelfMonitoringDecision(
        action=SelfMonitoringAction.REQUEST_DELEGATION,
        health_status=MissionHealthStatus.AT_RISK,
        reason_codes=(DegradationReasonCode.SPECIALIST_AGENT_UNAVAILABLE,),
        rationale="Agent unavailable",
        evaluated_at=now,
        suggested_target="specialist_delegation",
        suggested_payload={"session_id": "sess-200", "subtask_id": "task-5", "target_agent_id": "agent-backup"},
    )
    ass_deleg = HealthAssessment(
        assessment_id="ass-2",
        mission_id="m-1",
        tenant_id=context_a.tenant_id,
        status=MissionHealthStatus.AT_RISK,
        snapshot=snap,
        decision=dec_deleg,
        evaluated_at=now,
        previous_status=MissionHealthStatus.HEALTHY,
        remediation_counter=1,
    )
    res_deleg = dispatcher.dispatch(ass_deleg, context_a)
    assert res_deleg.status == DispatchStatus.EXECUTED
    assert len(coord_srv.delegate_calls) == 1
    assert coord_srv.delegate_calls[0][0] == "sess-200"

    # 3. Dispatch PAUSE
    dec_pause = SelfMonitoringDecision(
        action=SelfMonitoringAction.PAUSE,
        health_status=MissionHealthStatus.AT_RISK,
        reason_codes=(DegradationReasonCode.MISSION_STALLED,),
        rationale="Stall temporal detected",
        evaluated_at=now,
    )
    ass_pause = HealthAssessment(
        assessment_id="ass-3",
        mission_id="m-1",
        tenant_id=context_a.tenant_id,
        status=MissionHealthStatus.AT_RISK,
        snapshot=snap,
        decision=dec_pause,
        evaluated_at=now,
        previous_status=MissionHealthStatus.HEALTHY,
        remediation_counter=1,
    )
    res_pause = dispatcher.dispatch(ass_pause, context_a)
    assert res_pause.status == DispatchStatus.EXECUTED
    assert len(mission_srv.pause_calls) == 1

    # 4. Dispatch BLOCK - Invariant: BLOCK must NEVER turn into replan or delegation
    dec_block = SelfMonitoringDecision(
        action=SelfMonitoringAction.BLOCK,
        health_status=MissionHealthStatus.BLOCKED,
        reason_codes=(DegradationReasonCode.POLICY_DENIED_VIOLATION,),
        rationale="Policy denial / hard budget exceeded",
        evaluated_at=now,
        requires_escalation=True,
    )
    ass_block = HealthAssessment(
        assessment_id="ass-4",
        mission_id="m-1",
        tenant_id=context_a.tenant_id,
        status=MissionHealthStatus.BLOCKED,
        snapshot=snap,
        decision=dec_block,
        evaluated_at=now,
        previous_status=MissionHealthStatus.HEALTHY,
        remediation_counter=1,
    )
    res_block = dispatcher.dispatch(ass_block, context_a)
    assert res_block.status == DispatchStatus.REJECTED
    assert "blocked" in res_block.message.lower()


# =========================================================================
# Escenario M: Integración real de SelfMonitoringAuditAdapter con K.1, K.2 y P.8
# =========================================================================

def test_scenario_m_real_audit_adapter_integration_with_k1_k2_p8(context_a, clock, tmp_path):
    audit_repo = MockAuditRepository()
    trace_repo = JsonAgentTraceRepository(tmp_path / "traces")
    trace_srv = AgentTraceService(trace_repository=trace_repo)
    alert_repo = JsonProductionAlertRepository()  # In-memory mode when base_dir is None
    alerting_srv = ProductionAlertingService(repository=alert_repo, clock=clock)

    adapter = SelfMonitoringAuditAdapter(
        audit_repository=audit_repo,
        trace_service=trace_srv,
        alerting_service=alerting_srv,
    )

    now = clock.now()
    snap = MissionHealthSnapshot(
        snapshot_id="snap-audit-1",
        mission_id="m-audit",
        tenant_id=context_a.tenant_id,
        health_status=MissionHealthStatus.AT_RISK,
        signals=(),
        reason_codes=(DegradationReasonCode.REPEATED_TECHNICAL_FAILURES,),
        captured_at=now,
    )
    dec = SelfMonitoringDecision(
        action=SelfMonitoringAction.ESCALATE,
        health_status=MissionHealthStatus.BLOCKED,
        reason_codes=(DegradationReasonCode.REPEATED_TECHNICAL_FAILURES,),
        rationale="Critical degradation after 3 failed remediations",
        evaluated_at=now,
        requires_escalation=True,
        escalation_severity="CRITICAL",
        suggested_payload={"private_token": "sk-secret-12345"},  # Zero-CoT / Sanitization check
    )
    assessment = HealthAssessment(
        assessment_id="ass-audit-1",
        mission_id="m-audit",
        tenant_id=context_a.tenant_id,
        status=MissionHealthStatus.BLOCKED,
        snapshot=snap,
        decision=dec,
        evaluated_at=now,
        previous_status=MissionHealthStatus.AT_RISK,
        remediation_counter=3,
        remediation_limit_reached=True,
    )

    # 1. Auditar assessment
    adapter.record_assessment_audited(assessment, context_a)
    assert audit_repo.count(context_a) == 1

    # 2. Auditar escalación -> Debe persistir en Audit K.1 y levantar Alerta P.8 deduplicada
    adapter.record_escalation(assessment, dec, context_a)
    assert audit_repo.count(context_a) == 2

    # Verificar alerta generada en P.8
    alerts = alert_repo.list_alerts(environment=ApplicationEnvironment.PRODUCTION, tenant_id=context_a.tenant_id)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule_type == AlertRuleType.MISSION_HEALTH_DEGRADED
    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.target_resource == "m-audit"
    # Zero-CoT Sanitization: metadata no debe exponer secretos
    assert "sk-secret" not in str(alert.evidence)

    # 3. Deduplicación / Cooldown de alerta P.8: Segunda escalación inmediata no debe crear nueva alerta activa duplicada
    adapter.record_escalation(assessment, dec, context_a)
    alerts_after = alert_repo.list_alerts(environment=ApplicationEnvironment.PRODUCTION, tenant_id=context_a.tenant_id)
    assert len(alerts_after) == 1  # Deduplicated and suppressed under cooldown
