"""
Pruebas de Integración para Model Selection by Task (Hito M.5).

Transversal M — Control de Coste e Inferencia.

Escenarios de Integración requeridos:
A. simple structured extraction -> profile -> suitable M.1 route.
B. high-complexity reasoning task -> stronger requirements -> appropriate eligible route.
C. task requires tool use -> non-tool route excluded.
D. high-criticality commercial task -> inadequate-quality route excluded.
E. unknown task -> explicit UNKNOWN/NO_PROFILE.
F. preferred route unavailable -> M.1 fallback.
G. M.5 -> M.1 -> M.2 -> M.3 -> M.4 compatible pipeline.
E2E: Real Mission/Agent task -> task classification/profile -> M.5 requirements -> M.1 RoutingDecision -> M.2 budget -> M.3 if needed -> M.4 cache -> inference boundary / mock provider.
"""

from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.domain.mission.models import MissionType
from src.domain.model_routing.models import (
    ModelRoute,
    RoutingDecisionStatus,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RouteCapability,
    RouteStatus,
    RoutingPolicy,
)
from src.domain.model_selection.models import (
    TaskComplexity,
    SelectionStatus,
    StandardTaskType,
    TaskModelProfile,
    TaskSelectionPolicy,
    TaskSelectionRequest,
    TaskSelectionRequirements,
    ModelSelectionResult,
)
from src.application.model_selection.model_selection_service import (
    ModelSelectionByTaskService,
    create_default_task_selection_policy,
)
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry
from src.domain.context_budget.models import (
    ContextBudgetRequest,
    ContextBudgetPolicy,
    ContextBudgetStatus,
    InputTokensBreakdown,
)
from src.application.context_budget.context_budget_service import ContextBudgetService
from src.domain.prompt_compression.models import (
    CompressionRequest,
    CompressionPolicy,
    CompressionStatus,
    RawContextPayload,
    ContextItem,
    ContextComponentType,
    PriorityLevel,
)
from src.application.prompt_compression.deterministic_compressor import DeterministicPromptCompressor
from src.domain.caching.models import (
    CacheLookupRequest,
    CacheStoreRequest,
    CacheLookupStatus,
    CachePolicy,
)
from src.application.caching.inference_cache_service import InferenceCacheService
from src.infrastructure.persistence.data.in_memory.cache_repository import InMemoryCacheRepository


@pytest.fixture
def enterprise_routes():
    """Catálogo realista de rutas disponibles en el sistema."""
    return [
        ModelRoute(
            route_id="route-fast-lite",
            provider="omniroute",
            model_id="gpt-4o-mini",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
            status=RouteStatus.AVAILABLE,
            context_window=128000,
            quality_class=QualityRequirement.STANDARD,
            latency_class=LatencyRequirement.LOW_LATENCY,
            priority=10,
        ),
        ModelRoute(
            route_id="route-agent-tools",
            provider="omniroute",
            model_id="gpt-4o",
            capabilities=(
                RouteCapability.STRUCTURED_OUTPUT,
                RouteCapability.TOOL_USE,
                RouteCapability.FUNCTION_CALLING,
                RouteCapability.JSON_MODE,
            ),
            status=RouteStatus.AVAILABLE,
            context_window=128000,
            quality_class=QualityRequirement.HIGH,
            latency_class=LatencyRequirement.NORMAL,
            priority=5,
        ),
        ModelRoute(
            route_id="route-commercial-reasoning",
            provider="anthropic",
            model_id="claude-3-7-sonnet",
            capabilities=(
                RouteCapability.STRUCTURED_OUTPUT,
                RouteCapability.REASONING,
                RouteCapability.TOOL_USE,
                RouteCapability.LONG_CONTEXT,
            ),
            status=RouteStatus.AVAILABLE,
            context_window=200000,
            quality_class=QualityRequirement.SUPERIOR,
            latency_class=LatencyRequirement.NORMAL,
            priority=1,
        ),
    ]


