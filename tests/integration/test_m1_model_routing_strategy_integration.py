"""
Pruebas de Integración para Model Routing Strategy (Hito M.1).

Escenarios de Integración requeridos:
A. Simple task -> eligible route selected.
B. Required capability missing in cheap route -> capable route selected.
C. Preferred provider unavailable -> fallback.
D. High-criticality task -> inadequate route excluded.
E. No valid route -> NO_ROUTE/UNKNOWN explícito.
F. Routing decision connected to existing OmniRoute/gateway abstraction.
G. Same request replay -> deterministic result.
E2E: Mission/Agent task -> RoutingRequest -> RoutingPolicy -> available routes -> RoutingDecision -> existing inference boundary / mock provider.
"""

from decimal import Decimal
import json
from unittest.mock import MagicMock, patch
import pytest

from src.domain.mission.models import LoopDecision, LoopAction, LoopState, MissionStatus
from src.domain.model_routing.models import (
    RoutingDecisionStatus,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RouteCapability,
    RouteStatus,
    RouteExclusionReason,
    ModelRoute,
    RoutingRequest,
    RoutingPolicy,
    RoutingDecision,
)
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry
from src.infrastructure.llm.config import OmniRouteConfig
from src.infrastructure.llm.omniroute_decision_provider import OmniRouteDecisionProvider


def test_scenario_a_simple_task_eligible_route_selected():
    """Escenario A: Tarea simple selecciona la ruta elegible con mayor prioridad."""
    registry = InMemoryModelRouteRegistry([
        ModelRoute(
            route_id="route-omni-default",
            provider="omniroute",
            model_id="auto/best-coding",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
            quality_class=QualityRequirement.STANDARD,
            priority=1,
        ),
        ModelRoute(
            route_id="route-openai-fallback",
            provider="openai",
            model_id="gpt-4o",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.TOOL_USE),
            quality_class=QualityRequirement.HIGH,
            priority=10,
        ),
    ])
    strategy = DeterministicModelRoutingStrategy(registry=registry)
    req = RoutingRequest(
        task_type="simple_query",
        required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
    )

    decision = strategy.route(req)

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-omni-default"
    assert decision.selected_route.model_id == "auto/best-coding"
    assert len(decision.eligible_routes) == 2


def test_scenario_b_capability_missing_in_cheap_route_picks_capable():
    """Escenario B: Ruta económica carece de capacidad requerida -> selecciona la ruta capaz."""
    cheap_no_tools = ModelRoute(
        route_id="route-cheap-basic",
        provider="provider-a",
        model_id="basic-llm",
        capabilities=(RouteCapability.JSON_MODE,),
        priority=1,
    )
    expensive_with_tools = ModelRoute(
        route_id="route-pro-tools",
        provider="provider-b",
        model_id="advanced-llm",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.JSON_MODE),
        priority=10,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="agent_tool_execution",
        required_capabilities=(RouteCapability.TOOL_USE,),
    )

    decision = strategy.route(req, available_routes=[cheap_no_tools, expensive_with_tools])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-pro-tools"
    assert any(e.route_id == "route-cheap-basic" and e.reason_code == RouteExclusionReason.MISSING_CAPABILITY for e in decision.excluded_routes)


def test_scenario_c_preferred_provider_unavailable_fallback():
    """Escenario C: Proveedor preferido no disponible -> fallback explícito a otra ruta elegible."""
    primary_route = ModelRoute(
        route_id="route-openai-4o",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        status=RouteStatus.UNAVAILABLE,
        priority=1,
    )
    backup_route = ModelRoute(
        route_id="route-omniroute-best",
        provider="omniroute",
        model_id="auto/best-coding",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        status=RouteStatus.AVAILABLE,
        priority=2,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="supplier_negotiation",
        required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        preferred_providers=("openai",),
    )

    decision = strategy.route(req, available_routes=[primary_route, backup_route])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-omniroute-best"
    assert decision.fallback_applied is True
    assert any(e.route_id == "route-openai-4o" and e.reason_code == RouteExclusionReason.UNAVAILABLE for e in decision.excluded_routes)


def test_scenario_d_high_criticality_inadequate_route_excluded():
    """Escenario D: Tarea de alta criticidad descarta rutas con calidad insuficiente."""
    std_route = ModelRoute(
        route_id="route-light-mini",
        provider="omniroute",
        model_id="gpt-4o-mini",
        quality_class=QualityRequirement.STANDARD,
        priority=1,
    )
    superior_route = ModelRoute(
        route_id="route-deep-reasoning",
        provider="openai",
        model_id="o1",
        quality_class=QualityRequirement.SUPERIOR,
        priority=10,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="governance_policy_override",
        criticality=TaskCriticality.CRITICAL,
    )

    decision = strategy.route(req, available_routes=[std_route, superior_route])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-deep-reasoning"
    assert any(e.route_id == "route-light-mini" and e.reason_code == RouteExclusionReason.INSUFFICIENT_QUALITY for e in decision.excluded_routes)


