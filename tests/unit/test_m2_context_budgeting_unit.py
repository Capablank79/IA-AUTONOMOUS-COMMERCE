"""
Pruebas Unitarias para Context Budgeting (Hito M.2).

Transversal M — Control de Coste e Inferencia.

Cubre exhaustivamente los 14 requerimientos obligatorios:
1. within budget
2. exact boundary (requested_input == available_input)
3. over budget (requested_input > available_input)
4. output reservation (espacio de salida protegido)
5. safety margin (margen de seguridad protegido)
6. negative/invalid values rejected
7. unknown context window (UNKNOWN preserved, no safe)
8. unknown token estimate (UNKNOWN preserved, no safe)
9. deterministic estimation (TokenEstimator determinista y desglosado)
10. M.1 route integration (ModelRoute y RoutingDecision M.1)
11. policy versioning (ContextBudgetPolicy versionada)
12. no truncation (OVER_BUDGET no trunca texto ni descarta datos)
13. no compression (no modifica prompts ni comprime)
14. no M.3–M.6 logic (sin prompt compression, caching, selector económico, policy M.6)
"""

import pytest
from types import MappingProxyType

from src.domain.context_budget.models import (
    ContextBudgetStatus,
    BudgetExclusionReason,
    InputTokensBreakdown,
    ContextBudgetPolicy,
    ContextBudgetRequest,
    ContextBudgetDecision,
)
from src.domain.model_routing.models import (
    ModelRoute,
    RouteCapability,
    RouteStatus,
    QualityRequirement,
    LatencyRequirement,
    RoutingDecision,
    RoutingDecisionStatus,
    sanitize_routing_data,
)
from src.application.context_budget.context_budget_service import ContextBudgetService
from src.application.context_budget.token_estimator import DeterministicTokenEstimator
from src.application.model_routing.registry import InMemoryModelRouteRegistry


def _create_sample_route(route_id="route-std", context_window=8192) -> ModelRoute:
    return ModelRoute(
        route_id=route_id,
        provider="omniroute",
        model_id="gpt-4o-mini",
        context_window=context_window,
        capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        quality_class=QualityRequirement.STANDARD,
        latency_class=LatencyRequirement.NORMAL,
        priority=1,
    )


def test_1_within_budget():
    """1. Caso dentro del presupuesto de contexto (requested_input < available_input)."""
    route = _create_sample_route(context_window=8000)
    service = ContextBudgetService()
    # available = 8000 - 1024 - 256 = 6720
    req = ContextBudgetRequest(
        route=route,
        requested_input_tokens=3000,
        reserved_output_tokens=1024,
        safety_margin_tokens=256,
    )
    decision = service.assess_budget(req)

    assert decision.status == ContextBudgetStatus.WITHIN_BUDGET
    assert decision.is_within_budget is True
    assert decision.context_window == 8000
    assert decision.requested_input_tokens == 3000
    assert decision.reserved_output_tokens == 1024
    assert decision.safety_margin_tokens == 256
    assert decision.available_input_tokens == 6720
    assert decision.estimated_total_tokens == 4280
    assert decision.reason_code is None
    assert "within budget" in decision.rationale.lower()


def test_2_exact_boundary():
    """2. Caso límite exacto (requested_input == available_input) -> WITHIN_BUDGET."""
    route = _create_sample_route(context_window=4000)
    service = ContextBudgetService()
    # available = 4000 - 1000 - 500 = 2500
    req = ContextBudgetRequest(
        route=route,
        requested_input_tokens=2500,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
    )
    decision = service.assess_budget(req)

    assert decision.status == ContextBudgetStatus.WITHIN_BUDGET
    assert decision.available_input_tokens == 2500
    assert decision.requested_input_tokens == 2500
    assert decision.is_within_budget is True


def test_3_over_budget():
    """3. Caso que excede el presupuesto (requested_input > available_input) -> OVER_BUDGET."""
    route = _create_sample_route(context_window=4000)
    service = ContextBudgetService()
    # available = 4000 - 1000 - 500 = 2500
    req = ContextBudgetRequest(
        route=route,
        requested_input_tokens=2501,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
    )
    decision = service.assess_budget(req)

    assert decision.status == ContextBudgetStatus.OVER_BUDGET
    assert decision.is_within_budget is False
    assert decision.reason_code == BudgetExclusionReason.INPUT_TOO_LARGE
    assert decision.available_input_tokens == 2500
    assert decision.requested_input_tokens == 2501


def test_4_output_reservation_protection():
    """4. Espacio de salida protegido: si reserved_output + safety > context_window -> OVER_BUDGET."""
    route = _create_sample_route(context_window=2000)
    service = ContextBudgetService()
    # 1800 + 400 = 2200 > 2000 -> available_input = -200
    req = ContextBudgetRequest(
        route=route,
        requested_input_tokens=100,
        reserved_output_tokens=1800,
        safety_margin_tokens=400,
    )
    decision = service.assess_budget(req)

    assert decision.status == ContextBudgetStatus.OVER_BUDGET
    assert decision.is_within_budget is False
    assert decision.reason_code == BudgetExclusionReason.OUTPUT_RESERVATION_EXCEEDED
    assert decision.available_input_tokens == -200


