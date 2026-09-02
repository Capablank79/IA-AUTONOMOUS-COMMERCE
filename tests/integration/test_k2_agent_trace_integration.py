"""
Tests de Integración y E2E para Agent Trace (Hito K.2).

Valida:
1. AutonomousLoop con Agent Trace operativo (START -> OBSERVE -> SERVICE -> TOOL -> COMPLETE).
2. ContinuousMissionService (J.7) ciclo de autonomía con Agent Trace generado y enlazado.
3. Reconstrucción determinista tras reinicio / recarga de repositorio.
4. Fallo controlado (ActionExecutor error) reflejado en trazas y Audit Trail sin voltear el sistema.
5. Preservación de incertidumbre UNKNOWN en traza de agente.
6. Enlace cruzado con AuditRecord (K.1) sin duplicación ni interferencia.
7. Exclusión estricta de Chain-of-Thought y sanitización de secretos en runtime.
"""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import tempfile
import shutil

from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
    ExecutionTraceTimeline,
)
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.application.agent_trace.agent_trace_service import AgentTraceService

from src.domain.mission.models import LoopAction, LoopDecision, LoopState, MissionType, MissionPriority, MissionStatus
from src.domain.mission.ports import DecisionProvider, ActionExecutor
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits

from src.domain.continuous_mission.models import (
    ContinuousMission,
    ContinuousMissionCycle,
    ContinuousMissionStatus,
    ContinuousCycleStatus,
    ContinuousMissionStopCondition,
)
from src.domain.continuous_mission.ports import (
    ContinuousMissionRepositoryPort,
    CycleExecutorPort,
)
from src.application.continuous_mission.service import ContinuousMissionService

from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.audit.audit_trail_service import AuditTrailService


class MockDecisionProvider(DecisionProvider):
    def __init__(self, actions):
        self.actions = list(actions)
        self.call_count = 0

    def decide(self, state: LoopState) -> LoopDecision:
        self.call_count += 1
        if self.actions:
            return self.actions.pop(0)
        return LoopDecision(
            action=LoopAction.COMPLETE,
            reason="Completed by mock sequence",
            parameters={},
            target=state.current_target,
        )


class MockActionExecutor(ActionExecutor):
    def __init__(self, failure_on_action=False):
        self.failure_on_action = failure_on_action
        self.executed_actions = []

    def execute(self, decision: LoopDecision, state: LoopState) -> dict:
        self.executed_actions.append(decision)
        if self.failure_on_action:
            raise RuntimeError("Controlled action execution failure")
        return {"status": "SUCCESS", "executed_action": decision.action.value}


class InMemoryContinuousMissionRepo(ContinuousMissionRepositoryPort):
    def __init__(self):
        self.missions = {}
        self.cycles = {}

    def save(self, continuous_mission: ContinuousMission) -> None:
        self.missions[continuous_mission.continuous_mission_id] = continuous_mission

    def get_by_id(self, continuous_mission_id: str) -> Optional[ContinuousMission]:
        return self.missions.get(continuous_mission_id)

    def get_by_schedule_id(self, schedule_id: str) -> Optional[ContinuousMission]:
        for m in self.missions.values():
            if m.schedule_id == schedule_id:
                return m
        return None

    def list_all(self) -> List[ContinuousMission]:
        return list(self.missions.values())

    def list_active(self) -> List[ContinuousMission]:
        return [m for m in self.missions.values() if m.status == ContinuousMissionStatus.ACTIVE]

    def save_cycle(self, cycle: ContinuousMissionCycle) -> None:
        self.cycles[cycle.cycle_id] = cycle

    def get_cycle(self, cycle_id: str) -> Optional[ContinuousMissionCycle]:
        return self.cycles.get(cycle_id)

    def get_cycle_by_idempotency_key(self, idempotency_key: str) -> Optional[ContinuousMissionCycle]:
        for c in self.cycles.values():
            if c.idempotency_key == idempotency_key:
                return c
        return None

    def list_cycles(self, continuous_mission_id: Optional[str] = None) -> List[ContinuousMissionCycle]:
        res = list(self.cycles.values())
        if continuous_mission_id:
            res = [c for c in res if c.continuous_mission_id == continuous_mission_id]
        return res

class MockCycleExecutor(CycleExecutorPort):
    def __init__(self, cycle_status=ContinuousCycleStatus.SUCCESS):
        self.cycle_status = cycle_status
        self.call_count = 0

    def execute_cycle(self, mission: ContinuousMission, cycle: ContinuousMissionCycle):
        self.call_count += 1
        return self.cycle_status, f"mis-gen-{cycle.cycle_id}", {"result": "ok"}, None


@pytest.fixture
def temp_dir():
    dir_path = tempfile.mkdtemp()
    yield Path(dir_path)
    shutil.rmtree(dir_path, ignore_errors=True)


# 1. AutonomousLoop Integration con Agent Trace
def test_integration_autonomous_loop_agent_trace(temp_dir):
    trace_repo = JsonAgentTraceRepository(temp_dir / "traces")
    trace_service = AgentTraceService(trace_repo)

    decisions = [
        LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Continue exploration",
            parameters={"query": "electronics"},
            target="target_1",
        ),
        LoopDecision(
            action=LoopAction.COMPLETE,
            reason="Goal achieved",
            parameters={},
            target="target_1",
        ),
    ]
    provider = MockDecisionProvider(decisions)
    executor = MockActionExecutor()

    loop = AutonomousLoop(
        decision_provider=provider,
        action_executor=executor,
        max_iterations=5,
        agent_trace_service=trace_service,
    )

    result = loop.run(
        mission_id="mis-integ-1",
        goal="Discover winning items",
        execution_id="exec-integ-1",
    )

    assert result.status == "COMPLETED"

    # Verificar traza registrada
    timeline = trace_service.get_execution_timeline("exec-integ-1")
    assert timeline.execution_id == "exec-integ-1"
    assert timeline.status == TraceStatus.SUCCESS
    assert timeline.total_steps >= 4

    step_types = [s.step_type for s in timeline.steps]
    assert StepType.START in step_types
    assert StepType.OBSERVE in step_types
    assert StepType.TOOL_CALL in step_types
    assert StepType.COMPLETE in step_types


