from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from src.domain.mission.models import (
    LoopAction,
    LoopDecision,
    LoopState,
    LoopTraceEntry,
)
from src.domain.mission.ports import DecisionProvider, ActionExecutor

@dataclass
class LoopResult:
    status: str  # "COMPLETED", "REJECTED", "MAX_ITERATIONS_REACHED", "ERROR", etc.
    final_state: LoopState
    trace: List[LoopTraceEntry] = field(default_factory=list)
    output: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

class AutonomousLoop:
    """
    Motor del loop autónomo (observe -> decide -> act -> observe -> ...).
    Totalmente agnóstico del proveedor de decisiones (DecisionProvider)
    y del ejecutor de acciones (ActionExecutor).
    """

    def __init__(
        self,
        decision_provider: DecisionProvider,
        action_executor: ActionExecutor,
        max_iterations: int = 10
    ):
        if max_iterations <= 0:
            raise ValueError("max_iterations must be greater than zero")
        self.decision_provider = decision_provider
        self.action_executor = action_executor
        self.max_iterations = max_iterations

    def run(self, mission_id: str, goal: str, initial_target: Optional[str] = None) -> LoopResult:
        state = LoopState(
            mission_id=mission_id,
            iteration=0,
            goal=goal,
            current_target=initial_target,
            observations=[],
            evidences=[],
            decision_history=[]
        )

        trace: List[LoopTraceEntry] = []
        errors: List[str] = []
        finished = False
        final_status = "MAX_ITERATIONS_REACHED"

        while state.iteration < self.max_iterations and not finished:
            current_iteration = state.iteration + 1
            
            # 1. Decide
            try:
                decision = self.decision_provider.decide(state)
            except Exception as e:
                error_msg = f"DecisionProvider error at iteration {current_iteration}: {str(e)}"
                errors.append(error_msg)
                final_status = "ERROR"
                break

            if not isinstance(decision, LoopDecision):
                error_msg = f"Invalid decision returned by DecisionProvider at iteration {current_iteration}: {decision}"
                errors.append(error_msg)
                final_status = "ERROR"
                break

            # 2. Check terminal actions before or after execution
            if decision.action == LoopAction.COMPLETE:
                # Transición terminal exitosa
                updated_history = tuple(state.decision_history) + (decision,)
                state = LoopState(
                    mission_id=state.mission_id,
                    iteration=current_iteration,
                    goal=state.goal,
                    current_target=state.current_target,
                    observations=state.observations,
                    evidences=state.evidences,
                    decision_history=updated_history
                )
                trace.append(LoopTraceEntry(
                    iteration=current_iteration,
                    decision=decision,
                    reason=decision.reason,
                    action=decision.action,
                    target=decision.target or state.current_target,
                    parameters=decision.parameters,
                    observation={"status": "COMPLETED"},
                    timestamp=datetime.utcnow()
                ))
                finished = True
                final_status = "COMPLETED"
                break

            if decision.action == LoopAction.REJECT:
                # Transición terminal rechazada
                updated_history = tuple(state.decision_history) + (decision,)
                state = LoopState(
                    mission_id=state.mission_id,
                    iteration=current_iteration,
                    goal=state.goal,
                    current_target=state.current_target,
                    observations=state.observations,
                    evidences=state.evidences,
                    decision_history=updated_history
                )
                trace.append(LoopTraceEntry(
                    iteration=current_iteration,
                    decision=decision,
                    reason=decision.reason,
                    action=decision.action,
                    target=decision.target or state.current_target,
                    parameters=decision.parameters,
                    observation={"status": "REJECTED"},
                    timestamp=datetime.utcnow()
                ))
                finished = True
                final_status = "REJECTED"
                break

            # 3. Act (Execute non-terminal action)
            observation = {}
            try:
                raw_observation = self.action_executor.execute(decision, state)
                if isinstance(raw_observation, dict):
                    observation = raw_observation
                else:
                    observation = {"result": raw_observation}
            except Exception as e:
                error_msg = f"ActionExecutor error at iteration {current_iteration}: {str(e)}"
                errors.append(error_msg)
                observation = {"error": str(e), "status": "FAILED"}
                
                # Actualizamos trazabilidad y estado antes de terminar por error
                updated_history = tuple(state.decision_history) + (decision,)
                updated_observations = tuple(state.observations) + (observation,)
                state = LoopState(
                    mission_id=state.mission_id,
                    iteration=current_iteration,
                    goal=state.goal,
                    current_target=decision.target if decision.target is not None else state.current_target,
                    observations=updated_observations,
                    evidences=state.evidences,
                    decision_history=updated_history
                )
                trace.append(LoopTraceEntry(
                    iteration=current_iteration,
                    decision=decision,
                    reason=decision.reason,
                    action=decision.action,
                    target=decision.target or state.current_target,
                    parameters=decision.parameters,
                    observation=observation,
                    timestamp=datetime.utcnow()
                ))
                finished = True
                final_status = "ERROR"
                break

            # 4. Update State for next iteration
            next_target = decision.target if decision.target is not None else state.current_target
            updated_history = tuple(state.decision_history) + (decision,)
            updated_observations = tuple(state.observations) + (observation,)
            
            state = LoopState(
                mission_id=state.mission_id,
                iteration=current_iteration,
                goal=state.goal,
                current_target=next_target,
                observations=updated_observations,
                evidences=state.evidences,
                decision_history=updated_history
            )

            trace.append(LoopTraceEntry(
                iteration=current_iteration,
                decision=decision,
                reason=decision.reason,
                action=decision.action,
                target=next_target,
                parameters=decision.parameters,
                observation=observation,
                timestamp=datetime.utcnow()
            ))

        return LoopResult(
            status=final_status,
            final_state=state,
            trace=trace,
            output={
                "iterations_used": state.iteration,
                "max_iterations": self.max_iterations,
                "final_target": state.current_target
            },
            errors=errors
        )
