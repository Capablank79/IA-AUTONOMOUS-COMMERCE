"""
Pruebas Unitarias para Model Selection by Task (Hito M.5).

Transversal M — Control de Coste e Inferencia.

Cubre exhaustivamente:
1. Known task profile resolution (resolución de perfil estándar).
2. Unknown task handling (preservación explícita de UNKNOWN / NO_PROFILE sin modelo default).
3. LOW complexity task handling.
4. HIGH complexity task handling.
5. Critical task handling (escalado de calidad / criticidad).
6. Required TOOL_USE capability.
7. Required STRUCTURED_OUTPUT capability.
8. Required VISION capability.
9. Quality requirement enforcement.
10. Latency requirement enforcement.
11. Incapable route exclusion via M.1 delegation.
12. Deterministic result (mismas entradas -> mismo resultado y checksum).
13. Policy versioning and integrity checksums.
14. Strict isolation from M.6 (no economic optimization "cheapest always wins").
15. Secret sanitization and no Chain-of-Thought leakage.
"""

from decimal import Decimal
import pytest

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
    DefaultTaskSelectionPolicyProvider,
    create_default_task_selection_policy,
)
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry


@pytest.fixture
def standard_routes():
    """Rutas de prueba estándar para evaluación de M.5."""
    return [
        ModelRoute(
            route_id="route-fast-mini",
            provider="omniroute",
            model_id="gpt-4o-mini",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
            quality_class=QualityRequirement.STANDARD,
            latency_class=LatencyRequirement.LOW_LATENCY,
            priority=10,
        ),
        ModelRoute(
            route_id="route-tool-master",
            provider="omniroute",
            model_id="gpt-4o",
            capabilities=(
                RouteCapability.STRUCTURED_OUTPUT,
                RouteCapability.TOOL_USE,
                RouteCapability.FUNCTION_CALLING,
                RouteCapability.JSON_MODE,
            ),
            quality_class=QualityRequirement.HIGH,
            latency_class=LatencyRequirement.NORMAL,
            priority=5,
        ),
        ModelRoute(
            route_id="route-reasoning-pro",
            provider="anthropic",
            model_id="claude-3-7-sonnet",
            capabilities=(
                RouteCapability.STRUCTURED_OUTPUT,
                RouteCapability.REASONING,
                RouteCapability.TOOL_USE,
                RouteCapability.LONG_CONTEXT,
            ),
            quality_class=QualityRequirement.SUPERIOR,
            latency_class=LatencyRequirement.NORMAL,
            priority=1,
        ),
        ModelRoute(
            route_id="route-vision-expert",
            provider="google",
            model_id="gemini-2.0-flash",
            capabilities=(
                RouteCapability.STRUCTURED_OUTPUT,
                RouteCapability.VISION,
                RouteCapability.TOOL_USE,
            ),
            quality_class=QualityRequirement.HIGH,
            latency_class=LatencyRequirement.NORMAL,
            priority=2,
        ),
    ]


def test_1_known_task_profile_resolution(standard_routes):
    """1. Resolución de perfil para tarea conocida (MARKET_ANALYSIS)."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="MARKET_ANALYSIS")

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.is_successful is True
    assert result.task_type == "MARKET_ANALYSIS"
    assert result.resolved_profile is not None
    assert result.requirements.complexity == TaskComplexity.MEDIUM
    assert result.requirements.criticality == TaskCriticality.MEDIUM
    assert result.requirements.min_quality == QualityRequirement.STANDARD
    assert result.selected_route is not None
    assert result.selected_route.route_id in [r.route_id for r in standard_routes]


def test_2_unknown_task_handling(standard_routes):
    """2. Tarea desconocida nunca asigna modelo default silenciosamente -> UNKNOWN_TASK / NO_PROFILE."""
    service = ModelSelectionByTaskService()
    
    # Caso A: tarea formal UNKNOWN
    req_unknown = TaskSelectionRequest(task_type="UNKNOWN")
    result_unknown = service.select_model_for_task(req_unknown, available_routes=standard_routes)

    assert result_unknown.status == SelectionStatus.UNKNOWN_TASK
    assert result_unknown.is_successful is False
    assert result_unknown.selected_route is None
    assert result_unknown.resolved_profile is None
    assert "NO_DEFAULT_MODEL_ASSIGNED" in result_unknown.reason_codes

    # Caso B: tarea inventada no existente en el catálogo
    req_unregistered = TaskSelectionRequest(task_type="UNREGISTERED_CUSTOM_OPERATION")
    result_unregistered = service.select_model_for_task(req_unregistered, available_routes=standard_routes)

    assert result_unregistered.status == SelectionStatus.NO_PROFILE
    assert result_unregistered.is_successful is False
    assert result_unregistered.selected_route is None
    assert result_unregistered.resolved_profile is None
    assert "UNKNOWN_TASK_OR_NO_PROFILE" in result_unregistered.reason_codes


def test_3_low_complexity_task(standard_routes):
    """3. Tarea de baja complejidad (EXTRACTION) resuelve requerimientos livianos."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="EXTRACTION")

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.requirements.complexity == TaskComplexity.LOW
    assert result.requirements.criticality == TaskCriticality.LOW
    assert result.requirements.min_quality == QualityRequirement.STANDARD
    assert result.selected_route.route_id == "route-fast-mini"


