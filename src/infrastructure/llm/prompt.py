import json
from typing import Dict, Any
from src.domain.mission.models import LoopState, LoopAction


DECISION_SYSTEM_PROMPT = """You are an autonomous decision engine for business missions.
Your role is to analyze the current LoopState and determine the single next action to take.

You MUST respond strictly with a valid JSON object matching this schema:
{
  "action": "<ACTION_NAME>",
  "reason": "<explanation of the decision>",
  "target": "<target identifier string or null>",
  "parameters": {<key-value pairs of parameters>},
  "confidence": <float between 0.0 and 1.0 or null>
}

Valid actions:
- CONTINUE: Keep investigating or performing the current step/workflow.
- PIVOT: Change course, shift category, or switch focus when a dead end or better alternative is detected.
- REJECT: Abort or reject the opportunity/mission based on negative findings or failed criteria.
- PROMOTE: Escalate, advance the opportunity to the next phase, or approve the finding.
- COMPLETE: Successfully finish the mission when the goal has been achieved.

Rules:
1. Return ONLY the JSON object. Do not enclose in markdown blocks, do not include preamble or trailing text.
2. Base your decision ONLY on the provided state, observations, and evidences. Do not invent or assume facts not present.
3. Choose the action dynamically based on context; there is no fixed sequence of steps.
4. "action" must be one of: CONTINUE, PIVOT, REJECT, PROMOTE, COMPLETE.
5. "parameters" must be a JSON object (can be empty {}).
6. "confidence" if provided must be a float between 0.0 and 1.0 (or null).
"""


def build_user_prompt(state: LoopState) -> str:
    """
    Construye el payload de contexto del LoopState para el LLM en formato JSON legible.
    """
    decision_history_data = [
        {
            "action": d.action.value if isinstance(d.action, LoopAction) else str(d.action),
            "reason": d.reason,
            "target": d.target,
            "parameters": dict(d.parameters),
            "confidence": d.confidence
        }
        for d in state.decision_history
    ]

    state_data: Dict[str, Any] = {
        "mission_id": state.mission_id,
        "iteration": state.iteration,
        "goal": state.goal,
        "current_target": state.current_target,
        "observations": list(state.observations),
        "evidences": list(state.evidences),
        "decision_history": decision_history_data
    }

    return (
        "Current LoopState:\n"
        f"{json.dumps(state_data, indent=2, default=str)}\n\n"
        "Evaluate the state and return your decision JSON."
    )
