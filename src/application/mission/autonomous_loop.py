from datetime import datetime, timezone
import time
from typing import Dict, Any, List, Optional, Callable, Tuple
from dataclasses import dataclass, field
from decimal import Decimal

from src.domain.mission.models import (
    LoopAction,
    LoopDecision,
    LoopState,
    LoopTraceEntry,
)
from src.domain.mission.ports import DecisionProvider, ActionExecutor

@dataclass
class LoopLimits:
    max_iterations: int = 10
    max_time_seconds: Optional[float] = None
    max_external_calls: Optional[int] = None
    max_cost_usd: Optional[Decimal] = None

@dataclass
class LoopResult:
    status: str  # "COMPLETED", "REJECTED", "MAX_ITERATIONS_REACHED", "TIME_LIMIT_REACHED", "CALL_LIMIT_REACHED", "CONVERGED", "ERROR", etc.
    final_state: LoopState
    trace: List[LoopTraceEntry] = field(default_factory=list)
    output: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    termination_reason: Optional[str] = None

class AutonomousLoop:
    """
    Motor del loop autónomo (observe -> evaluate -> decide -> act -> observe -> learn -> ...).
    Totalmente agnóstico del proveedor de decisiones (DecisionProvider)
    y del ejecutor de acciones (ActionExecutor).

    Soporta:
    - Validación determinista de COMPLETE (el LLM no tiene autoridad unilateral)
    - Límites operativos configurables (max_iterations, time, calls, cost)
    - Tracking inmutable de best_known y métricas de progreso
    - Manejo y recuperación ante fallos del ejecutor o decisiones inválidas
    """

    def __init__(
        self,
        decision_provider: DecisionProvider,
        action_executor: ActionExecutor,
        max_iterations: int = 10,
        limits: Optional[LoopLimits] = None,
        completion_validator: Optional[Callable[[LoopState], Tuple[bool, str]]] = None,
        state_enhancer: Optional[Callable[[LoopState, Dict[str, Any]], LoopState]] = None
    ):
        if max_iterations <= 0:
            raise ValueError("max_iterations must be greater than zero")
        self.decision_provider = decision_provider
        self.action_executor = action_executor
        self.max_iterations = max_iterations
        self.limits = limits or LoopLimits(max_iterations=max_iterations)
        self.completion_validator = completion_validator
        self.state_enhancer = state_enhancer

    def run(self, mission_id: str, goal: str, initial_target: Optional[str] = None) -> LoopResult:
        state = LoopState(
            mission_id=mission_id,
            iteration=0,
            goal=goal,
            current_target=initial_target,
            observations=(),
            evidences=(),
            decision_history=()
        )

        trace: List[LoopTraceEntry] = []
        errors: List[str] = []
        finished = False
        final_status = "MAX_ITERATIONS_REACHED"
        termination_reason = "MAX_ITERATIONS"
        start_time = time.time()

        while state.iteration < self.limits.max_iterations and not finished:
            # Comprobar límite de tiempo si está configurado
            if self.limits.max_time_seconds is not None:
                elapsed = time.time() - start_time
                if elapsed >= self.limits.max_time_seconds:
                    final_status = "TIME_LIMIT_REACHED"
                    termination_reason = "TIME_LIMIT"
                    break

            # Comprobar límite de llamadas externas si el executor lo expone
            if self.limits.max_external_calls is not None and hasattr(self.action_executor, "external_calls_count"):
                if self.action_executor.external_calls_count >= self.limits.max_external_calls:
                    final_status = "CALL_LIMIT_REACHED"
                    termination_reason = "CALL_LIMIT"
                    break

            current_iteration = state.iteration + 1
            
            # 1. Decide
            try:
                decision = self.decision_provider.decide(state)
            except Exception as e:
                error_msg = f"DecisionProvider error at iteration {current_iteration}: {str(e)}"
                errors.append(error_msg)
                final_status = "ERROR"
                termination_reason = "ERROR"
                break

            if not isinstance(decision, LoopDecision):
                error_msg = f"Invalid decision returned by DecisionProvider at iteration {current_iteration}: {decision}"
                errors.append(error_msg)
                final_status = "ERROR"
                termination_reason = "INVALID_DECISION"
                break

            # 2. Check terminal action: COMPLETE
            if decision.action == LoopAction.COMPLETE:
                # Validar deterministamente si la misión realmente cumple los criterios de finalización
                can_complete = True
                validation_reason = "Validation passed"
                if self.completion_validator is not None:
                    can_complete, validation_reason = self.completion_validator(state)

                if can_complete:
                    updated_history = tuple(state.decision_history) + (decision,)
                    state = LoopState(
                        mission_id=state.mission_id,
                        iteration=current_iteration,
                        goal=state.goal,
                        current_target=state.current_target,
                        observations=state.observations,
                        evidences=state.evidences,
                        decision_history=updated_history,
                        best_known=state.best_known,
                        progress=state.progress
                    )
                    obs_payload = {"status": "COMPLETED"}
                    if validation_reason != "Validation passed":
                        obs_payload["validation"] = validation_reason
                    trace.append(LoopTraceEntry(
                        iteration=current_iteration,
                        decision=decision,
                        reason=decision.reason,
                        action=decision.action,
                        target=decision.target or state.current_target,
                        parameters=decision.parameters,
                        observation=obs_payload,
                        timestamp=datetime.now(timezone.utc)
                    ))
                    finished = True
                    final_status = "COMPLETED"
                    termination_reason = "CONVERGED"
                    break
                else:
                    # El LLM intentó completar sin suficiente evidencia/score/cobertura: Rechazar COMPLETE prematuro y continuar loop
                    rej_observation = {
                        "status": "PREMATURE_COMPLETION_REJECTED",
                        "validation_error": validation_reason,
                        "instruction": "Continue gathering evidence or exploring promising targets before completing."
                    }
                    updated_history = tuple(state.decision_history) + (decision,)
                    updated_observations = tuple(state.observations) + (rej_observation,)
                    state = LoopState(
                        mission_id=state.mission_id,
                        iteration=current_iteration,
                        goal=state.goal,
                        current_target=state.current_target,
                        observations=updated_observations,
                        evidences=state.evidences,
                        decision_history=updated_history,
                        best_known=state.best_known,
                        progress=state.progress
                    )
                    trace.append(LoopTraceEntry(
                        iteration=current_iteration,
                        decision=decision,
                        reason=f"Attempted COMPLETE rejected: {validation_reason}",
                        action=decision.action,
                        target=state.current_target,
                        parameters=decision.parameters,
                        observation=rej_observation,
                        timestamp=datetime.now(timezone.utc)
                    ))
                    continue

            # Check terminal action: REJECT
            if decision.action == LoopAction.REJECT:
                updated_history = tuple(state.decision_history) + (decision,)
                state = LoopState(
                    mission_id=state.mission_id,
                    iteration=current_iteration,
                    goal=state.goal,
                    current_target=state.current_target,
                    observations=state.observations,
                    evidences=state.evidences,
                    decision_history=updated_history,
                    best_known=state.best_known,
                    progress=state.progress
                )
                trace.append(LoopTraceEntry(
                    iteration=current_iteration,
                    decision=decision,
                    reason=decision.reason,
                    action=decision.action,
                    target=decision.target or state.current_target,
                    parameters=decision.parameters,
                    observation={"status": "REJECTED"},
                    timestamp=datetime.now(timezone.utc)
                ))
                finished = True
                final_status = "REJECTED"
                termination_reason = "REJECTED"
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
                
                updated_history = tuple(state.decision_history) + (decision,)
                updated_observations = tuple(state.observations) + (observation,)
                state = LoopState(
                    mission_id=state.mission_id,
                    iteration=current_iteration,
                    goal=state.goal,
                    current_target=decision.target if decision.target is not None else state.current_target,
                    observations=updated_observations,
                    evidences=state.evidences,
                    decision_history=updated_history,
                    best_known=state.best_known,
                    progress=state.progress
                )
                trace.append(LoopTraceEntry(
                    iteration=current_iteration,
                    decision=decision,
                    reason=decision.reason,
                    action=decision.action,
                    target=decision.target or state.current_target,
                    parameters=decision.parameters,
                    observation=observation,
                    timestamp=datetime.now(timezone.utc)
                ))
                finished = True
                final_status = "ERROR"
                termination_reason = "EXECUTOR_ERROR"
                break

            # 4. Update State for next iteration
            next_target = decision.target if decision.target is not None else state.current_target
            updated_history = tuple(state.decision_history) + (decision,)
            updated_observations = tuple(state.observations) + (observation,)
            
            # Si hay un state_enhancer configurado, permitir enriquecer evidencias, best_known y progress
            new_state = LoopState(
                mission_id=state.mission_id,
                iteration=current_iteration,
                goal=state.goal,
                current_target=next_target,
                observations=updated_observations,
                evidences=state.evidences,
                decision_history=updated_history,
                best_known=state.best_known,
                progress=state.progress
            )

            if self.state_enhancer is not None:
                try:
                    new_state = self.state_enhancer(new_state, observation)
                except Exception as e:
                    errors.append(f"State enhancer warning: {str(e)}")

            state = new_state

            trace_score = float(state.best_known.score) if state.best_known and hasattr(state.best_known, "score") else None
            trace.append(LoopTraceEntry(
                iteration=current_iteration,
                decision=decision,
                reason=decision.reason,
                action=decision.action,
                target=next_target,
                parameters=decision.parameters,
                observation=observation,
                timestamp=datetime.now(timezone.utc),
                score=trace_score
            ))

        return LoopResult(
            status=final_status,
            final_state=state,
            trace=trace,
            output={
                "iterations_used": state.iteration,
                "max_iterations": self.limits.max_iterations,
                "final_target": state.current_target,
                "best_known": state.best_known,
                "progress": state.progress
            },
            errors=errors,
            termination_reason=termination_reason
        )