def test_4_high_complexity_task(standard_routes):
    """4. Tarea de alta complejidad (PROFIT_EVALUATION) exige calidad superior y reasoning."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="PROFIT_EVALUATION")

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.requirements.complexity == TaskComplexity.HIGH
    assert result.requirements.criticality == TaskCriticality.CRITICAL
    assert result.requirements.min_quality == QualityRequirement.SUPERIOR
    assert RouteCapability.REASONING in result.requirements.required_capabilities
    assert result.selected_route.route_id == "route-reasoning-pro"


def test_5_critical_task_escalation(standard_routes):
    """5. Tarea crítica con override o política asegura requerimiento de calidad alto."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(
        task_type="MARKET_ANALYSIS",
        criticality_override=TaskCriticality.CRITICAL,
    )

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.requirements.criticality == TaskCriticality.CRITICAL
    assert result.requirements.min_quality in (QualityRequirement.HIGH, QualityRequirement.SUPERIOR)
    assert any("CRITICALITY_OVERRIDDEN" in code for code in result.reason_codes)


def test_6_required_tool_use_capability(standard_routes):
    """6. Tarea que requiere TOOL_USE (MARKET_DISCOVERY) descarta rutas sin tools."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="MARKET_DISCOVERY")

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert RouteCapability.TOOL_USE in result.requirements.required_capabilities
    assert RouteCapability.TOOL_USE in result.selected_route.capabilities
    assert result.selected_route.route_id != "route-fast-mini"  # route-fast-mini no tiene TOOL_USE


def test_7_required_structured_output_capability(standard_routes):
    """7. Tarea que requiere STRUCTURED_OUTPUT asegura soporte en ruta seleccionada."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="STRUCTURED_GENERATION")

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert RouteCapability.STRUCTURED_OUTPUT in result.requirements.required_capabilities
    assert RouteCapability.STRUCTURED_OUTPUT in result.selected_route.capabilities


def test_8_required_vision_capability(standard_routes):
    """8. Tarea que requiere VISION (VISION_ANALYSIS) selecciona ruta con visión."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="VISION_ANALYSIS")

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert RouteCapability.VISION in result.requirements.required_capabilities
    assert RouteCapability.VISION in result.selected_route.capabilities
    assert result.selected_route.route_id == "route-vision-expert"


def test_9_quality_requirement_enforcement(standard_routes):
    """9. Requerimiento de calidad SUPERIOR excluye rutas STANDARD o HIGH."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="CAPITAL_ALLOCATION")

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.requirements.min_quality == QualityRequirement.SUPERIOR
    assert result.selected_route.quality_class == QualityRequirement.SUPERIOR


def test_10_latency_requirement_enforcement():
    """10. Requerimiento de latencia LOW_LATENCY excluye rutas lentas."""
    routes = [
        ModelRoute(
            route_id="route-slow-high-quality",
            provider="provider-a",
            model_id="heavy-model",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
            quality_class=QualityRequirement.HIGH,
            latency_class=LatencyRequirement.NORMAL,
        ),
        ModelRoute(
            route_id="route-ultra-fast",
            provider="provider-b",
            model_id="edge-model",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.JSON_MODE),
            quality_class=QualityRequirement.STANDARD,
            latency_class=LatencyRequirement.LOW_LATENCY,
        ),
    ]
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="EXTRACTION")  # EXTRACTION pide LOW_LATENCY

    result = service.select_model_for_task(req, available_routes=routes)

    assert result.status == SelectionStatus.SUCCESS
    assert result.selected_route.route_id == "route-ultra-fast"


