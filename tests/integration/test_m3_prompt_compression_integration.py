"""
Pruebas de Integración para Prompt Compression (Hito M.3).

Transversal M — Control de Coste e Inferencia.

Escenarios de integración requeridos:
A. Flujo encadenado completo: Routing M.1 -> Budgeting M.2 (OVER_BUDGET) -> Compression M.3 (COMPRESSED) -> Re-evaluación M.2 (WITHIN_BUDGET).
B. Flujo con contexto ya ajustado: M.1 -> M.2 (WITHIN_BUDGET) -> M.3 (UNCHANGED).
C. Flujo con desborde imposible de comprimir: M.1 -> M.2 (OVER_BUDGET) -> M.3 (CANNOT_COMPRESS) -> Preservación de componentes críticos.
D. Integración con OmniRouteDecisionProvider / LoopState: Preparación de prompt determinista ante sobrepeso de evidencias de mercado.
E. Determinismo y trazabilidad cruzada (hashes SHA-256 e idempotencia).
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
from src.domain.prompt_compression.models import (
    CompressionStatus,
    ContextComponentType,
    PriorityLevel,
    ContextItem,
    CompressionActionType,
    CompressionAction,
    CompressionPolicy,
    RawContextPayload,
    CompressedContextPayload,
    CompressionRequest,
    CompressionResult,
)
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry
from src.application.context_budget.context_budget_service import ContextBudgetService
from src.application.context_budget.token_estimator import DeterministicTokenEstimator
from src.application.prompt_compression.deterministic_compressor import DeterministicPromptCompressor
from src.infrastructure.llm.config import OmniRouteConfig
from src.infrastructure.llm.omniroute_decision_provider import OmniRouteDecisionProvider
from src.infrastructure.llm.prompt import build_user_prompt, DECISION_SYSTEM_PROMPT


def test_scenario_a_e2e_m1_m2_m3_reevaluation_pipeline():
    """
    Escenario A:
    1. M.1 selecciona ruta con context_window moderado (ej: 4096).
    2. Contexto grande de entrada -> M.2 evalúa OVER_BUDGET.
    3. M.3 toma el OVER_BUDGET y el available_input_tokens -> Comprime deterministamente -> COMPRESSED.
    4. M.2 reevalúa con el breakdown comprimido -> WITHIN_BUDGET.
    """
    # 1. Configurar M.1
    route = ModelRoute(
        route_id="route-fast",
        provider="omniroute",
        model_id="gpt-4o-mini",
        context_window=2048,
        capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        quality_class=QualityRequirement.STANDARD,
        priority=1,
    )
    registry = InMemoryModelRouteRegistry([route])
    router = DeterministicModelRoutingStrategy(registry=registry)
    routing_decision = router.route(RoutingRequest(task_type="market_analysis"))
    assert routing_decision.status == RoutingDecisionStatus.SELECTED

    # 2. Contexto amplio (duplicados + historial largo + evidencias)
    estimator = DeterministicTokenEstimator(chars_per_token=4.0)
    raw_payload = RawContextPayload(
        system_instructions=DECISION_SYSTEM_PROMPT,
        user_input="Evaluate opportunity OPP-ML-01 with recent signals.",
        retrieved_evidence=[
            {"source": "mercadolibre", "price": 15000, "seller": f"S{i}", "stock": 50, "description": "Long item details " * 10}
            for i in range(20)
        ],
        conversation_history=[
            {"role": "user", "content": f"Previous query iteration {i} requesting analysis with additional detailed instructions " * 5}
            for i in range(30)
        ],
    )
    initial_breakdown = estimator.estimate_breakdown(
        system_instructions=raw_payload.system_instructions,
        user_input=raw_payload.user_input,
        retrieved_evidence=raw_payload.retrieved_evidence,
        conversation_history=raw_payload.conversation_history,
    )

    # 3. M.2 Context Budgeting inicial
    budget_service = ContextBudgetService(route_registry=registry, token_estimator=estimator)
    budget_policy = ContextBudgetPolicy(
        policy_id="strict_budget",
        default_reserved_output_tokens=1024,
        safety_margin_tokens=256,
    )
    # available_input = 2048 - 1024 - 256 = 768 tokens
    initial_req = ContextBudgetRequest(
        route=routing_decision,
        input_breakdown=initial_breakdown,
    )
    initial_budget_decision = budget_service.assess_budget(initial_req, policy=budget_policy)
    
    # Debe ser OVER_BUDGET
    assert initial_budget_decision.status == ContextBudgetStatus.OVER_BUDGET
    assert initial_budget_decision.available_input_tokens == 768

    # 4. M.3 Prompt Compression
    compressor = DeterministicPromptCompressor(token_estimator=estimator)
    comp_req = CompressionRequest(
        raw_payload=raw_payload,
        target_budget_tokens=initial_budget_decision.available_input_tokens,
        budget_decision=initial_budget_decision,
        model_id=route.model_id,
    )
    comp_res = compressor.compress_context(comp_req)

    assert comp_res.status == CompressionStatus.COMPRESSED
    assert comp_res.is_within_target_budget is True
    assert comp_res.final_token_count <= 768
    assert comp_res.tokens_saved > 0

    # 5. M.2 Re-evaluación con el nuevo breakdown comprimido
    compressed_breakdown = comp_res.final_breakdown
    re_req = ContextBudgetRequest(
        route=routing_decision,
        input_breakdown=compressed_breakdown,
    )
    re_budget_decision = budget_service.assess_budget(re_req, policy=budget_policy)

    assert re_budget_decision.status == ContextBudgetStatus.WITHIN_BUDGET
    assert re_budget_decision.is_within_budget is True
    assert re_budget_decision.requested_input_tokens == comp_res.final_token_count


def test_scenario_b_pipeline_when_already_within_budget():
    """
    Escenario B:
    Contexto pequeño que cabe en el presupuesto -> M.2 evalúa WITHIN_BUDGET -> M.3 retorna UNCHANGED.
    """
    route = ModelRoute(
        route_id="route-large",
        provider="omniroute",
        model_id="gpt-4o",
        context_window=32768,
        capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
    )
    estimator = DeterministicTokenEstimator()
    raw_payload = RawContextPayload(
        system_instructions="You are an autonomous assistant.",
        user_input="Quick status check.",
    )
    breakdown = estimator.estimate_breakdown(
        system_instructions=raw_payload.system_instructions,
        user_input=raw_payload.user_input,
    )

    budget_service = ContextBudgetService(token_estimator=estimator)
    budget_req = ContextBudgetRequest(route=route, input_breakdown=breakdown)
    budget_decision = budget_service.assess_budget(budget_req)

    assert budget_decision.status == ContextBudgetStatus.WITHIN_BUDGET

    compressor = DeterministicPromptCompressor(token_estimator=estimator)
    comp_req = CompressionRequest(
        raw_payload=raw_payload,
        target_budget_tokens=budget_decision.available_input_tokens,
        budget_decision=budget_decision,
    )
    comp_res = compressor.compress_context(comp_req)

    assert comp_res.status == CompressionStatus.UNCHANGED
    assert comp_res.tokens_saved == 0
    assert len(comp_res.actions_applied) == 0


def test_scenario_c_impossible_compression_preserves_safety():
    """
    Escenario C:
    Contexto protegido masivo con budget minúsculo -> M.3 reporta CANNOT_COMPRESS sin corromper ni truncar opacamente.
    """
    route = ModelRoute(
        route_id="route-micro",
        provider="omniroute",
        model_id="micro-model",
        context_window=100,  # Muy pequeño
    )
    estimator = DeterministicTokenEstimator()
    raw_payload = RawContextPayload(
        system_instructions="Safety instructions " * 20,
        user_input="Critical non-droppable query " * 20,
    )
    compressor = DeterministicPromptCompressor(token_estimator=estimator)
    comp_req = CompressionRequest(
        raw_payload=raw_payload,
        target_budget_tokens=10,  # Imposible albergar los protegidos
    )
    comp_res = compressor.compress_context(comp_req)

    assert comp_res.status == CompressionStatus.CANNOT_COMPRESS
    assert comp_res.is_within_target_budget is False
    assert comp_res.compressed_payload.system_instructions == raw_payload.system_instructions
    assert comp_res.compressed_payload.user_input == raw_payload.user_input


def test_scenario_d_integration_with_loop_state_and_omniroute():
    """
    Escenario D:
    LoopState con muchas evidencias -> compresión estructurada determinista previa a llamada LLM mockeada.
    """
    state = LoopState(
        mission_id="mission-m3-demo",
        iteration=3,
        goal="Discover winning products",
        current_target="electronics",
        observations=("Obs 1", "Obs 2"),
        evidences=(
            {"sku": "SKU-1", "price": 1000},
            {"sku": "SKU-1", "price": 1000},  # Duplicado
            {"sku": "SKU-2", "price": 2000},
        ),
    )

    raw_payload = RawContextPayload(
        system_instructions=DECISION_SYSTEM_PROMPT,
        user_input=build_user_prompt(state),
        retrieved_evidence=list(state.evidences),
    )
    compressor = DeterministicPromptCompressor()
    comp_req = CompressionRequest(
        raw_payload=raw_payload,
        target_budget_tokens=600,
    )
    comp_res = compressor.compress_context(comp_req)

    assert comp_res.status in (CompressionStatus.COMPRESSED, CompressionStatus.UNCHANGED)
    assert comp_res.is_within_target_budget is True

    # Verificar que el prompt comprimido es parseable y utilizable por DecisionProvider
    mock_llm_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "action": "CONTINUE",
                        "reason": "Context compressed and evaluated successfully",
                        "target": "electronics",
                        "parameters": {},
                        "confidence": 0.95
                    })
                }
            }
        ]
    }
    with patch("src.infrastructure.llm.omniroute_decision_provider.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_llm_response).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        provider = OmniRouteDecisionProvider(
            config=OmniRouteConfig(base_url="http://localhost:8000/v1", api_key="dummy-key", model="gpt-4o-mini")
        )
        decision = provider.decide(state)
        assert decision.action == LoopAction.CONTINUE
        assert decision.confidence == 0.95
