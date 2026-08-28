import pytest
from typing import List, Dict, Any, Optional

from src.domain.mission.models import (
    LoopAction,
    LoopDecision,
    LoopState,
)
from src.domain.mission.ports import DecisionProvider, ActionExecutor
from src.application.mission.autonomous_loop import AutonomousLoop, LoopResult

class ScriptedDecisionProvider(DecisionProvider):
    """
    Fake DecisionProvider que devuelve decisiones preprogramadas en secuencia
    basándose en la iteración actual del estado.
    """
    def __init__(self, decisions: List[LoopDecision]):
        self.decisions = decisions

    def decide(self, state: LoopState) -> LoopDecision:
        index = state.iteration
        if index < len(self.decisions):
            return self.decisions[index]
        return LoopDecision(
            action=LoopAction.COMPLETE,
            reason="No more decisions provided"
        )

class RecordingActionExecutor(ActionExecutor):
    """
    Fake ActionExecutor que registra las decisiones ejecutadas y devuelve
    respuestas preconfiguradas o por defecto.
    """
    def __init__(self, responses: Optional[Dict[str, Any]] = None, raise_on_action: Optional[str] = None):
        self.executed_decisions: List[LoopDecision] = []
        self.responses = responses or {}
        self.raise_on_action = raise_on_action

    def execute(self, decision: LoopDecision, state: LoopState) -> dict:
        self.executed_decisions.append(decision)
        if self.raise_on_action and decision.action == self.raise_on_action:
            raise RuntimeError(f"Simulated execution failure for action {decision.action}")
        
        action_key = decision.action.value
        if action_key in self.responses:
            return self.responses[action_key]
        return {"status": "executed", "action": decision.action, "target": decision.target}

def test_loop_executes_action_and_completes():
    """
    TEST 1: El loop puede ejecutar una acción y terminar con COMPLETE.
    """
    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Initial search", target="target_1"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Goal achieved")
    ]
    provider = ScriptedDecisionProvider(decisions)
    executor = RecordingActionExecutor()
    loop = AutonomousLoop(decision_provider=provider, action_executor=executor, max_iterations=5)

    result = loop.run(mission_id="m-1", goal="Test goal", initial_target="start")

    assert result.status == "COMPLETED"
    assert result.output["iterations_used"] == 2
    assert len(executor.executed_decisions) == 1
    assert executor.executed_decisions[0].action == LoopAction.CONTINUE
    assert len(result.trace) == 2
    assert result.trace[0].action == LoopAction.CONTINUE
    assert result.trace[1].action == LoopAction.COMPLETE

def test_loop_changes_action_between_iterations_without_hardcoding():
    """
    TEST 2: El loop puede cambiar de acción entre iteraciones (ACTION_A -> ACTION_B -> COMPLETE)
    demostrando que AutonomousLoop NO contiene la secuencia hardcodeada.
    """
    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Perform step A", target="item_A", parameters={"param": "A"}),
        LoopDecision(action=LoopAction.PROMOTE, reason="Perform step B", target="item_B", parameters={"param": "B"}),
        LoopDecision(action=LoopAction.COMPLETE, reason="Workflow finished")
    ]
    provider = ScriptedDecisionProvider(decisions)
    executor = RecordingActionExecutor()
    loop = AutonomousLoop(decision_provider=provider, action_executor=executor, max_iterations=5)

    result = loop.run(mission_id="m-2", goal="Dynamic workflow")

    assert result.status == "COMPLETED"
    assert result.output["iterations_used"] == 3
    assert len(executor.executed_decisions) == 2
    assert executor.executed_decisions[0].action == LoopAction.CONTINUE
    assert executor.executed_decisions[1].action == LoopAction.PROMOTE
    assert result.trace[0].action == LoopAction.CONTINUE
    assert result.trace[1].action == LoopAction.PROMOTE
    assert result.trace[2].action == LoopAction.COMPLETE

def test_agent_can_pivot():
    """
    TEST 3: El agente puede hacer PIVOT (PIVOT -> CONTINUE -> COMPLETE).
    """
    decisions = [
        LoopDecision(action=LoopAction.PIVOT, reason="Market saturated, switching category", target="category_B"),
        LoopDecision(action=LoopAction.CONTINUE, reason="Search in category B", target="item_B1"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Target found")
    ]
    provider = ScriptedDecisionProvider(decisions)
    executor = RecordingActionExecutor()
    loop = AutonomousLoop(decision_provider=provider, action_executor=executor, max_iterations=5)

    result = loop.run(mission_id="m-3", goal="Pivot test", initial_target="category_A")

    assert result.status == "COMPLETED"
    assert result.output["iterations_used"] == 3
    assert len(executor.executed_decisions) == 2
    assert executor.executed_decisions[0].action == LoopAction.PIVOT
    assert executor.executed_decisions[1].action == LoopAction.CONTINUE
    assert result.final_state.current_target == "item_B1"

def test_agent_can_reject_and_terminate():
    """
    TEST 4: El agente puede REJECT y terminar correctamente.
    """
    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Evaluate item", target="item_X"),
        LoopDecision(action=LoopAction.REJECT, reason="No profit margin viable")
    ]
    provider = ScriptedDecisionProvider(decisions)
    executor = RecordingActionExecutor()
    loop = AutonomousLoop(decision_provider=provider, action_executor=executor, max_iterations=5)

    result = loop.run(mission_id="m-4", goal="Reject test")

    assert result.status == "REJECTED"
    assert result.output["iterations_used"] == 2
    assert len(executor.executed_decisions) == 1
    assert result.trace[1].action == LoopAction.REJECT
    assert result.trace[1].reason == "No profit margin viable"

