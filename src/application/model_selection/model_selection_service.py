"""
Servicio de Aplicación para Selección de Modelos por Tarea (Model Selection by Task - M.5).

Transversal M — Control de Coste e Inferencia.

Implementa ModelSelectionByTaskServicePort orquestando:
1. Recepción y validación del descriptor / solicitud de tarea (TaskSelectionRequest).
2. Resolución del perfil de modelo de la tarea (TaskModelProfile) vía TaskSelectionPolicy.
3. Tratamiento explícito de tareas desconocidas (UNKNOWN / UNKNOWN_TASK / NO_PROFILE):
   - Nunca asigna un modelo por defecto silenciosamente.
   - Preserva la incertidumbre y reporta reason codes claros.
4. Consolidación de requerimientos de inferencia:
   - Complejidad intrínseca (LOW, MEDIUM, HIGH, UNKNOWN).
   - Criticidad de negocio (LOW, MEDIUM, HIGH, CRITICAL) de M.1.
   - Capacidades técnicas obligatorias (TOOL_USE, STRUCTURED_OUTPUT, VISION, etc.) de M.1.
   - Requerimientos mínimos de calidad (STANDARD, HIGH, SUPERIOR) y latencia máxima.
   - Transporte de límites o preferencias de costo sin implementar optimización económica global (M.6).
5. Transformación del perfil a un RoutingRequest estándar de M.1.
6. Invocación de la estrategia de routing M.1 (DeterministicModelRoutingStrategy).
7. Ensamblado y retorno del ModelSelectionResult estructurado e inmutable.
"""

from decimal import Decimal
from typing import Optional, Sequence, List, Dict, Any, Tuple
from datetime import datetime, timezone

from src.domain.model_routing.models import (
    ModelRoute,
    RoutingRequest,
    RoutingPolicy,
    RoutingDecision,
    RoutingDecisionStatus,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RouteCapability,
)
from src.domain.model_routing.ports import (
    ModelRoutingStrategyPort,
    ModelRouteRegistryPort,
)
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
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
from src.domain.model_selection.ports import (
    TaskSelectionPolicyPort,
    ModelSelectionByTaskServicePort,
)


