import json
import socket
from io import BytesIO
from urllib.error import HTTPError, URLError
from unittest.mock import patch, MagicMock

import pytest

from src.domain.mission.models import LoopAction, LoopDecision, LoopState
from src.infrastructure.llm.config import OmniRouteConfig
from src.infrastructure.llm.exceptions import (
    OmniRouteConnectionError,
    OmniRouteContractValidationError,
    OmniRouteHttpError,
    OmniRouteParseError,
    OmniRouteTimeoutError,
)
from src.infrastructure.llm.omniroute_decision_provider import OmniRouteDecisionProvider


def _create_sample_state(
    mission_id: str = "mission-123",
    iteration: int = 1,
    goal: str = "Find profitable suppliers for SSDs",
    current_target: str = "supplier_a",
    observations: tuple = ({"step": "search", "found": 5},),
    evidences: tuple = ("evidence_item_1",),
    decision_history: tuple = ()
) -> LoopState:
    return LoopState(
        mission_id=mission_id,
        iteration=iteration,
        goal=goal,
        current_target=current_target,
        observations=observations,
        evidences=evidences,
        decision_history=decision_history
    )


def _mock_http_response(content_dict: dict, status_code: int = 200):
    body_json = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 123456789,
        "model": "auto/best-coding",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content_dict)
                },
                "finish_reason": "stop"
            }
        ]
    }
    raw_bytes = json.dumps(body_json).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw_bytes
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    return mock_resp


def test_valid_response_to_loop_decision():
    """1. Respuesta válida -> LoopDecision válido."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "CONTINUE",
        "reason": "Still gathering supplier catalog",
        "target": "supplier_catalog",
        "parameters": {"depth": 1},
        "confidence": 0.95
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        decision = provider.decide(state)

    assert isinstance(decision, LoopDecision)
    assert decision.action == LoopAction.CONTINUE
    assert decision.reason == "Still gathering supplier catalog"
    assert decision.target == "supplier_catalog"
    assert decision.parameters == {"depth": 1}
    assert decision.confidence == 0.95


def test_action_continue():
    """2. CONTINUE."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "CONTINUE",
        "reason": "Proceed to next page",
        "target": "page_2",
        "parameters": {"page": 2},
        "confidence": 0.8
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        decision = provider.decide(state)

    assert decision.action == LoopAction.CONTINUE
    assert decision.target == "page_2"
    assert decision.confidence == 0.8


def test_action_pivot_with_target():
    """3. PIVOT con target."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "PIVOT",
        "reason": "Category saturated, pivoting to NVMe SSDs",
        "target": "nvme_category",
        "parameters": {"min_speed": 3500},
        "confidence": 0.9
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        decision = provider.decide(state)

    assert decision.action == LoopAction.PIVOT
    assert decision.reason == "Category saturated, pivoting to NVMe SSDs"
    assert decision.target == "nvme_category"
    assert decision.parameters == {"min_speed": 3500}


def test_action_reject():
    """4. REJECT."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "REJECT",
        "reason": "Margin below minimum threshold",
        "target": None,
        "parameters": {},
        "confidence": 0.99
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        decision = provider.decide(state)

    assert decision.action == LoopAction.REJECT
    assert decision.target is None
    assert decision.confidence == 0.99


def test_action_promote():
    """5. PROMOTE."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "PROMOTE",
        "reason": "High demand opportunity discovered",
        "target": "opp_ssd_sata",
        "parameters": {"roi": 0.45},
        "confidence": 0.92
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        decision = provider.decide(state)

    assert decision.action == LoopAction.PROMOTE
    assert decision.target == "opp_ssd_sata"
    assert decision.parameters == {"roi": 0.45}


def test_action_complete():
    """6. COMPLETE."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "COMPLETE",
        "reason": "Mission objectives fully satisfied",
        "target": None,
        "parameters": {"total_found": 10},
        "confidence": 1.0
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        decision = provider.decide(state)

    assert decision.action == LoopAction.COMPLETE
    assert decision.confidence == 1.0