def test_5_safety_margin_protection():
    """5. Margen de seguridad reduce available_input adecuadamente."""
    route = _create_sample_route(context_window=10000)
    service = ContextBudgetService()
    # With safety margin 1000: available = 10000 - 1000 - 1000 = 8000
    req = ContextBudgetRequest(
        route=route,
        requested_input_tokens=8500,
        reserved_output_tokens=1000,
        safety_margin_tokens=1000,
    )
    decision = service.assess_budget(req)
    assert decision.status == ContextBudgetStatus.OVER_BUDGET
    assert decision.available_input_tokens == 8000


def test_6_negative_and_invalid_values_rejected():
    """6. Rechazo estricto de valores negativos o tipos inválidos (no float ni strings arbitrarios en tokens)."""
    # ModelRoute rechaza context_window negativo o no entero
    with pytest.raises(ValueError):
        ModelRoute(
            route_id="invalid-route",
            provider="omniroute",
            model_id="gpt-4o",
            context_window=-500,
        )

    with pytest.raises(ValueError):
        ModelRoute(
            route_id="invalid-route-float",
            provider="omniroute",
            model_id="gpt-4o",
            context_window=4096.5,  # float prohibido
        )

    # ContextBudgetRequest rechaza tokens negativos o floats
    with pytest.raises(ValueError):
        ContextBudgetRequest(
            route="some-route",
            requested_input_tokens=-10,
        )

    with pytest.raises(ValueError):
        ContextBudgetRequest(
            route="some-route",
            requested_input_tokens=100.5,  # float prohibido
        )

    with pytest.raises(ValueError):
        InputTokensBreakdown(system_instructions=-5)


def test_7_unknown_context_window():
    """7. Context window desconocido produce UNKNOWN, nunca WITHIN_BUDGET (UNKNOWN != safe)."""
    route_no_window = ModelRoute(
        route_id="route-unknown-window",
        provider="omniroute",
        model_id="legacy-llm",
        context_window=None,
    )
    service = ContextBudgetService()
    req = ContextBudgetRequest(
        route=route_no_window,
        requested_input_tokens=500,
    )
    decision = service.assess_budget(req)

    assert decision.status == ContextBudgetStatus.UNKNOWN
    assert decision.is_within_budget is False
    assert decision.reason_code == BudgetExclusionReason.MODEL_CONTEXT_UNKNOWN
    assert decision.available_input_tokens is None


def test_8_unknown_token_estimate():
    """8. Conteo de tokens desconocido (sin requested_input ni breakdown) produce UNKNOWN."""
    route = _create_sample_route(context_window=8192)
    service = ContextBudgetService()
    req = ContextBudgetRequest(
        route=route,
        requested_input_tokens=None,
        input_breakdown=None,
    )
    decision = service.assess_budget(req)

    assert decision.status == ContextBudgetStatus.UNKNOWN
    assert decision.is_within_budget is False
    assert decision.reason_code == BudgetExclusionReason.TOKEN_ESTIMATE_UNKNOWN
    assert decision.requested_input_tokens is None


def test_9_deterministic_estimation_and_breakdown():
    """9. Estimador determinista calcula desglose y produce enteros reproducibles."""
    estimator = DeterministicTokenEstimator(chars_per_token=4.0)

    # Estimación de texto
    count1 = estimator.estimate_text_tokens("Hello world! Autonomous commerce testing.")
    count2 = estimator.estimate_text_tokens("Hello world! Autonomous commerce testing.")
    assert count1 == count2
    assert isinstance(count1, int)
    assert count1 > 0

    # Desglose de componentes
    breakdown = estimator.estimate_breakdown(
        system_instructions="You are an autonomous assistant.",
        user_input="Analyze this market opportunity.",
        memory_context="Previous decision: CONTINUE.",
        tool_schemas=[{"name": "search_market", "description": "finds items"}],
        retrieved_evidence=[{"item_id": "ML123", "price": 100}],
        conversation_history=[{"role": "user", "content": "hello"}],
        other={"runtime_flag": True},
    )

    assert isinstance(breakdown, InputTokensBreakdown)
    assert breakdown.system_instructions > 0
    assert breakdown.user_input > 0
    assert breakdown.memory_context > 0
    assert breakdown.tool_schemas > 0
    assert breakdown.retrieved_evidence > 0
    assert breakdown.conversation_history > 0
    assert breakdown.other > 0
    assert breakdown.total_input_tokens == (
        breakdown.system_instructions
        + breakdown.user_input
        + breakdown.memory_context
        + breakdown.tool_schemas
        + breakdown.retrieved_evidence
        + breakdown.conversation_history
        + breakdown.other
    )

    # Servicio con breakdown
    service = ContextBudgetService()
    route = _create_sample_route(context_window=8192)
    req = ContextBudgetRequest(
        route=route,
        input_breakdown=breakdown,
    )
    decision = service.assess_budget(req)
    assert decision.status == ContextBudgetStatus.WITHIN_BUDGET
    assert decision.requested_input_tokens == breakdown.total_input_tokens