def test_scenario_e_no_valid_route_explicit_no_route():
    """Escenario E: Ausencia de rutas válidas produce NO_ROUTE explícito."""
    unavail_route = ModelRoute(
        route_id="route-dead-1",
        provider="omniroute",
        model_id="gpt-4o",
        status=RouteStatus.UNAVAILABLE,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(task_type="market_monitoring")

    decision = strategy.route(req, available_routes=[unavail_route])

    assert decision.status == RoutingDecisionStatus.NO_ROUTE
    assert decision.selected_route is None
    assert decision.is_selected is False
    assert len(decision.eligible_routes) == 0
    assert len(decision.excluded_routes) == 1


def test_scenario_f_routing_decision_connected_to_omniroute_provider():
    """Escenario F: Conexión de RoutingDecision con OmniRouteDecisionProvider sin alterar el gateway."""
    route = ModelRoute(
        route_id="route-omni-prod",
        provider="omniroute",
        model_id="auto/best-coding",
        capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        status=RouteStatus.AVAILABLE,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="loop_decision",
        required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
    )
    decision = strategy.route(req, available_routes=[route])
    assert decision.status == RoutingDecisionStatus.SELECTED

    provider = OmniRouteDecisionProvider(
        config=OmniRouteConfig(base_url="http://localhost:20128/v1"),
        route=decision,
    )

    assert provider.config.model == "auto/best-coding"
    assert provider.active_route.route_id == "route-omni-prod"
    assert provider.active_route.provider == "omniroute"


def test_scenario_g_same_request_replay_deterministic():
    """Escenario G: Repetición del mismo request frente a catálogo persistido produce decisión determinista idéntica."""
    registry = InMemoryModelRouteRegistry([
        ModelRoute(route_id="r1", provider="p1", model_id="m1", quality_class=QualityRequirement.STANDARD, priority=5),
        ModelRoute(route_id="r2", provider="p2", model_id="m2", quality_class=QualityRequirement.HIGH, priority=2),
        ModelRoute(route_id="r3", provider="p3", model_id="m3", quality_class=QualityRequirement.STANDARD, priority=2),
    ])
    strategy = DeterministicModelRoutingStrategy(registry=registry)
    req = RoutingRequest(task_type="market_analysis", min_quality=QualityRequirement.STANDARD)
    policy = RoutingPolicy(policy_id="replay_policy", version="1.0.0")

    runs = [strategy.route(req, policy=policy) for _ in range(10)]

    first_checksum = runs[0].calculate_checksum()
    first_selected_id = runs[0].selected_route.route_id

    for r in runs[1:]:
        assert r.calculate_checksum() == first_checksum
        assert r.selected_route.route_id == first_selected_id


def test_m1_e2e_mission_to_routing_to_inference_boundary():
    """
    E2E M.1: Flujo completo desde una tarea de misión de agente, generación de RoutingRequest,
    evaluación por RoutingPolicy y DeterministicModelRoutingStrategy,
    entrega de la RoutingDecision a OmniRouteDecisionProvider y ejecución simulada de inferencia.
    """
    # 1. Definición del catálogo de rutas
    fast_route = ModelRoute(
        route_id="route-fast-ml",
        provider="omniroute",
        model_id="gpt-4o-mini",
        capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
        quality_class=QualityRequirement.STANDARD,
        latency_class=LatencyRequirement.LOW_LATENCY,
        priority=1,
    )
    registry = InMemoryModelRouteRegistry([fast_route])
    strategy = DeterministicModelRoutingStrategy(registry=registry)

    # 2. Solicitud de inferencia de un agente en el loop
    req = RoutingRequest(
        task_type="product_discovery_step",
        complexity="STANDARD",
        criticality=TaskCriticality.MEDIUM,
        required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        min_quality=QualityRequirement.STANDARD,
    )

    # 3. Estrategia evalúa y produce RoutingDecision
    decision = strategy.route(req)
    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.model_id == "gpt-4o-mini"

    # 4. Inyección en el DecisionProvider de inferencia
    provider = OmniRouteDecisionProvider(
        config=OmniRouteConfig(base_url="http://localhost:20128/v1"),
        route=decision,
    )

    # 5. Llamada de inferencia hacia el boundary (mocked sin llamadas destructivas)
    mock_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "action": "CONTINUE",
                        "reason": "Suficiente evidencia preliminar recopilada",
                        "target": "auriculares-bluetooth-pro",
                        "parameters": {"query": "auriculares bluetooth"},
                        "confidence": 0.95
                    })
                }
            }
        ]
    }

    state = LoopState(
        mission_id="mission-e2e-m1",
        iteration=1,
        goal="Discover winners in ML Chile",
        current_target="auriculares bluetooth",
    )

    with patch.object(provider, "_call_chat_completion", return_value=mock_payload) as mock_call:
        loop_decision = provider.decide(state)

        assert mock_call.called
        assert loop_decision.action == LoopAction.CONTINUE
        assert loop_decision.parameters["query"] == "auriculares bluetooth"
        assert loop_decision.confidence == 0.95

    # Demuestra que downstream conoce exactamente el provider/model/route elegido
    assert provider.active_route.route_id == "route-fast-ml"
    assert provider.active_route.model_id == "gpt-4o-mini"
    assert provider.active_route.provider == "omniroute"
