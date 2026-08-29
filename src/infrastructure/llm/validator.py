import json
import re
from typing import Any, Mapping
from src.domain.mission.models import LoopAction, LoopDecision
from src.infrastructure.llm.exceptions import OmniRouteContractValidationError


def parse_and_validate_decision(raw_text: str) -> LoopDecision:
    """
    Parsea y valida estrictamente el texto devuelto por el LLM.
    Si la respuesta no cumple estrictamente el contrato, lanza OmniRouteContractValidationError.
    """
    if not raw_text or not raw_text.strip():
        raise OmniRouteContractValidationError("LLM returned empty or whitespace-only response")

    cleaned = raw_text.strip()
    # Permitir extraer json si el modelo accidentalmente incluyó markdown codeblocks ```json ... ```
    if cleaned.startswith("```"):
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if match:
            cleaned = match.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise OmniRouteContractValidationError(f"Invalid JSON in LLM response: {str(exc)}") from exc

    if not isinstance(data, dict):
        raise OmniRouteContractValidationError(f"LLM response must be a JSON object/dict, got {type(data).__name__}")

    # 1. Validar action
    if "action" not in data:
        raise OmniRouteContractValidationError("Missing required field 'action'")
    raw_action = data["action"]
    if not isinstance(raw_action, str):
        raise OmniRouteContractValidationError(f"'action' must be a string, got {type(raw_action).__name__}")
    
    try:
        action_enum = LoopAction(raw_action)
    except ValueError:
        valid_actions = [a.value for a in LoopAction]
        raise OmniRouteContractValidationError(
            f"Unknown or invalid action '{raw_action}'. Must be one of: {valid_actions}"
        )

    # 2. Validar reason
    if "reason" not in data:
        raise OmniRouteContractValidationError("Missing required field 'reason'")
    reason = data["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise OmniRouteContractValidationError(f"'reason' must be a non-empty string, got {type(reason).__name__}")

    # 3. Validar target
    target = data.get("target")
    if target is not None and not isinstance(target, str):
        raise OmniRouteContractValidationError(f"'target' must be a string or null, got {type(target).__name__}")

    # 4. Validar parameters
    parameters = data.get("parameters", {})
    if parameters is None:
        parameters = {}
    elif not isinstance(parameters, (dict, Mapping)):
        raise OmniRouteContractValidationError(
            f"'parameters' must be a dictionary/mapping, got {type(parameters).__name__}"
        )

    # 5. Validar confidence
    confidence = data.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise OmniRouteContractValidationError(
                f"'confidence' must be a number or null, got {type(confidence).__name__}"
            )
        confidence_float = float(confidence)
        if not (0.0 <= confidence_float <= 1.0):
            raise OmniRouteContractValidationError(
                f"'confidence' must be between 0.0 and 1.0, got {confidence_float}"
            )
        confidence = confidence_float

    return LoopDecision(
        action=action_enum,
        reason=reason,
        target=target,
        parameters=dict(parameters),
        confidence=confidence
    )
