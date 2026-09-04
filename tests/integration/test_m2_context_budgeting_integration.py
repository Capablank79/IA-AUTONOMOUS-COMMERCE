"""
Pruebas de Integración para Context Budgeting (Hito M.2).

Transversal M — Control de Coste e Inferencia.

Escenarios requeridos:
A. RoutingDecision M.1 -> route context window -> request within budget.
B. large context -> OVER_BUDGET.
C. same request with smaller-context route -> OVER_BUDGET.
D. same request with larger-context route -> WITHIN_BUDGET.
E. unknown route capacity -> UNKNOWN.
F. reserved output always protected.
G. decision handed to existing inference boundary/mock.
E2E: Mission/Agent input -> M.1 RoutingDecision -> M.2 ContextBudget -> inference boundary
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
    ModelRoute,
    RoutingRequest,
    RoutingPolicy,
    RoutingDecision,
)
from src.domain.context_budget.models import (
    ContextBudgetStatus,
    BudgetExclusionReason,
    InputTokensBreakdown,
    ContextBudgetPolicy,
    ContextBudgetRequest,
    ContextBudgetDecision,
)
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry
from src.application.context_budget.context_budget_service import ContextBudgetService
from src.application.context_budget.token_estimator import DeterministicTokenEstimator
from src.infrastructure.llm.config import OmniRouteConfig
from src.infrastructure.llm.omniroute_decision_provider import OmniRouteDecisionProvider
from src.infrastructure.llm.prompt import build_user_prompt, DECISION_SYSTEM_PROMPT


def test_scenario_a_routing_decision_to_budget_within():
    """Escenario A: M.1 selecciona ruta -> context_window obtenido -> M.2 evalúa within budget."""
    route_standard = ModelRoute(
        route_id="route-omni-standard",
        provider="omniroute",
        model_id="gpt-4o-mini",
        context_window=16384,
        capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        quality_class=QualityRequirement.STANDARD,
        priority=1,
    )
    registry = InMemoryModelRouteRegistry([route_standard])
    router = DeterministicModelRoutingStrategy(registry=registry)
    budget_service = ContextBudgetService(route_registry=registry)

    # 1. Routing M.1
    req_routing = RoutingRequest(task_type="market_analysis")
    routing_decision = router.route(req_routing)
    assert routing_decision.status == RoutingDecisionStatus.SELECTED

    # 2. Context Budgeting M.2
    req_budget = ContextBudgetRequest(
        route=routing_decision,
        requested_input_tokens=2000,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
    )
    budget_decision = budget_service.assess_budget(req_budget)

    assert budget_decision.status == ContextBudgetStatus.WITHIN_BUDGET
    assert budget_decision.is_within_budget is True
    assert budget_decision.context_window == 16384
    assert budget_decision.available_input_tokens == 14884  # 16384 - 1000 - 500


def test_scenario_b_large_context_over_budget():
    """Escenario B: Contexto excesivo -> OVER_BUDGET determinista con razón INPUT_TOO_LARGE."""
    route = ModelRoute(
        route_id="route-small",
        provider="omniroute",
        model_id="legacy-small-model",
        context_window=4096,
    )
    service = ContextBudgetService()
    # available = 4096 - 1024 - 256 = 2816
    req = ContextBudgetRequest(
        route=route,
        requested_input_tokens=3500,
    )
    decision = service.assess_budget(req)

    assert decision.status == ContextBudgetStatus.OVER_BUDGET
    assert decision.reason_code == BudgetExclusionReason.INPUT_TOO_LARGE
    assert decision.available_input_tokens == 2816
    assert decision.requested_input_tokens == 3500


def test_scenario_c_and_d_same_request_different_capacity_routes():
    """Escenarios C y D: Misma solicitud evaluada contra ruta pequeña (OVER) y ruta grande (WITHIN)."""
    small_route = ModelRoute(
        route_id="route-8k",
        provider="provider-a",
        model_id="model-8k",
        context_window=8192,
    )
    large_route = ModelRoute(
        route_id="route-128k",
        provider="provider-b",
        model_id="model-128k",
        context_window=128000,
    )
    service = ContextBudgetService()

    # Input grande de 10,000 tokens
    req_tokens = 10000

    # Escenario C: Small route -> OVER_BUDGET
    dec_small = service.assess_budget(ContextBudgetRequest(route=small_route, requested_input_tokens=req_tokens))
    assert dec_small.status == ContextBudgetStatus.OVER_BUDGET
    assert dec_small.reason_code == BudgetExclusionReason.INPUT_TOO_LARGE

    # Escenario D: Large route -> WITHIN_BUDGET
    dec_large = service.assess_budget(ContextBudgetRequest(route=large_route, requested_input_tokens=req_tokens))
    assert dec_large.status == ContextBudgetStatus.WITHIN_BUDGET
    assert dec_large.available_input_tokens == 128000 - 1024 - 256  # 126720


def test_scenario_e_unknown_route_capacity():
    """Escenario E: Ruta sin context_window configurado produce UNKNOWN y no safe."""
    route_no_window = ModelRoute(
        route_id="route-no-window",
        provider="omniroute",
        model_id="custom-llm",
        context_window=None,
    )
    service = ContextBudgetService()
    decision = service.assess_budget(ContextBudgetRequest(route=route_no_window, requested_input_tokens=100))

    assert decision.status == ContextBudgetStatus.UNKNOWN
    assert decision.is_within_budget is False
    assert decision.reason_code == BudgetExclusionReason.MODEL_CONTEXT_UNKNOWN


def test_scenario_f_reserved_output_always_protected():
    """Escenario F: El espacio para output reservado y safety margin nunca se reduce para acomodar input."""
    route = ModelRoute(
        route_id="route-compact",
        provider="omniroute",
        model_id="compact-3k",
        context_window=3000,
    )
    policy = ContextBudgetPolicy(
        policy_id="high_output_policy",
        version="1.0.0",
        default_reserved_output_tokens=2000,
        safety_margin_tokens=500,
    )
    service = ContextBudgetService(default_policy=policy)

    # available_input = 3000 - 2000 - 500 = 500
    # Input de 600 excede available_input, impidiendo que el output se comprima a < 2000
    decision = service.assess_budget(ContextBudgetRequest(route=route, requested_input_tokens=600))
    assert decision.status == ContextBudgetStatus.OVER_BUDGET
    assert decision.reserved_output_tokens == 2000
    assert decision.safety_margin_tokens == 500
    assert decision.available_input_tokens == 500


def test_scenario_g_decision_handed_to_inference_boundary():
    """Escenario G: Decisión de presupuesto verificada antes de invocar la frontera de inferencia / mock."""
    route = ModelRoute(
        route_id="route-omni-prod",
        provider="omniroute",
        model_id="gpt-4o-mini",
        context_window=32000,
        capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
    )
    estimator = DeterministicTokenEstimator()
    service = ContextBudgetService()

    # Construir un estado real de LoopState
    state = LoopState(
        mission_id="mission-market-99",
        iteration=1,
        goal="Find high margin electronics",
        current_target="Bluetooth Headphones",
        observations=("Found 12 candidate suppliers",),
        evidences=({"supplier_id": "SUPP-01", "price": 15.0},),
    )
    user_prompt = build_user_prompt(state)

    # Medir desglose y tokens
    breakdown = estimator.estimate_breakdown(
        system_instructions=DECISION_SYSTEM_PROMPT,
        user_input=user_prompt,
    )
    budget_req = ContextBudgetRequest(route=route, input_breakdown=breakdown)
    budget_dec = service.assess_budget(budget_req)

    assert budget_dec.status == ContextBudgetStatus.WITHIN_BUDGET

    # Al estar WITHIN_BUDGET, la inferencia procede con OmniRouteDecisionProvider
    mock_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "action": "CONTINUE",
                        "reason": "Supplier matches margin criteria",
                        "target": "Bluetooth Headphones",
                        "parameters": {"stage": "quote_validation"},
                        "confidence": 0.95
                    })
                }
            }
        ]
    }
    with patch.object(OmniRouteDecisionProvider, "_call_chat_completion", return_value=mock_response):
        provider = OmniRouteDecisionProvider(route=route)
        loop_dec = provider.decide(state)
        assert loop_dec.action == LoopAction.CONTINUE
        assert loop_dec.confidence == 0.95


def test_e2e_mission_input_to_routing_to_budget_to_inference_boundary():
    """
    E2E M.2:
    Mission/Agent input -> M.1 RoutingDecision -> M.2 ContextBudget -> Inference boundary.
    Verifica los 2 casos canónicos:
    1. WITHIN_BUDGET -> can proceed to inference.
    2. OVER_BUDGET -> explicit stop / hand-off sin compresión automática.
    """
    route_standard = ModelRoute(
        route_id="route-e2e-standard",
        provider="omniroute",
        model_id="gpt-4o-mini",
        context_window=4096,
        capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        quality_class=QualityRequirement.STANDARD,
        priority=1,
    )
    registry = InMemoryModelRouteRegistry([route_standard])
    router = DeterministicModelRoutingStrategy(registry=registry)
    budget_service = ContextBudgetService(route_registry=registry)
    estimator = DeterministicTokenEstimator()

    # CASO 1: WITHIN_BUDGET
    state_normal = LoopState(
        mission_id="mission-e2e-1",
        iteration=1,
        goal="Price monitoring",
        current_target="ITEM-100",
    )
    routing_dec_1 = router.route(RoutingRequest(task_type="price_monitor"))
    assert routing_dec_1.status == RoutingDecisionStatus.SELECTED

    breakdown_1 = estimator.estimate_breakdown(
        system_instructions=DECISION_SYSTEM_PROMPT,
        user_input=build_user_prompt(state_normal),
    )
    budget_dec_1 = budget_service.assess_budget(
        ContextBudgetRequest(route=routing_dec_1, input_breakdown=breakdown_1)
    )
    assert budget_dec_1.status == ContextBudgetStatus.WITHIN_BUDGET

    # Procede a inferencia
    mock_resp_1 = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "action": "PROMOTE",
                    "reason": "Optimal price opportunity detected",
                    "target": "ITEM-100",
                    "parameters": {},
                    "confidence": 0.9
                })
            }
        }]
    }
    with patch.object(OmniRouteDecisionProvider, "_call_chat_completion", return_value=mock_resp_1):
        provider = OmniRouteDecisionProvider(route=routing_dec_1.selected_route)
        decision = provider.decide(state_normal)
        assert decision.action == LoopAction.PROMOTE

    # CASO 2: OVER_BUDGET
    # Creamos un estado con un historial masivo que desborda el context_window de 4096
    large_observations = tuple(f"Observation log entry {i}: details about market flux" for i in range(500))
    state_massive = LoopState(
        mission_id="mission-e2e-massive",
        iteration=50,
        goal="Analyze large market segment",
        observations=large_observations,
    )
    breakdown_massive = estimator.estimate_breakdown(
        system_instructions=DECISION_SYSTEM_PROMPT,
        user_input=build_user_prompt(state_massive),
    )
    budget_dec_massive = budget_service.assess_budget(
        ContextBudgetRequest(route=routing_dec_1, input_breakdown=breakdown_massive)
    )

    assert budget_dec_massive.status == ContextBudgetStatus.OVER_BUDGET
    assert budget_dec_massive.is_within_budget is False
    assert budget_dec_massive.reason_code == BudgetExclusionReason.INPUT_TOO_LARGE

    # Control de flujo: NO se llama a la inferencia con payload desbordado; parada explícita
    # Sin compresión automática (eso corresponde a M.3)
    can_proceed = budget_dec_massive.is_within_budget
    assert can_proceed is False
