import os
import urllib.request
import pytest
from src.domain.mission.models import LoopState, LoopAction, LoopDecision
from src.infrastructure.llm.config import OmniRouteConfig
from src.infrastructure.llm.omniroute_decision_provider import OmniRouteDecisionProvider


def is_omniroute_running(base_url: str) -> bool:
    """Comprueba si el endpoint de OmniRoute responde."""
    try:
        # Intenta consultar la URL base o /models
        url = f"{base_url.rstrip('/')}/models"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


DEFAULT_OMNIROUTE_URL = os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128/v1")
OMNIROUTE_AVAILABLE = is_omniroute_running(DEFAULT_OMNIROUTE_URL)


@pytest.mark.skipif(
    not OMNIROUTE_AVAILABLE,
    reason=f"OmniRoute gateway is not running at {DEFAULT_OMNIROUTE_URL}. Integration test skipped."
)
def test_omniroute_real_gateway_integration():
    """
    Test de integración real con OmniRoute.
    Flujo:
    LoopState -> OmniRouteDecisionProvider -> OmniRoute -> auto/best-coding -> LoopDecision
    """
    config = OmniRouteConfig.from_env()
    provider = OmniRouteDecisionProvider(config=config)

    state = LoopState(
        mission_id="mission-live-test",
        iteration=1,
        goal="Discover if gaming laptops in Mercado Libre have viable arbitrage margins",
        current_target="category_gaming_laptops",
        observations=(
            {
                "action_executed": "FETCH_CATEGORY_METRICS",
                "total_items": 45,
                "median_price": 850000,
                "average_demand": "HIGH"
            },
        ),
        evidences=(
            "Found 3 supplier quotes with average unit cost 520000 CLP",
        ),
        decision_history=()
    )

    decision = provider.decide(state)

    # Validaciones sobre el LoopDecision obtenido
    assert isinstance(decision, LoopDecision)
    assert decision.action in list(LoopAction)
    assert isinstance(decision.reason, str) and len(decision.reason) > 0
    if decision.target is not None:
        assert isinstance(decision.target, str)
    assert isinstance(decision.parameters, (dict, type(decision.parameters)))
    if decision.confidence is not None:
        assert 0.0 <= decision.confidence <= 1.0
