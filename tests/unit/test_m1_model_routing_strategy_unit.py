"""
Pruebas Unitarias para Model Routing Strategy (Hito M.1).

Cubre los 14 requerimientos obligatorios:
1. Eligible route selection
2. Capability mismatch excluded
3. Deterministic selection
4. Unavailable route excluded
5. Explicit fallback
6. No eligible route (NO_ROUTE)
7. UNKNOWN preserved (uncertain health excluded)
8. Criticality requirement
9. Latency constraint
10. Cost metadata handling (sin M.2/M.6)
11. Tie semantics (desempate determinista lexicográfico)
12. Policy versioning
13. Secret sanitization
14. No M.2–M.6 logic (no context budgeting, caching, prompt compression, full cost optimization)
"""

from decimal import Decimal
import pytest

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
    ExclusionRecord,
    RoutingPolicy,
    RoutingDecision,
    sanitize_routing_data,
)
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry


def test_1_eligible_route_selection():
    """1. Caso de ruta elegible básica seleccionada correctamente."""
    route1 = ModelRoute(
        route_id="route-fast-1",
        provider="omniroute",
        model_id="gpt-4o-mini",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        quality_class=QualityRequirement.STANDARD,
        latency_class=LatencyRequirement.NORMAL,
        priority=10,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="market_analysis",
        required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        min_quality=QualityRequirement.STANDARD,
    )

    decision = strategy.route(req, available_routes=[route1])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.is_selected is True
    assert decision.selected_route is not None
    assert decision.selected_route.route_id == "route-fast-1"
    assert len(decision.eligible_routes) == 1
    assert len(decision.excluded_routes) == 0
    assert "Selected route 'route-fast-1'" in decision.deterministic_rationale


def test_2_capability_mismatch_excluded():
    """2. Exclusión estricta de rutas que carecen de las capacidades requeridas."""
    route_no_tools = ModelRoute(
        route_id="route-text-only",
        provider="openai",
        model_id="gpt-3.5-turbo",
        capabilities=(RouteCapability.JSON_MODE,),
        quality_class=QualityRequirement.STANDARD,
    )
    route_with_tools = ModelRoute(
        route_id="route-with-tools",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.JSON_MODE),
        quality_class=QualityRequirement.HIGH,
    )

    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="supplier_quote",
        required_capabilities=(RouteCapability.TOOL_USE,),
    )

    decision = strategy.route(req, available_routes=[route_no_tools, route_with_tools])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-with-tools"
    assert len(decision.excluded_routes) == 1
    assert decision.excluded_routes[0].route_id == "route-text-only"
    assert decision.excluded_routes[0].reason_code == RouteExclusionReason.MISSING_CAPABILITY


def test_3_deterministic_selection():
    """3. Determinismo estricto: Mismo request + rutas + policy -> mismo checksum y selección exacta."""
    routes = [
        ModelRoute(
            route_id="route-b",
            provider="omniroute",
            model_id="gpt-4o-mini",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
            priority=20,
        ),
        ModelRoute(
            route_id="route-a",
            provider="omniroute",
            model_id="gpt-4o-mini",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
            priority=10,
        ),
    ]
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="opportunity_discovery",
        required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
    )
    policy = RoutingPolicy(policy_id="pol-det", version="1.0.0")

    d1 = strategy.route(req, available_routes=routes, policy=policy)
    d2 = strategy.route(req, available_routes=routes, policy=policy)

    assert d1.selected_route.route_id == "route-a"
    assert d2.selected_route.route_id == "route-a"
    assert d1.calculate_checksum() == d2.calculate_checksum()


def test_4_unavailable_route_excluded():
    """4. Exclusión de rutas marcadas como UNAVAILABLE o DEGRADED cuando la política no las admite."""
    route_unavail = ModelRoute(
        route_id="route-down",
        provider="anthropic",
        model_id="claude-3-5-sonnet",
        capabilities=(RouteCapability.TOOL_USE,),
        status=RouteStatus.UNAVAILABLE,
        priority=1,
    )
    route_avail = ModelRoute(
        route_id="route-up",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE,),
        status=RouteStatus.AVAILABLE,
        priority=10,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(task_type="deep_analysis", required_capabilities=(RouteCapability.TOOL_USE,))

    decision = strategy.route(req, available_routes=[route_unavail, route_avail])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-up"
    assert any(e.route_id == "route-down" and e.reason_code == RouteExclusionReason.UNAVAILABLE for e in decision.excluded_routes)


