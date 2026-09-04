import json
import socket
from typing import Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.domain.mission.models import LoopState, LoopDecision
from src.domain.mission.ports import DecisionProvider
from src.domain.model_routing.models import ModelRoute, RoutingDecision
from src.infrastructure.llm.config import OmniRouteConfig
from src.infrastructure.llm.exceptions import (
    OmniRouteConnectionError,
    OmniRouteContractValidationError,
    OmniRouteHttpError,
    OmniRouteParseError,
    OmniRouteTimeoutError,
)
from src.infrastructure.llm.prompt import DECISION_SYSTEM_PROMPT, build_user_prompt
from src.infrastructure.llm.validator import parse_and_validate_decision


class OmniRouteDecisionProvider(DecisionProvider):
    """
    Adaptador de infraestructura que implementa DecisionProvider conectándose
    a OmniRoute (o cualquier gateway OpenAI-compatible).
    """

    def __init__(
        self,
        config: Optional[OmniRouteConfig] = None,
        route: Optional[Union[ModelRoute, RoutingDecision]] = None,
    ):
        if route is not None:
            # Reusar o extraer la ruta seleccionada de M.1 sin alterar el gateway
            selected = route.selected_route if isinstance(route, RoutingDecision) else route
            if selected is not None:
                base_cfg = config or OmniRouteConfig()
                self.config = OmniRouteConfig(
                    base_url=base_cfg.base_url,
                    api_key=base_cfg.api_key,
                    model=selected.model_id,
                    timeout=base_cfg.timeout,
                )
                self.active_route: Optional[ModelRoute] = selected
            else:
                self.config = config or OmniRouteConfig()
                self.active_route = None
        else:
            self.config = config or OmniRouteConfig()
            self.active_route = None

    def decide(self, state: LoopState) -> LoopDecision:
        user_prompt = build_user_prompt(state)
        raw_response = self._call_chat_completion(user_prompt)
        content = self._extract_message_content(raw_response)
        return parse_and_validate_decision(content)

    def _call_chat_completion(self, user_content: str) -> dict:
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": DECISION_SYSTEM_PROMPT},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.0
        }

        data_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        req = Request(endpoint, data=data_bytes, headers=headers, method="POST")

        try:
            with urlopen(req, timeout=self.config.timeout) as resp:
                resp_bytes = resp.read()
                resp_text = resp_bytes.decode("utf-8")
        except HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise OmniRouteHttpError(
                f"OmniRoute HTTP request failed with status {exc.code}: {exc.reason}",
                status_code=exc.code,
                response_body=err_body
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise OmniRouteTimeoutError(f"OmniRoute request timed out after {self.config.timeout}s: {str(exc)}") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise OmniRouteTimeoutError(f"OmniRoute request timed out: {str(exc.reason)}") from exc
            raise OmniRouteConnectionError(f"Failed to connect to OmniRoute: {str(exc.reason)}") from exc
        except Exception as exc:
            raise OmniRouteConnectionError(f"Unexpected connection error when contacting OmniRoute: {str(exc)}") from exc

        if not resp_text or not resp_text.strip():
            raise OmniRouteParseError("OmniRoute returned an empty HTTP response")

        try:
            return json.loads(resp_text)
        except json.JSONDecodeError as exc:
            raise OmniRouteParseError(f"OmniRoute response is not valid JSON: {str(exc)}") from exc

    def _extract_message_content(self, response_json: dict) -> str:
        if not isinstance(response_json, dict):
            raise OmniRouteParseError(f"Expected JSON object in OmniRoute response, got {type(response_json).__name__}")

        choices = response_json.get("choices")
        if not choices or not isinstance(choices, list) or len(choices) == 0:
            raise OmniRouteParseError("OmniRoute response contains no 'choices' or empty choices list")

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise OmniRouteParseError("Choice element is not a JSON object")

        message = first_choice.get("message")
        if not message or not isinstance(message, dict):
            raise OmniRouteParseError("Choice contains no 'message' object")

        content = message.get("content")
        if content is None:
            raise OmniRouteParseError("Message contains no 'content'")

        if not isinstance(content, str):
            raise OmniRouteParseError(f"Message content must be string, got {type(content).__name__}")

        return content