def test_max_iterations_prevents_infinite_loop():
    """
    TEST 5: El límite máximo de iteraciones evita un loop infinito.
    """
    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Looping forever", target="target_loop")
    ] * 20
    provider = ScriptedDecisionProvider(decisions)
    executor = RecordingActionExecutor()
    loop = AutonomousLoop(decision_provider=provider, action_executor=executor, max_iterations=3)

    result = loop.run(mission_id="m-5", goal="Infinite loop test")

    assert result.status == "MAX_ITERATIONS_REACHED"
    assert result.output["iterations_used"] == 3
    assert len(executor.executed_decisions) == 3

def test_executor_exception_handled_gracefully():
    """
    TEST 6: Una excepción del executor queda correctamente representada y el loop termina de manera controlada.
    """
    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Will crash in executor", target="faulty_target")
    ]
    provider = ScriptedDecisionProvider(decisions)
    executor = RecordingActionExecutor(raise_on_action=LoopAction.CONTINUE)
    loop = AutonomousLoop(decision_provider=provider, action_executor=executor, max_iterations=5)

    result = loop.run(mission_id="m-6", goal="Error test")

    assert result.status == "ERROR"
    assert len(result.errors) == 1
    assert "Simulated execution failure" in result.errors[0]
    assert len(result.trace) == 1
    assert result.trace[0].observation.get("status") == "FAILED"
    assert "Simulated execution failure" in result.trace[0].observation.get("error")

def test_traceability_registers_decisions_and_observations():
    """
    TEST 7: La decisión y las observaciones quedan registradas en el historial/traza.
    """
    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Gather intel", target="item_100", parameters={"depth": 2}, confidence=0.95),
        LoopDecision(action=LoopAction.COMPLETE, reason="Sufficient data gathered", confidence=0.99)
    ]
    responses = {
        "CONTINUE": {"data": "Intel data", "metrics": [1, 2, 3]}
    }
    provider = ScriptedDecisionProvider(decisions)
    executor = RecordingActionExecutor(responses=responses)
    loop = AutonomousLoop(decision_provider=provider, action_executor=executor, max_iterations=5)

    result = loop.run(mission_id="m-7", goal="Traceability test")

    assert result.status == "COMPLETED"
    assert len(result.trace) == 2

    entry1 = result.trace[0]
    assert entry1.iteration == 1
    assert entry1.decision.confidence == 0.95
    assert entry1.reason == "Gather intel"
    assert entry1.action == LoopAction.CONTINUE
    assert entry1.target == "item_100"
    assert entry1.parameters == {"depth": 2}
    assert entry1.observation == {"data": "Intel data", "metrics": [1, 2, 3]}
    assert entry1.timestamp is not None

    entry2 = result.trace[1]
    assert entry2.iteration == 2
    assert entry2.action == LoopAction.COMPLETE
    assert entry2.reason == "Sufficient data gathered"
    assert entry2.observation == {"status": "COMPLETED"}

    # Verificar que el historial del estado final también conserva las decisiones y observaciones
    assert len(result.final_state.decision_history) == 2
    assert len(result.final_state.observations) == 1
    assert result.final_state.observations[0] == {"data": "Intel data", "metrics": [1, 2, 3]}

def test_loop_state_immutability():
    """
    TEST 8: Demuestra que LoopState, LoopDecision y LoopTraceEntry son inmutables.
    """
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Test immutability",
        parameters={"key": "value"}
    )
    state = LoopState(
        mission_id="m-immutability",
        iteration=1,
        goal="Immutability goal",
        observations=[{"obs_key": "obs_val"}],
        evidences=["ev_1"],
        decision_history=[decision]
    )

    # 1. dataclass frozen check
    with pytest.raises(Exception):
        state.iteration = 2

    # 2. tuple collections assignment check
    with pytest.raises(AttributeError):
        state.observations.append({"obs_key2": "obs_val2"})

    with pytest.raises(AttributeError):
        state.evidences.append("ev_2")

    with pytest.raises(AttributeError):
        state.decision_history.append(decision)

    # 3. dict inside observations read-only check (MappingProxyType)
    with pytest.raises(TypeError):
        state.observations[0]["obs_key"] = "modified"

    # 4. LoopDecision parameters read-only check
    with pytest.raises(TypeError):
        decision.parameters["key"] = "modified"