def test_invalid_json():
    """7. JSON inválido."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    body_json = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "NOT A JSON OBJECT {action: broken"
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body_json).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = mock_resp
        with pytest.raises(OmniRouteContractValidationError) as exc_info:
            provider.decide(state)
        assert "Invalid JSON in LLM response" in str(exc_info.value)


def test_response_without_content():
    """8. Respuesta sin content."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    body_json = {
        "choices": [
            {
                "message": {
                    "role": "assistant"
                    # "content" missing
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body_json).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = mock_resp
        with pytest.raises(OmniRouteParseError) as exc_info:
            provider.decide(state)
        assert "Message contains no 'content'" in str(exc_info.value)


def test_unknown_action():
    """9. Action desconocida."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "EXPLODE_MISSION",
        "reason": "Invalid action test",
        "target": None
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        with pytest.raises(OmniRouteContractValidationError) as exc_info:
            provider.decide(state)
        assert "Unknown or invalid action 'EXPLODE_MISSION'" in str(exc_info.value)


def test_confidence_out_of_range():
    """10. Confidence fuera de rango."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "CONTINUE",
        "reason": "Confidence test",
        "confidence": 1.5
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        with pytest.raises(OmniRouteContractValidationError) as exc_info:
            provider.decide(state)
        assert "'confidence' must be between 0.0 and 1.0" in str(exc_info.value)

    # También probar confidence negativo
    llm_payload["confidence"] = -0.1
    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        with pytest.raises(OmniRouteContractValidationError) as exc_info:
            provider.decide(state)
        assert "'confidence' must be between 0.0 and 1.0" in str(exc_info.value)


def test_parameters_invalid_structure():
    """11. Parameters con estructura inválida."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    llm_payload = {
        "action": "CONTINUE",
        "reason": "Parameters invalid type",
        "parameters": ["not", "a", "dict"]
    }

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(llm_payload)
        with pytest.raises(OmniRouteContractValidationError) as exc_info:
            provider.decide(state)
        assert "'parameters' must be a dictionary/mapping" in str(exc_info.value)


def test_http_error():
    """12. HTTP error."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    err_fp = BytesIO(b'{"error": "Internal server error"}')
    http_error = HTTPError(
        url="http://localhost:20128/v1/chat/completions",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=err_fp
    )

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen", side_effect=http_error):
        with pytest.raises(OmniRouteHttpError) as exc_info:
            provider.decide(state)
        assert exc_info.value.status_code == 500
        assert "500" in str(exc_info.value)
        assert "Internal server error" in exc_info.value.response_body


def test_timeout():
    """13. Timeout."""
    provider = OmniRouteDecisionProvider(OmniRouteConfig(timeout=5.0))
    state = _create_sample_state()

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen", side_effect=socket.timeout("Connection timed out")):
        with pytest.raises(OmniRouteTimeoutError) as exc_info:
            provider.decide(state)
        assert "timed out" in str(exc_info.value).lower()


def test_connection_rejected():
    """14. Conexión rechazada."""
    provider = OmniRouteDecisionProvider()
    state = _create_sample_state()

    url_error = URLError(reason=ConnectionRefusedError("[WinError 10061] No connection could be made"))

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen", side_effect=url_error):
        with pytest.raises(OmniRouteConnectionError) as exc_info:
            provider.decide(state)
        assert "Failed to connect to OmniRoute" in str(exc_info.value)


def test_preservation_of_context_in_request():
    """15. Preservación del contexto relevante del LoopState en la solicitud."""
    config = OmniRouteConfig(
        base_url="http://custom-omniroute:9999/v1",
        api_key="sk-test-key-123",
        model="custom-model-v1",
        timeout=15.0
    )
    provider = OmniRouteDecisionProvider(config=config)

    previous_decision = LoopDecision(
        action=LoopAction.PIVOT,
        reason="Pivoting from A to B",
        target="category_B",
        parameters={"step": 1},
        confidence=0.85
    )
    state = _create_sample_state(
        mission_id="m-context-test-456",
        iteration=2,
        goal="Discover high yield products",
        current_target="target_item_5",
        observations=({"obs_1": "val_1"}, {"obs_2": "val_2"}),
        evidences=("evidence_alpha", "evidence_beta"),
        decision_history=(previous_decision,)
    )

    llm_payload = {
        "action": "COMPLETE",
        "reason": "Verified all contexts",
        "confidence": 0.99
    }

    captured_request = None

    def fake_urlopen(req, timeout=None):
        nonlocal captured_request
        captured_request = req
        return _mock_http_response(llm_payload)

    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen", side_effect=fake_urlopen):
        decision = provider.decide(state)

    assert decision.action == LoopAction.COMPLETE
    assert captured_request is not None
    assert captured_request.full_url == "http://custom-omniroute:9999/v1/chat/completions"
    assert captured_request.get_header("Authorization") == "Bearer sk-test-key-123"
    assert captured_request.get_header("Content-type") == "application/json"

    # Verificar el cuerpo enviado
    sent_body = json.loads(captured_request.data.decode("utf-8"))
    assert sent_body["model"] == "custom-model-v1"
    messages = sent_body["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"

    user_content = messages[1]["content"]
    assert "m-context-test-456" in user_content
    assert "Discover high yield products" in user_content
    assert "target_item_5" in user_content
    assert "obs_1" in user_content
    assert "obs_2" in user_content
    assert "evidence_alpha" in user_content
    assert "evidence_beta" in user_content
    assert "Pivoting from A to B" in user_content
    assert "category_B" in user_content