def get_default_task_profiles() -> Dict[str, TaskModelProfile]:
    """
    Construye el catálogo canónico de perfiles de tareas reales del sistema.
    """
    profiles: Dict[str, TaskModelProfile] = {
        # 1. Market Analysis / Discovery
        StandardTaskType.MARKET_DISCOVERY.value: TaskModelProfile(
            task_type=StandardTaskType.MARKET_DISCOVERY.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.HIGH,
            min_quality=QualityRequirement.HIGH,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.TOOL_USE),
        ),
        StandardTaskType.MARKET_ANALYSIS.value: TaskModelProfile(
            task_type=StandardTaskType.MARKET_ANALYSIS.value,
            complexity=TaskComplexity.MEDIUM,
            criticality=TaskCriticality.MEDIUM,
            min_quality=QualityRequirement.STANDARD,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        ),
        # 2. Simple Extraction & Classification
        StandardTaskType.EXTRACTION.value: TaskModelProfile(
            task_type=StandardTaskType.EXTRACTION.value,
            complexity=TaskComplexity.LOW,
            criticality=TaskCriticality.LOW,
            min_quality=QualityRequirement.STANDARD,
            latency_requirement=LatencyRequirement.LOW_LATENCY,
            required_capabilities=(RouteCapability.JSON_MODE, RouteCapability.STRUCTURED_OUTPUT),
        ),
        StandardTaskType.CLASSIFICATION.value: TaskModelProfile(
            task_type=StandardTaskType.CLASSIFICATION.value,
            complexity=TaskComplexity.LOW,
            criticality=TaskCriticality.LOW,
            min_quality=QualityRequirement.STANDARD,
            latency_requirement=LatencyRequirement.LOW_LATENCY,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        ),
        # 3. Supplier Search & Discovery
        StandardTaskType.SUPPLIER_SEARCH.value: TaskModelProfile(
            task_type=StandardTaskType.SUPPLIER_SEARCH.value,
            complexity=TaskComplexity.MEDIUM,
            criticality=TaskCriticality.MEDIUM,
            min_quality=QualityRequirement.STANDARD,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        ),
        StandardTaskType.SUPPLIER_DISCOVERY.value: TaskModelProfile(
            task_type=StandardTaskType.SUPPLIER_DISCOVERY.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.HIGH,
            min_quality=QualityRequirement.HIGH,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        ),
        StandardTaskType.SUPPLIER_ANALYSIS.value: TaskModelProfile(
            task_type=StandardTaskType.SUPPLIER_ANALYSIS.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.HIGH,
            min_quality=QualityRequirement.HIGH,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.REASONING),
        ),
        # 4. Economics & Capital
        StandardTaskType.PROFIT_EVALUATION.value: TaskModelProfile(
            task_type=StandardTaskType.PROFIT_EVALUATION.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.CRITICAL,
            min_quality=QualityRequirement.SUPERIOR,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.REASONING),
        ),
        StandardTaskType.CAPITAL_ALLOCATION.value: TaskModelProfile(
            task_type=StandardTaskType.CAPITAL_ALLOCATION.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.CRITICAL,
            min_quality=QualityRequirement.SUPERIOR,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.REASONING),
        ),
        StandardTaskType.OPERATING_MODEL_EVALUATION.value: TaskModelProfile(
            task_type=StandardTaskType.OPERATING_MODEL_EVALUATION.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.HIGH,
            min_quality=QualityRequirement.HIGH,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.REASONING),
        ),
        StandardTaskType.COMMERCIAL_REASONING.value: TaskModelProfile(
            task_type=StandardTaskType.COMMERCIAL_REASONING.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.CRITICAL,
            min_quality=QualityRequirement.SUPERIOR,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.REASONING, RouteCapability.STRUCTURED_OUTPUT),
        ),
        # 5. Publication & Content Generation
        StandardTaskType.STRUCTURED_GENERATION.value: TaskModelProfile(
            task_type=StandardTaskType.STRUCTURED_GENERATION.value,
            complexity=TaskComplexity.MEDIUM,
            criticality=TaskCriticality.MEDIUM,
            min_quality=QualityRequirement.STANDARD,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        ),
        StandardTaskType.COMMERCIAL_PUBLICATION.value: TaskModelProfile(
            task_type=StandardTaskType.COMMERCIAL_PUBLICATION.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.HIGH,
            min_quality=QualityRequirement.HIGH,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.TOOL_USE),
        ),
        StandardTaskType.LISTING_GENERATION.value: TaskModelProfile(
            task_type=StandardTaskType.LISTING_GENERATION.value,
            complexity=TaskComplexity.MEDIUM,
            criticality=TaskCriticality.MEDIUM,
            min_quality=QualityRequirement.STANDARD,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        ),
        # 6. Governance & Policy
        StandardTaskType.POLICY_SENSITIVE_DECISION.value: TaskModelProfile(
            task_type=StandardTaskType.POLICY_SENSITIVE_DECISION.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.CRITICAL,
            min_quality=QualityRequirement.SUPERIOR,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.REASONING, RouteCapability.STRUCTURED_OUTPUT),
        ),
        StandardTaskType.POLICY_EVALUATION.value: TaskModelProfile(
            task_type=StandardTaskType.POLICY_EVALUATION.value,
            complexity=TaskComplexity.MEDIUM,
            criticality=TaskCriticality.HIGH,
            min_quality=QualityRequirement.HIGH,
            latency_requirement=LatencyRequirement.LOW_LATENCY,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        ),
        # 7. Tool Execution & Vision
        StandardTaskType.TOOL_EXECUTION_PLANNING.value: TaskModelProfile(
            task_type=StandardTaskType.TOOL_EXECUTION_PLANNING.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.HIGH,
            min_quality=QualityRequirement.HIGH,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.TOOL_USE, RouteCapability.FUNCTION_CALLING, RouteCapability.STRUCTURED_OUTPUT),
        ),
        StandardTaskType.VISION_ANALYSIS.value: TaskModelProfile(
            task_type=StandardTaskType.VISION_ANALYSIS.value,
            complexity=TaskComplexity.HIGH,
            criticality=TaskCriticality.MEDIUM,
            min_quality=QualityRequirement.HIGH,
            latency_requirement=LatencyRequirement.NORMAL,
            required_capabilities=(RouteCapability.VISION, RouteCapability.STRUCTURED_OUTPUT),
        ),
    }
    return profiles


