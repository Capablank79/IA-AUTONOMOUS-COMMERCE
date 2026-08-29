import json
from unittest.mock import patch, MagicMock
import pytest

from src.application.mission.autonomous_loop import AutonomousLoop
from src.domain.mission.models import LoopAction, LoopDecision, LoopState
from src.domain.mission.ports import ActionExecutor
from src.infrastructure.llm.config import OmniRouteConfig
from src.infrastructure.llm.omniroute_decision_provider import OmniRouteDecisionProvider


class DummyActionExecutor(ActionExecutor):
    def __init__(self):
        self.executed_decisions = []

    def execute(self, decision: LoopDecision, state: LoopState) -> dict:
        self.executed_decisions.append(decision)
        return {
            "status": "SUCCESS",
            "executed_action": decision.action.value,
            "found_items": 3
        }


def _mock_llm_sequence(responses):
    """
    Crea un generador de respuestas HTTP mockeadas para simular múltiples iteraciones del LLM.
    """
    call_idx = 0

    def fake_urlopen(req, timeout=None):
        nonlocal call_idx
        resp_payload = responses[call_idx]
        call_idx += 1

        body_json = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(resp_payload)
                    }
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body_json).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        return mock_resp

    return fake_urlopen


def test_autonomous_loop_with_omniroute_provider_composition():
    """
    FASE 9: Composición de AutonomousLoop + OmniRouteDecisionProvider inyectado.
    Demuestra que el loop opera normalmente con OmniRouteDecisionProvider sin modificar el core.
    """
    # 1. Configurar adaptador con respuestas simuladas
    llm_responses = [
        {
            "action": "CONTINUE",
            "reason": "Search candidate listings",
            "target": "target_search_1",
            "parameters": {"depth": 1},
            "confidence": 0.9
        },
        {
            "action": "PROMOTE",
            "reason": "High margin candidate found",
            "target": "item_candidate_42",
            "parameters": {"roi": 0.4},
            "confidence": 0.95
        },
        {
            "action": "COMPLETE",
            "reason": "Mission objective accomplished",
            "target": "item_candidate_42",
            "parameters": {},
            "confidence": 1.0
        }
    ]

    config = OmniRouteConfig(base_url="http://localhost:20128/v1", model="auto/best-coding")
    provider = OmniRouteDecisionProvider(config=config)
    executor = DummyActionExecutor()

    # 2. Inyectar dependencias en AutonomousLoop
    loop = AutonomousLoop(
        decision_provider=provider,
        action_executor=executor,
        max_iterations=5
    )

    # 3. Ejecutar loop
    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen", side_effect=_mock_llm_sequence(llm_responses)):
        result = loop.run(
            mission_id="mission-composition-test",
            goal="Identify and promote profitable item"
        )

    # 4. Verificaciones
    assert result.status == "COMPLETED"
    assert result.output["iterations_used"] == 3
    assert len(executor.executed_decisions) == 2
    assert executor.executed_decisions[0].action == LoopAction.CONTINUE
    assert executor.executed_decisions[1].action == LoopAction.PROMOTE

    assert len(result.trace) == 3
    assert result.trace[0].action == LoopAction.CONTINUE
    assert result.trace[1].action == LoopAction.PROMOTE
    assert result.trace[2].action == LoopAction.COMPLETE
    assert result.final_state.current_target == "item_candidate_42"