# 2. ContinuousMission Integration con Agent Trace
def test_integration_continuous_mission_agent_trace(temp_dir):
    trace_repo = JsonAgentTraceRepository(temp_dir / "traces")
    trace_service = AgentTraceService(trace_repo)
    cm_repo = InMemoryContinuousMissionRepo()
    cycle_exec = MockCycleExecutor(ContinuousCycleStatus.SUCCESS)

    cm_service = ContinuousMissionService(
        repository=cm_repo,
        cycle_executor=cycle_exec,
        agent_trace_service=trace_service,
    )

    cm = cm_service.create_continuous_mission(
        schedule_id="sch-001",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Continuous surveillance",
        continuous_mission_id="cm-001",
    )
    cm_service.start_mission("cm-001")

    cycle = cm_service.execute_next_cycle("cm-001")
    assert cycle.status == ContinuousCycleStatus.SUCCESS

    traces = trace_service.list_records(mission_id="cm-001")
    assert len(traces) >= 2
    assert any(t.step_type == StepType.START for t in traces)
    assert any(t.step_type == StepType.COMPLETE for t in traces)


# 3. Controlled failure E2E & isolation
def test_integration_failure_trace_and_audit(temp_dir):
    trace_repo = JsonAgentTraceRepository(temp_dir / "traces")
    trace_service = AgentTraceService(trace_repo)
    audit_repo = JsonAuditRepository(temp_dir / "audit")
    audit_service = AuditTrailService(audit_repo)

    # Provider que intenta una acción que falla
    provider = MockDecisionProvider([
            LoopDecision(
                action=LoopAction.CONTINUE,
                reason="Continue will fail",
                parameters={},
                target="target_fail",
            )
        ])
    failing_executor = MockActionExecutor(failure_on_action=True)

    loop = AutonomousLoop(
        decision_provider=provider,
        action_executor=failing_executor,
        max_iterations=5,
        agent_trace_service=trace_service,
    )

    result = loop.run(
            mission_id="mis-fail-1",
            goal="Testing failure trace",
            execution_id="exec-fail-1",
            correlation_id="exec-fail-1",
        )

    assert result.status == "ERROR"

    timeline = trace_service.get_execution_timeline("exec-fail-1")
    assert timeline.status == TraceStatus.FAILED

    failures = [s for s in timeline.steps if s.status == TraceStatus.FAILED or s.step_type == StepType.FAILURE]
    assert len(failures) >= 1
    assert "Controlled action execution failure" in failures[0].output_reference

    # Registrar hecho auditable en Audit Trail para demostrar enlace no duplicado
    audit_rec = audit_service.record_mission_state_changed(
        mission_id="mis-fail-1",
        previous_status=MissionStatus.RUNNING,
        new_status=MissionStatus.FAILED,
        correlation_id="exec-fail-1",
    )

    assert audit_rec is not None
    assert audit_rec.correlation_id == timeline.correlation_id
    assert audit_rec.mission_id == timeline.mission_id


# 4. UNKNOWN state handling in Agent Trace
def test_integration_unknown_state_trace(temp_dir):
    trace_repo = JsonAgentTraceRepository(temp_dir / "traces")
    trace_service = AgentTraceService(trace_repo)

    cm_repo = InMemoryContinuousMissionRepo()
    cycle_exec = MockCycleExecutor(ContinuousCycleStatus.UNKNOWN)

    cm_service = ContinuousMissionService(
        repository=cm_repo,
        cycle_executor=cycle_exec,
        agent_trace_service=trace_service,
    )

    cm = cm_service.create_continuous_mission(
        schedule_id="sch-unk",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Surveillance with uncertainty",
        continuous_mission_id="cm-unk",
    )
    cm_service.start_mission("cm-unk")

    cycle = cm_service.execute_next_cycle("cm-unk")
    assert cycle.status == ContinuousCycleStatus.UNKNOWN

    traces = trace_service.list_records(mission_id="cm-unk", status=TraceStatus.UNKNOWN)
    assert len(traces) >= 1
    assert traces[0].status == TraceStatus.UNKNOWN


# 5. Restart, Persistence and Security check
def test_integration_restart_persistence_and_security(temp_dir):
    storage_path = temp_dir / "traces_durable"
    repo1 = JsonAgentTraceRepository(storage_path)
    service1 = AgentTraceService(repo1)

    rec = service1.record_step(
        component_name="SurveillanceAgent",
        execution_id="exec-dur-1",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="FETCH_MARKET",
        status=TraceStatus.SUCCESS,
        metadata={
            "api_key": "super_secret_token",
            "chain_of_thought": "Privately thinking...",
            "valid_metric": 42,
        },
    )

    del service1
    del repo1

    # Reload from disk
    repo2 = JsonAgentTraceRepository(storage_path)
    loaded_rec = repo2.get_by_id(rec.trace_id)

    assert loaded_rec is not None
    assert loaded_rec.metadata["api_key"] == "[REDACTED]"
    assert loaded_rec.metadata["chain_of_thought"] == "[REDACTED]"
    assert loaded_rec.metadata["valid_metric"] == 42
    assert loaded_rec.verify_checksum() is True