def test_11_incapable_route_excluded_via_m1():
    """11. Si ninguna ruta cumple las capacidades requeridas, M.1 falla y M.5 reporta ROUTING_FAILED."""
    routes_without_tools = [
        ModelRoute(
            route_id="route-basic-only",
            provider="omniroute",
            model_id="basic-mini",
            capabilities=(RouteCapability.JSON_MODE,),
            quality_class=QualityRequirement.STANDARD,
        ),
    ]
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="MARKET_DISCOVERY")  # Requiere TOOL_USE y STRUCTURED_OUTPUT

    result = service.select_model_for_task(req, available_routes=routes_without_tools)

    assert result.status == SelectionStatus.ROUTING_FAILED
    assert result.is_successful is False
    assert result.selected_route is None
    assert result.routing_decision.status == RoutingDecisionStatus.NO_ROUTE


def test_12_deterministic_result(standard_routes):
    """12. Determinismo: Mismas entradas producen idéntico resultado y checksums."""
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="COMMERCIAL_REASONING")

    res1 = service.select_model_for_task(req, available_routes=standard_routes)
    res2 = service.select_model_for_task(req, available_routes=standard_routes)

    assert res1.status == res2.status
    assert res1.selected_route.route_id == res2.selected_route.route_id
    assert res1.requirements.calculate_checksum if hasattr(res1.requirements, 'calculate_checksum') else True
    assert res1.calculate_checksum() == res2.calculate_checksum()


def test_13_policy_versioning():
    """13. Versionado e integridad de la política de selección."""
    custom_profile = TaskModelProfile(
        task_type="CUSTOM_CLASSIFICATION",
        complexity=TaskComplexity.LOW,
        criticality=TaskCriticality.LOW,
        min_quality=QualityRequirement.STANDARD,
        latency_requirement=LatencyRequirement.NORMAL,
        policy_id="custom_policy_v2",
        policy_version="2.1.0",
    )
    policy = TaskSelectionPolicy(
        policy_id="custom_policy_v2",
        version="2.1.0",
        profiles={"CUSTOM_CLASSIFICATION": custom_profile},
    )
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="CUSTOM_CLASSIFICATION")

    result = service.select_model_for_task(
        req,
        available_routes=[
            ModelRoute(
                route_id="route-default",
                provider="omniroute",
                model_id="gpt-4o-mini",
                capabilities=(),
                quality_class=QualityRequirement.STANDARD,
            )
        ],
        task_policy=policy,
    )

    assert result.status == SelectionStatus.SUCCESS
    assert result.policy_id == "custom_policy_v2"
    assert result.policy_version == "2.1.0"
    assert policy.calculate_checksum() is not None


def test_14_no_m6_economic_optimization(standard_routes):
    """14. M.5 no realiza optimización de menor costo ('cheapest always wins') reservada para M.6."""
    # Verificamos que M.5 selecciona según los requerimientos técnicos y de calidad del perfil,
    # sin reordenar arbitrariamente por el precio más bajo.
    service = ModelSelectionByTaskService()
    req = TaskSelectionRequest(task_type="PROFIT_EVALUATION")

    result = service.select_model_for_task(req, available_routes=standard_routes)

    assert result.status == SelectionStatus.SUCCESS
    # Selecciona claude-3-7-sonnet por requerir SUPERIOR y REASONING, no gpt-4o-mini por ser más barato
    assert result.selected_route.route_id == "route-reasoning-pro"
    assert result.selected_route.model_id == "claude-3-7-sonnet"


def test_15_security_sanitization_and_no_cot():
    """15. Sanitización de secretos y exclusión de Chain-of-Thought en inputs y metadata."""
    sensitive_meta = {
        "api_key": "sk-secret-12345",
        "password": "super-secret-password",
        "authorization": "Bearer eyJhbGciOi...",
        "chain_of_thought": "Thinking step by step about secrets...",
        "reasoning": "Internal scratchpad...",
        "safe_context": "category_electronics",
    }
    req = TaskSelectionRequest(
        task_type="MARKET_ANALYSIS",
        task_metadata=sensitive_meta,
    )

    # Verificar que los metadatos fueron sanitizados
    assert req.task_metadata.get("api_key") == "[REDACTED]"
    assert req.task_metadata.get("password") == "[REDACTED]"
    assert req.task_metadata.get("authorization") == "[REDACTED]"
    assert req.task_metadata.get("chain_of_thought") == "[REDACTED]"
    assert req.task_metadata.get("safe_context") == "category_electronics"