def test_5_explicit_fallback():
    """5. Fallback explícito registrado cuando la ruta primaria/preferida falla o no está disponible."""
    route_primary = ModelRoute(
        route_id="route-primary",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE,),
        status=RouteStatus.UNAVAILABLE,
        priority=1,
    )
    route_secondary = ModelRoute(
        route_id="route-secondary",
        provider="anthropic",
        model_id="claude-3-5-sonnet",
        capabilities=(RouteCapability.TOOL_USE,),
        status=RouteStatus.AVAILABLE,
        priority=2,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="complex_negotiation",
        required_capabilities=(RouteCapability.TOOL_USE,),
        preferred_providers=("openai",),
    )

    decision = strategy.route(req, available_routes=[route_primary, route_secondary])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-secondary"
    assert decision.fallback_applied is True


def test_6_no_eligible_route():
    """6. Manejo explícito de NO_ROUTE cuando ninguna ruta cumple los requisitos."""
    route1 = ModelRoute(
        route_id="route-vision-only",
        provider="google",
        model_id="gemini-pro-vision",
        capabilities=(RouteCapability.VISION,),
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="reasoning_task",
        required_capabilities=(RouteCapability.REASONING, RouteCapability.TOOL_USE),
    )

    decision = strategy.route(req, available_routes=[route1])

    assert decision.status == RoutingDecisionStatus.NO_ROUTE
    assert decision.selected_route is None
    assert decision.is_selected is False
    assert len(decision.eligible_routes) == 0
    assert len(decision.excluded_routes) == 1
    assert decision.excluded_routes[0].reason_code == RouteExclusionReason.MISSING_CAPABILITY