def create_default_task_selection_policy() -> TaskSelectionPolicy:
    """Crea la política por defecto de selección de modelos con los perfiles del sistema."""
    return TaskSelectionPolicy(
        policy_id="default_task_selection_policy",
        version="1.0.0",
        profiles=get_default_task_profiles(),
        allow_dynamic_override=True,
        strict_capability_matching=True,
    )


class DefaultTaskSelectionPolicyProvider(TaskSelectionPolicyPort):
    """
    Proveedor en memoria de la política de selección por tarea.
    """
    def __init__(self, policy: Optional[TaskSelectionPolicy] = None):
        self._policy = policy or create_default_task_selection_policy()

    def get_policy(self) -> TaskSelectionPolicy:
        return self._policy

    def get_profile(self, task_type: str) -> Optional[TaskModelProfile]:
        return self._policy.get_profile(task_type)


class ModelSelectionByTaskService(ModelSelectionByTaskServicePort):
    """
    Servicio de Selección de Modelos por Tarea (M.5).
    """
    def __init__(
        self,
        routing_strategy: Optional[ModelRoutingStrategyPort] = None,
        policy_provider: Optional[TaskSelectionPolicyPort] = None,
        registry: Optional[ModelRouteRegistryPort] = None,
    ):
        self._routing_strategy = routing_strategy or DeterministicModelRoutingStrategy(registry=registry)
        self._policy_provider = policy_provider or DefaultTaskSelectionPolicyProvider()

    def resolve_requirements(
        self,
        request: TaskSelectionRequest,
        policy: Optional[TaskSelectionPolicy] = None,
    ) -> TaskSelectionRequirements:
        """
        Resuelve los requerimientos de inferencia a partir de la tarea y la política configurada.
        """
        active_policy = policy or self._policy_provider.get_policy()
        profile = active_policy.get_profile(request.task_type)

        reason_codes: List[str] = []

        if profile is None or request.task_type.upper() == StandardTaskType.UNKNOWN.value:
            # Tarea desconocida: no inventar requerimientos arbitrarios
            reason_codes.append("TASK_PROFILE_UNKNOWN")
            return TaskSelectionRequirements(
                task_type=request.task_type,
                complexity=request.complexity_override or TaskComplexity.UNKNOWN,
                criticality=request.criticality_override or TaskCriticality.LOW,
                min_quality=QualityRequirement.ANY,
                latency_requirement=LatencyRequirement.ANY,
                required_capabilities=request.additional_capabilities,
                preferred_provider=request.preferred_provider,
                preferred_route_id=request.preferred_route_id,
                cost_ceiling=request.cost_ceiling,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                reason_codes=tuple(reason_codes),
            )

        reason_codes.append("TASK_PROFILE_RESOLVED")

        # Determinar complejidad y criticidad con soporte a override si la política lo autoriza
        complexity = profile.complexity
        if active_policy.allow_dynamic_override and request.complexity_override is not None:
            complexity = request.complexity_override
            reason_codes.append(f"COMPLEXITY_OVERRIDDEN:{complexity.value}")

        criticality = profile.criticality
        if active_policy.allow_dynamic_override and request.criticality_override is not None:
            criticality = request.criticality_override
            reason_codes.append(f"CRITICALITY_OVERRIDDEN:{criticality.value}")

        # Consolidar capacidades requeridas (perfil base + adicionales de la petición)
        all_caps = set(profile.required_capabilities)
        if request.additional_capabilities:
            all_caps.update(request.additional_capabilities)
            reason_codes.append("ADDITIONAL_CAPABILITIES_ADDED")

        # Ajuste determinista de calidad por criticidad o complejidad si es necesario
        min_quality = profile.min_quality
        if criticality == TaskCriticality.CRITICAL and min_quality == QualityRequirement.STANDARD:
            min_quality = QualityRequirement.HIGH
            reason_codes.append("QUALITY_ESCALATED_FOR_CRITICALITY")

        # Preferencias de proveedor / ruta y límites de costo
        preferred_provider = request.preferred_provider or profile.preferred_provider
        preferred_route_id = request.preferred_route_id or profile.preferred_route_id
        cost_ceiling = request.cost_ceiling if request.cost_ceiling is not None else profile.max_cost_limit

        return TaskSelectionRequirements(
            task_type=request.task_type,
            complexity=complexity,
            criticality=criticality,
            min_quality=min_quality,
            latency_requirement=profile.latency_requirement,
            required_capabilities=tuple(all_caps),
            preferred_provider=preferred_provider,
            preferred_route_id=preferred_route_id,
            cost_ceiling=cost_ceiling,
            policy_id=active_policy.policy_id,
            policy_version=active_policy.version,
            reason_codes=tuple(reason_codes),
        )

    def select_model_for_task(
        self,
        request: TaskSelectionRequest,
        available_routes: Optional[Sequence[ModelRoute]] = None,
        task_policy: Optional[TaskSelectionPolicy] = None,
        routing_policy: Optional[RoutingPolicy] = None,
    ) -> ModelSelectionResult:
        """
        Orquesta la selección completa de modelo por tarea y delegación a M.1.
        """
        active_task_policy = task_policy or self._policy_provider.get_policy()
        profile = active_task_policy.get_profile(request.task_type)

        # 1. Validación de tarea conocida vs desconocida
        if profile is None or request.task_type.upper() == StandardTaskType.UNKNOWN.value:
            # Preservar incertidumbre: no seleccionar un modelo arbitrario
            reqs = self.resolve_requirements(request, policy=active_task_policy)
            status = SelectionStatus.UNKNOWN_TASK if request.task_type.upper() == StandardTaskType.UNKNOWN.value else SelectionStatus.NO_PROFILE
            return ModelSelectionResult(
                status=status,
                task_type=request.task_type,
                resolved_profile=None,
                requirements=reqs,
                routing_decision=None,
                selected_route=None,
                policy_id=active_task_policy.policy_id,
                policy_version=active_task_policy.version,
                reason_codes=("UNKNOWN_TASK_OR_NO_PROFILE", "NO_DEFAULT_MODEL_ASSIGNED"),
                deterministic_rationale=f"Task '{request.task_type}' has no configured profile in policy '{active_task_policy.policy_id}' v{active_task_policy.version}. Preserving explicit uncertainty without arbitrary model assignment.",
            )

        # 2. Resolución de requerimientos
        reqs = self.resolve_requirements(request, policy=active_task_policy)

        # 3. Transformación a RoutingRequest de M.1
        m1_routing_request = reqs.to_m1_routing_request()

        # 4. Delegación a la estrategia de routing de M.1
        routing_decision = self._routing_strategy.route(
            request=m1_routing_request,
            available_routes=available_routes,
            policy=routing_policy,
        )

        # 5. Ensamblar resultado según decisión de M.1
        reason_codes = list(reqs.reason_codes)
        reason_codes.append(f"ROUTING_STATUS:{routing_decision.status.value}")

        if routing_decision.is_selected:
            status = SelectionStatus.SUCCESS
            selected_route = routing_decision.selected_route
            rationale = (
                f"Successfully selected route '{selected_route.route_id}' ({selected_route.model_id}) "
                f"for task '{request.task_type}' with complexity '{reqs.complexity.value}', "
                f"criticality '{reqs.criticality.value}', min quality '{reqs.min_quality.value}'. "
                f"M.1 rationale: {routing_decision.deterministic_rationale}"
            )
        else:
            status = SelectionStatus.ROUTING_FAILED
            selected_route = None
            rationale = (
                f"Routing failed for task '{request.task_type}' under policy '{active_task_policy.policy_id}' v{active_task_policy.version}. "
                f"M.1 status: {routing_decision.status.value}. "
                f"M.1 rationale: {routing_decision.deterministic_rationale}"
            )

        return ModelSelectionResult(
            status=status,
            task_type=request.task_type,
            resolved_profile=profile,
            requirements=reqs,
            routing_decision=routing_decision,
            selected_route=selected_route,
            policy_id=active_task_policy.policy_id,
            policy_version=active_task_policy.version,
            reason_codes=tuple(reason_codes),
            deterministic_rationale=rationale,
        )