def test_scenario_a_simple_structured_extraction(enterprise_routes):
    """Escenario A: simple structured extraction -> profile -> suitable M.1 route."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type=StandardTaskType.EXTRACTION.value)

    result = service.select_model_for_task(req, available_routes=enterprise_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.is_successful is True
    assert result.requirements.complexity == TaskComplexity.LOW
    assert result.requirements.latency_requirement == LatencyRequirement.LOW_LATENCY
    assert result.selected_route.route_id == "route-fast-lite"
    assert result.routing_decision.status == RoutingDecisionStatus.SELECTED


def test_scenario_b_high_complexity_reasoning_task(enterprise_routes):
    """Escenario B: high-complexity reasoning task -> stronger requirements -> appropriate eligible route."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type=StandardTaskType.COMMERCIAL_REASONING.value)

    result = service.select_model_for_task(req, available_routes=enterprise_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.requirements.complexity == TaskComplexity.HIGH
    assert result.requirements.criticality == TaskCriticality.CRITICAL
    assert result.requirements.min_quality == QualityRequirement.SUPERIOR
    assert RouteCapability.REASONING in result.requirements.required_capabilities
    assert result.selected_route.route_id == "route-commercial-reasoning"


def test_scenario_c_tool_use_requirement_excludes_non_tool_routes(enterprise_routes):
    """Escenario C: task requires tool use -> non-tool route excluded."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type=StandardTaskType.SUPPLIER_DISCOVERY.value)

    result = service.select_model_for_task(req, available_routes=enterprise_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert RouteCapability.TOOL_USE in result.requirements.required_capabilities
    assert result.selected_route.route_id in ("route-agent-tools", "route-commercial-reasoning")
    # route-fast-lite queda descartada por falta de TOOL_USE
    excluded_ids = [e.route_id for e in result.routing_decision.excluded_routes]
    assert "route-fast-lite" in excluded_ids


def test_scenario_d_high_criticality_commercial_task(enterprise_routes):
    """Escenario D: high-criticality commercial task -> inadequate-quality route excluded."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type=StandardTaskType.PROFIT_EVALUATION.value)

    result = service.select_model_for_task(req, available_routes=enterprise_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.requirements.criticality == TaskCriticality.CRITICAL
    assert result.requirements.min_quality == QualityRequirement.SUPERIOR
    # Solo route-commercial-reasoning tiene calidad SUPERIOR
    assert result.selected_route.route_id == "route-commercial-reasoning"


def test_scenario_e_unknown_task_explicit_status(enterprise_routes):
    """Escenario E: unknown task -> explicit UNKNOWN/NO_PROFILE."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="NON_EXISTENT_MISSION_TYPE")

    result = service.select_model_for_task(req, available_routes=enterprise_routes)

    assert result.status == SelectionStatus.NO_PROFILE
    assert result.is_successful is False
    assert result.selected_route is None
    assert result.routing_decision is None
    assert "NO_DEFAULT_MODEL_ASSIGNED" in result.reason_codes


def test_scenario_f_preferred_route_unavailable_fallback(enterprise_routes):
    """Escenario F: preferred route unavailable -> M.1 fallback."""
    # Marcamos route-commercial-reasoning como UNAVAILABLE
    routes_with_outage = [
        ModelRoute(
            route_id="route-fast-lite",
            provider="omniroute",
            model_id="gpt-4o-mini",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
            status=RouteStatus.AVAILABLE,
            quality_class=QualityRequirement.STANDARD,
            priority=10,
        ),
        ModelRoute(
            route_id="route-agent-tools",
            provider="omniroute",
            model_id="gpt-4o",
            capabilities=(
                RouteCapability.STRUCTURED_OUTPUT,
                RouteCapability.TOOL_USE,
                RouteCapability.FUNCTION_CALLING,
                RouteCapability.JSON_MODE,
            ),
            status=RouteStatus.AVAILABLE,
            quality_class=QualityRequirement.HIGH,
            priority=5,
        ),
        ModelRoute(
            route_id="route-commercial-reasoning",
            provider="anthropic",
            model_id="claude-3-7-sonnet",
            capabilities=(
                RouteCapability.STRUCTURED_OUTPUT,
                RouteCapability.REASONING,
                RouteCapability.TOOL_USE,
                RouteCapability.LONG_CONTEXT,
            ),
            status=RouteStatus.UNAVAILABLE,  # Caída de proveedor
            quality_class=QualityRequirement.SUPERIOR,
            priority=1,
        ),
    ]
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(
        task_type=StandardTaskType.MARKET_DISCOVERY.value,
        preferred_route_id="route-commercial-reasoning",
    )

    result = service.select_model_for_task(req, available_routes=routes_with_outage)

    assert result.status == SelectionStatus.SUCCESS
    # Fallback determinista a route-agent-tools que cumple TOOL_USE y STRUCTURED_OUTPUT
    assert result.selected_route.route_id == "route-agent-tools"
    assert result.routing_decision.fallback_applied is True


def test_scenario_g_pipeline_m5_to_m1_to_m2_to_m3_to_m4(enterprise_routes):
    """
    Escenario G & E2E:
    Demostrar flujo integral completamente compatible:
    Task (Market Discovery)
    -> M.5 Requirements resolution
    -> M.1 Routing Decision
    -> M.2 Context Budget evaluation
    -> M.3 Prompt Compression (si excede presupuesto)
    -> M.4 Inference Caching
    -> Mock Inference Output.
    """
    # 1. M.5 Model Selection by Task
    selection_service = ModelSelectionByTaskService()
    task_req = TaskSelectionRequest(task_type=StandardTaskType.MARKET_DISCOVERY.value)
    selection_result = selection_service.select_model_for_task(task_req, available_routes=enterprise_routes)

    assert selection_result.status == SelectionStatus.SUCCESS
    selected_route = selection_result.selected_route
    assert selected_route is not None

    # 2. M.2 Context Budgeting
    budget_service = ContextBudgetService()
    budget_policy = ContextBudgetPolicy(
        policy_id="test_budget_policy",
        default_reserved_output_tokens=2000,
        safety_margin_tokens=500,
    )
    
    # Supongamos una entrada con histórico extenso
    breakdown = InputTokensBreakdown(
        system_instructions=500,
        user_input=300,
        conversation_history=1000,
        retrieved_evidence=2500,
    )
    budget_req = ContextBudgetRequest(
        route=selected_route,
        input_breakdown=breakdown,
    )
    budget_decision = budget_service.assess_budget(budget_req, policy=budget_policy)
    assert budget_decision.status == ContextBudgetStatus.WITHIN_BUDGET
    assert budget_decision.available_input_tokens > breakdown.total_input_tokens

    # 3. M.3 Prompt Compression (simulación si estuviera en límite)
    compressor = DeterministicPromptCompressor()
    compression_policy = CompressionPolicy(policy_id="test_comp_policy")
    items = (
        ContextItem(
            item_id="item-sys",
            component_type=ContextComponentType.SYSTEM_INSTRUCTIONS,
            priority=PriorityLevel.PROTECTED,
            content="You are a market discovery assistant.",
            token_count=500,
        ),
        ContextItem(
            item_id="item-user",
            component_type=ContextComponentType.USER_INPUT,
            priority=PriorityLevel.PROTECTED,
            content="Analyze category wireless audio.",
            token_count=300,
        ),
        ContextItem(
            item_id="item-ev",
            component_type=ContextComponentType.RETRIEVED_EVIDENCE,
            priority=PriorityLevel.NORMAL,
            content='{"listing_id": "MLA1", "title": "Headphones"}',
            token_count=2500,
        ),
    )
    raw_payload = RawContextPayload(
        system_instructions="You are a market discovery assistant.",
        user_input="Analyze category wireless audio.",
        custom_items=items,
    )
    comp_req = CompressionRequest(
        raw_payload=raw_payload,
        target_budget_tokens=3000,
        policy=compression_policy,
    )
    comp_result = compressor.compress_context(comp_req)
    assert comp_result.status in (CompressionStatus.COMPRESSED, CompressionStatus.UNCHANGED)

    # 4. M.4 Caching lookup & store
    cache_repo = InMemoryCacheRepository()
    cache_service = InferenceCacheService(repository=cache_repo)
    
    cache_lookup_req = CacheLookupRequest(
        normalized_prompt_or_payload="Analyze category wireless audio.",
        route_or_model_id=selected_route.route_id,
    )
    lookup_res = cache_service.lookup(cache_lookup_req)
    assert lookup_res.status == CacheLookupStatus.MISS  # Primer acceso -> MISS

    # Simular inferencia y almacenar en caché
    mock_inference_output = {"discovered_opportunities": ["MLA1001", "MLA1002"]}
    store_req = CacheStoreRequest(
        lookup_request=cache_lookup_req,
        result_data=mock_inference_output,
    )
    store_res = cache_service.store(store_req)
    assert store_res is not None
    assert store_res.result_data["discovered_opportunities"] == ("MLA1001", "MLA1002")

    # Segundo lookup -> HIT
    lookup_hit = cache_service.lookup(cache_lookup_req)
    assert lookup_hit.status == CacheLookupStatus.HIT
    assert lookup_hit.entry is not None
    assert lookup_hit.entry.result_data["discovered_opportunities"] == ("MLA1001", "MLA1002")


def test_e2e_mission_orchestration_flow(enterprise_routes):
    """
    E2E: Demuestra cómo una misión comercial real de tipo MissionType.PROFIT_EVALUATION
    obtiene su perfil de inferencia mediante M.5, delega a M.1, valida contexto y ejecuta inferencia mock.
    """
    # 1. Simulación de Mission Type real
    mission_type = MissionType.PROFIT_EVALUATION.value
    
    # 2. Invocación de M.5
    selection_service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(
        task_type=mission_type,
        correlation_id="mission-run-9988",
    )
    selection_result = selection_service.select_model_for_task(req, available_routes=enterprise_routes)

    # 3. Validar selección estructurada
    assert selection_result.is_successful is True
    assert selection_result.status == SelectionStatus.SUCCESS
    assert selection_result.requirements.criticality == TaskCriticality.CRITICAL
    assert selection_result.requirements.min_quality == QualityRequirement.SUPERIOR
    assert selection_result.selected_route.route_id == "route-commercial-reasoning"
    assert selection_result.routing_decision.is_selected is True
    assert selection_result.calculate_checksum() is not None