def test_7_unknown_preserved():
    """7. Las rutas con estado UNKNOWN no se asumen operativas ni disponibles a ciegas."""
    route_unknown = ModelRoute(
        route_id="route-unknown-health",
        provider="custom-llm",
        model_id="local-model-v1",
        capabilities=(RouteCapability.TOOL_USE,),
        status=RouteStatus.UNKNOWN,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(task_type="action_decision", required_capabilities=(RouteCapability.TOOL_USE,))

    decision = strategy.route(req, available_routes=[route_unknown])

    assert decision.status == RoutingDecisionStatus.NO_ROUTE
    assert len(decision.excluded_routes) == 1
    assert decision.excluded_routes[0].reason_code == RouteExclusionReason.UNKNOWN_STATUS


def test_8_criticality_requirement():
    """8. Requerimiento de criticidad filtra rutas que no alcanzan el nivel de calidad exigido."""
    route_std = ModelRoute(
        route_id="route-std",
        provider="omniroute",
        model_id="gpt-4o-mini",
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.STANDARD,
        priority=1,
    )
    route_high = ModelRoute(
        route_id="route-high",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.HIGH,
        priority=5,
    )
    strategy = DeterministicModelRoutingStrategy()

    # Tarea CRITICAL requiere calidad SUPERIOR/HIGH
    req = RoutingRequest(
        task_type="capital_allocation_decision",
        criticality=TaskCriticality.HIGH,
        required_capabilities=(RouteCapability.TOOL_USE,),
    )

    decision = strategy.route(req, available_routes=[route_std, route_high])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-high"
    assert any(e.route_id == "route-std" and e.reason_code == RouteExclusionReason.INSUFFICIENT_QUALITY for e in decision.excluded_routes)


def test_9_latency_constraint():
    """9. Requerimiento de latencia excluye rutas más lentas que el umbral estipulado."""
    route_slow = ModelRoute(
        route_id="route-slow-batch",
        provider="openai",
        model_id="o1-preview",
        capabilities=(RouteCapability.REASONING,),
        latency_class=LatencyRequirement.NORMAL,
        quality_class=QualityRequirement.SUPERIOR,
    )
    route_fast = ModelRoute(
        route_id="route-fast-interactive",
        provider="openai",
        model_id="gpt-4o-mini",
        capabilities=(RouteCapability.REASONING,),
        latency_class=LatencyRequirement.LOW_LATENCY,
        quality_class=QualityRequirement.HIGH,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="realtime_interaction",
        max_latency=LatencyRequirement.LOW_LATENCY,
        required_capabilities=(RouteCapability.REASONING,),
    )

    decision = strategy.route(req, available_routes=[route_slow, route_fast])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-fast-interactive"
    assert any(e.route_id == "route-slow-batch" and e.reason_code == RouteExclusionReason.LATENCY_TOO_HIGH for e in decision.excluded_routes)


def test_10_cost_metadata_handling():
    """10. Consumo de metadatos de coste sin aplicar lógica de presupuesto ni optimización M.2/M.6."""
    route_cheap = ModelRoute(
        route_id="route-cheap",
        provider="omniroute",
        model_id="gpt-4o-mini",
        estimated_cost_input_per_million=Decimal("0.150"),
        estimated_cost_output_per_million=Decimal("0.600"),
        flat_cost_per_request=Decimal("0.001"),
    )
    route_expensive = ModelRoute(
        route_id="route-expensive",
        provider="openai",
        model_id="gpt-4o",
        estimated_cost_input_per_million=Decimal("2.500"),
        estimated_cost_output_per_million=Decimal("10.000"),
        flat_cost_per_request=Decimal("0.050"),
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(
        task_type="bounded_task",
        cost_ceiling_per_call=Decimal("0.010"),
    )

    decision = strategy.route(req, available_routes=[route_cheap, route_expensive])

    assert decision.status == RoutingDecisionStatus.SELECTED
    assert decision.selected_route.route_id == "route-cheap"
    assert any(e.route_id == "route-expensive" and e.reason_code == RouteExclusionReason.COST_CEILING_EXCEEDED for e in decision.excluded_routes)


def test_11_tie_semantics():
    """11. Semántica determinista de desempate: ante igualdad de prioridad y calidad, orden lexicográfico por route_id."""
    r_z = ModelRoute(
        route_id="route-z",
        provider="omniroute",
        model_id="model-z",
        priority=10,
        quality_class=QualityRequirement.HIGH,
    )
    r_a = ModelRoute(
        route_id="route-a",
        provider="omniroute",
        model_id="model-a",
        priority=10,
        quality_class=QualityRequirement.HIGH,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(task_type="general")

    # Invertir orden de entrada intencionalmente
    decision1 = strategy.route(req, available_routes=[r_z, r_a])
    decision2 = strategy.route(req, available_routes=[r_a, r_z])

    assert decision1.selected_route.route_id == "route-a"
    assert decision2.selected_route.route_id == "route-a"
    assert decision1.calculate_checksum() == decision2.calculate_checksum()


def test_12_policy_versioning():
    """12. Soporte para versionado y personalización declarativa de RoutingPolicy."""
    policy_v1 = RoutingPolicy(policy_id="strict-pol", version="1.0.0", strict_criticality_filter=True)
    policy_v2 = RoutingPolicy(policy_id="relaxed-pol", version="2.0.0", strict_criticality_filter=False)

    route_std = ModelRoute(
        route_id="route-std",
        provider="omniroute",
        model_id="gpt-4o-mini",
        quality_class=QualityRequirement.STANDARD,
    )
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(task_type="capital", criticality=TaskCriticality.HIGH)

    dec_v1 = strategy.route(req, available_routes=[route_std], policy=policy_v1)
    dec_v2 = strategy.route(req, available_routes=[route_std], policy=policy_v2)

    assert dec_v1.status == RoutingDecisionStatus.NO_ROUTE
    assert dec_v1.policy_version == "1.0.0"

    assert dec_v2.status == RoutingDecisionStatus.SELECTED
    assert dec_v2.policy_version == "2.0.0"


def test_13_secret_sanitization():
    """13. Sanitización estricta de secretos, API keys y CoT en metadatos y requests."""
    route = ModelRoute(
        route_id="route-safe",
        provider="omniroute",
        model_id="gpt-4o",
        metadata={
            "api_key": "sk-secret-123456",
            "authorization": "Bearer super-secret",
            "token": "tok_abcdef",
            "chain_of_thought": "secret reasoning steps",
            "safe_tag": "production-tier-1",
        },
    )
    req = RoutingRequest(
        task_type="audit",
        context_metadata={
            "password": "my_password",
            "secret_key": "raw_secret",
            "valid_key": "valid_value",
        },
    )

    assert route.metadata["api_key"] == "[REDACTED]"
    assert route.metadata["authorization"] == "[REDACTED]"
    assert route.metadata["token"] == "[REDACTED]"
    assert route.metadata["chain_of_thought"] == "[REDACTED]"
    assert route.metadata["safe_tag"] == "production-tier-1"

    assert req.context_metadata["password"] == "[REDACTED]"
    assert req.context_metadata["secret_key"] == "[REDACTED]"
    assert req.context_metadata["valid_key"] == "valid_value"


def test_14_no_m2_to_m6_logic():
    """14. Verificación de límites de diseño: M.1 no implementa M.2–M.6 (no cache, no prompt compression, no budget optimization)."""
    # M.1 es puramente una estrategia determinista de selección de ruta basada en metadatos declarativos.
    strategy = DeterministicModelRoutingStrategy()
    req = RoutingRequest(task_type="opportunity_discovery")

    assert not hasattr(strategy, "optimize_budget")
    assert not hasattr(strategy, "compress_prompt")
    assert not hasattr(strategy, "get_cached_response")
    assert not hasattr(strategy, "execute_cost_policy")