def test_10_m1_route_integration():
    """10. Integración con M.1 ModelRoute, RoutingDecision y ModelRouteRegistryPort."""
    route = ModelRoute(
        route_id="route-m1-gpt4",
        provider="openai",
        model_id="gpt-4o",
        context_window=128000,
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
    )
    registry = InMemoryModelRouteRegistry([route])
    service = ContextBudgetService(route_registry=registry)

    # 10.a Directamente con ModelRoute
    decision1 = service.assess_budget(ContextBudgetRequest(route=route, requested_input_tokens=10000))
    assert decision1.status == ContextBudgetStatus.WITHIN_BUDGET
    assert decision1.route_id == "route-m1-gpt4"

    # 10.b Con RoutingDecision M.1
    m1_decision = RoutingDecision(
        status=RoutingDecisionStatus.SELECTED,
        selected_route=route,
        eligible_routes=(route,),
        deterministic_rationale="Selected highest priority route",
    )
    decision2 = service.assess_budget(ContextBudgetRequest(route=m1_decision, requested_input_tokens=10000))
    assert decision2.status == ContextBudgetStatus.WITHIN_BUDGET
    assert decision2.route_id == "route-m1-gpt4"

    # 10.c Con route_id string desde registry
    decision3 = service.assess_budget(ContextBudgetRequest(route="route-m1-gpt4", requested_input_tokens=10000))
    assert decision3.status == ContextBudgetStatus.WITHIN_BUDGET
    assert decision3.route_id == "route-m1-gpt4"

    # 10.d Con RoutingDecision NO_ROUTE -> UNKNOWN / MODEL_CONTEXT_UNKNOWN
    m1_no_route = RoutingDecision(
        status=RoutingDecisionStatus.NO_ROUTE,
        selected_route=None,
    )
    decision4 = service.assess_budget(ContextBudgetRequest(route=m1_no_route, requested_input_tokens=10000))
    assert decision4.status == ContextBudgetStatus.UNKNOWN
    assert decision4.reason_code == BudgetExclusionReason.MODEL_CONTEXT_UNKNOWN


def test_11_policy_versioning():
    """11. Versionado de políticas y trazabilidad de checksum."""
    policy_v1 = ContextBudgetPolicy(
        policy_id="strict_safety_policy",
        version="1.2.0",
        default_reserved_output_tokens=2048,
        safety_margin_tokens=512,
    )
    route = _create_sample_route(context_window=10000)
    service = ContextBudgetService(default_policy=policy_v1)

    req = ContextBudgetRequest(route=route, requested_input_tokens=5000)
    decision = service.assess_budget(req, policy=policy_v1)

    assert decision.policy_id == "strict_safety_policy"
    assert decision.policy_version == "1.2.0"
    assert decision.reserved_output_tokens == 2048
    assert decision.safety_margin_tokens == 512
    # available = 10000 - 2048 - 512 = 7440
    assert decision.available_input_tokens == 7440

    # Checksum canónico
    checksum1 = decision.calculate_checksum()
    checksum2 = decision.calculate_checksum()
    assert checksum1 == checksum2
    assert len(checksum1) == 64


def test_12_no_truncation():
    """12. OVER_BUDGET no trunca silenciosamente ningún texto ni componente."""
    route = _create_sample_route(context_window=2000)
    service = ContextBudgetService()
    breakdown = InputTokensBreakdown(
        system_instructions=500,
        user_input=1000,
        retrieved_evidence=1000,
    )
    req = ContextBudgetRequest(
        route=route,
        input_breakdown=breakdown,
        reserved_output_tokens=500,
        safety_margin_tokens=200,
    )
    # available = 2000 - 500 - 200 = 1300. requested = 2500.
    decision = service.assess_budget(req)

    assert decision.status == ContextBudgetStatus.OVER_BUDGET
    assert decision.requested_input_tokens == 2500
    # Los datos del breakdown se conservan intactos
    assert decision.input_breakdown == breakdown
    assert decision.input_breakdown.retrieved_evidence == 1000


def test_13_no_compression():
    """13. M.2 no ejecuta algoritmos de compresión ni modifica strings."""
    service = ContextBudgetService()
    # Verificamos que el servicio no exponga métodos de compresión ni altere inputs
    assert not hasattr(service, "compress_prompt")
    assert not hasattr(service, "summarize_context")


def test_14_no_m3_m6_logic():
    """14. M.2 no implementa caching (M.4), model selection by cost/task (M.5), ni cost policy (M.6)."""
    service = ContextBudgetService()
    assert not hasattr(service, "cache_lookup")
    assert not hasattr(service, "get_cached_response")
    assert not hasattr(service, "select_cheapest_route")
    assert not hasattr(service, "enforce_economic_policy")
