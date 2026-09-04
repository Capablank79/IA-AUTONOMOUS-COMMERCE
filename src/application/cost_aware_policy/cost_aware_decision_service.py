"""
Servicio de Aplicación para la Política de Decisión Consciente del Coste (Cost-aware Decision Policy - Hito M.6).

Transversal M — Control de Coste e Inferencia (Hito M.6).

M.6 responde:
"Entre opciones técnicamente válidas, ¿qué decisión de inferencia cumple la política de coste sin violar
requisitos mínimos de calidad, capacidad o criticidad?"

Principios M.6 aplicados en el servicio:
1. Quality First:
   - Filtra primero capacidades requeridas (M.1 / M.5).
   - Filtra piso mínimo de calidad (quality_floor y min_quality).
   - Valida adecuación a criticidad (restringe degradaciones o costes desconocidos para HIGH/CRITICAL).
   - NUNCA selecciona una ruta no válida o incapaz por ser barata.
2. Estimación con tarifas K.3:
   - Consulta tarifas vigentes vía PricingCatalogPort (o usa estimaciones provistas en ModelRoute si no hay catálogo).
   - Si no hay pricing determinable: el coste es UNKNOWN (UNKNOWN != 0.00).
   - Precisión matemática estricta en Decimal.
3. Cache Impact (M.4):
   - Si cache_hit es True: la inferencia se evita (coste incremental 0.00) y se documenta la decisión con CACHE_HIT_AVOIDED.
4. Presupuesto y Cost Ceiling:
   - Evalúa límites por inferencia, por clase de tarea y por misión configurados en CostAwarePolicy o request.budget_ceiling.
   - Si todas las rutas válidas exceden el presupuesto: estado REJECTED / NO_ELIGIBLE_OPTION con razón EXCEEDS_BUDGET.
5. Determinismo y Tie-Break:
   - Ordena candidatos válidos determinísticamente:
     a) Cumplimiento estricto de requisitos.
     b) Dentro de presupuesto.
     c) Menor coste total estimado.
     d) Mayor calidad (quality_class).
     e) Menor latencia (latency_class).
     f) Mayor prioridad base (priority menor).
     g) Tie-break determinista lexicográfico por route_id.
6. Aislamiento y trazabilidad:
   - Emite CostAwareDecision con razón estructurada, resumen de estimaciones por ruta y hash SHA-256 canónico.
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Dict, List, Optional, Sequence, Tuple, Union

from src.domain.cost_aware_policy.models import (
    CostAwareDecision,
    CostAwareDecisionStatus,
    CostAwarePolicy,
    CostAwareReasonCode,
    CostAwareRequest,
    RouteCostEstimate,
)
from src.domain.model_routing.models import (
    LatencyRequirement,
    ModelRoute,
    QualityRequirement,
    RouteCapability,
    RouteStatus,
    TaskCriticality,
)
from src.domain.cost.models import CostType, PricingRate, UsageUnit
from src.domain.cost.ports import PricingCatalogPort
from src.application.cost.pricing_catalog import get_default_pricing_catalog

logger = logging.getLogger(__name__)

# Jerarquía ordenada para comparación determinista de calidad
_QUALITY_ORDER = {
    QualityRequirement.ANY: 0,
    QualityRequirement.STANDARD: 1,
    QualityRequirement.HIGH: 2,
    QualityRequirement.SUPERIOR: 3,
}

# Jerarquía ordenada para comparación determinista de latencia (menor número = más rápido/estricto)
_LATENCY_ORDER = {
    LatencyRequirement.REAL_TIME: 1,
    LatencyRequirement.LOW_LATENCY: 2,
    LatencyRequirement.NORMAL: 3,
    LatencyRequirement.ANY: 4,
}


def _is_quality_satisfactory(route_quality: QualityRequirement, min_required: QualityRequirement) -> bool:
    """Verifica si la calidad de la ruta cumple o supera el mínimo requerido."""
    return _QUALITY_ORDER.get(route_quality, 0) >= _QUALITY_ORDER.get(min_required, 0)


def _is_latency_satisfactory(route_latency: LatencyRequirement, max_allowed: LatencyRequirement) -> bool:
    """Verifica si la latencia de la ruta es igual o más rápida que la máxima permitida."""
    if max_allowed == LatencyRequirement.ANY:
        return True
    return _LATENCY_ORDER.get(route_latency, 3) <= _LATENCY_ORDER.get(max_allowed, 4)


class CostAwareDecisionService:
    """
    Servicio de aplicación para evaluar y emitir decisiones conscientes del coste (M.6).
    """

    def __init__(
        self,
        pricing_catalog: Optional[PricingCatalogPort] = None,
        default_policy: Optional[CostAwarePolicy] = None,
        isolate_failures: bool = True,
    ):
        self.pricing_catalog = pricing_catalog or get_default_pricing_catalog()
        self.default_policy = default_policy or CostAwarePolicy(
            policy_id="default_cost_policy",
            version="1.0.0",
            prefer_cheapest_among_valid=True,
            enforce_strict_budget=True,
            quality_floor=QualityRequirement.STANDARD,
            allow_unknown_cost_for_critical=False,
            allow_degraded_for_critical=False,
        )
        self.isolate_failures = isolate_failures

    def evaluate(
        self,
        request: CostAwareRequest,
        policy_override: Optional[CostAwarePolicy] = None,
    ) -> CostAwareDecision:
        """
        Evalúa determinísticamente las rutas candidatas y emite una decisión consciente de coste.
        """
        try:
            return self._execute_evaluation(request=request, policy_override=policy_override)
        except Exception as ex:
            logger.exception("Error executing cost-aware decision evaluation: %s", ex)
            if not self.isolate_failures:
                raise
            return CostAwareDecision(
                status=CostAwareDecisionStatus.ERROR,
                selected_route=None,
                estimated_cost=None,
                currency=request.currency,
                budget_ceiling=request.budget_ceiling,
                task_type=request.task_type,
                mission_id=request.mission_id,
                task_id=request.task_id,
                execution_id=request.execution_id,
                eligible_routes=request.eligible_routes,
                route_estimates=(),
                cache_impact_avoided=False,
                policy_id=(policy_override or request.policy or self.default_policy).policy_id,
                policy_version=(policy_override or request.policy or self.default_policy).version,
                reason_codes=(CostAwareReasonCode.EVALUATION_ERROR.value,),
                deterministic_rationale=f"Evaluation failed with error: {str(ex)}",
            )

    def _execute_evaluation(
        self,
        request: CostAwareRequest,
        policy_override: Optional[CostAwarePolicy] = None,
    ) -> CostAwareDecision:
        active_policy = policy_override or request.policy or self.default_policy

        # 1. Validar política de divisa permitida
        req_currency = request.currency.upper()
        if active_policy.allowed_currencies and req_currency not in active_policy.allowed_currencies:
            return CostAwareDecision(
                status=CostAwareDecisionStatus.REJECTED,
                selected_route=None,
                estimated_cost=None,
                currency=req_currency,
                budget_ceiling=request.budget_ceiling,
                task_type=request.task_type,
                mission_id=request.mission_id,
                task_id=request.task_id,
                execution_id=request.execution_id,
                eligible_routes=request.eligible_routes,
                route_estimates=(),
                cache_impact_avoided=False,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                reason_codes=(CostAwareReasonCode.CURRENCY_DISALLOWED.value,),
                deterministic_rationale=f"Requested currency '{req_currency}' is not in policy allowed currencies: {active_policy.allowed_currencies}",
            )

        # 2. Determinar techo de presupuesto efectivo (Budget Ceiling)
        effective_budget: Optional[Decimal] = request.budget_ceiling
        if effective_budget is None and active_policy.max_cost_per_inference is not None:
            effective_budget = active_policy.max_cost_per_inference
        if request.task_type in active_policy.max_cost_by_task_class:
            task_limit = active_policy.max_cost_by_task_class[request.task_type]
            if effective_budget is None or task_limit < effective_budget:
                effective_budget = task_limit

        # 3. Caso especial: CACHE HIT confirmado (M.4)
        if request.cache_hit:
            # Si hay cache HIT, la inferencia se evita por completo.
            # Coste incremental = 0.00
            # Si hay rutas provistas, seleccionamos la primera disponible o de referencia si existe
            first_route = request.eligible_routes[0] if request.eligible_routes else None
            return CostAwareDecision(
                status=CostAwareDecisionStatus.APPROVED,
                selected_route=first_route,
                estimated_cost=Decimal("0.00"),
                currency=req_currency,
                budget_ceiling=effective_budget,
                task_type=request.task_type,
                mission_id=request.mission_id,
                task_id=request.task_id,
                execution_id=request.execution_id,
                eligible_routes=request.eligible_routes,
                route_estimates=(),
                cache_impact_avoided=True,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                reason_codes=(CostAwareReasonCode.CACHE_HIT_AVOIDED.value, CostAwareReasonCode.WITHIN_BUDGET.value),
                deterministic_rationale="Inference avoided due to confirmed cache HIT. Incremental estimated inference cost is 0.00.",
            )

        # 4. Si no hay rutas candidatas
        if not request.eligible_routes:
            return CostAwareDecision(
                status=CostAwareDecisionStatus.NO_ELIGIBLE_OPTION,
                selected_route=None,
                estimated_cost=None,
                currency=req_currency,
                budget_ceiling=effective_budget,
                task_type=request.task_type,
                mission_id=request.mission_id,
                task_id=request.task_id,
                execution_id=request.execution_id,
                eligible_routes=(),
                route_estimates=(),
                cache_impact_avoided=False,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                reason_codes=(CostAwareReasonCode.NO_ELIGIBLE_ROUTES.value,),
                deterministic_rationale="No candidate routes provided for cost-aware evaluation.",
            )

        # 5. Evaluar cada ruta individualmente (Estimación de coste + Quality First filtering)
        estimates: List[RouteCostEstimate] = []
        for route in request.eligible_routes:
            est = self._estimate_route_cost(
                route=route,
                request=request,
                policy=active_policy,
                effective_budget=effective_budget,
            )
            estimates.append(est)

        # 6. Filtrar opciones válidas (Quality First y Capacidades)
        technically_valid = [e for e in estimates if e.is_technically_eligible]

        if not technically_valid:
            # Ninguna ruta cumplió los requisitos técnicos/calidad/criticidad
            primary_reasons = []
            for e in estimates:
                primary_reasons.extend(e.exclusion_reasons)
            primary_reasons = list(dict.fromkeys(primary_reasons))  # dedup preservando orden
            return CostAwareDecision(
                status=CostAwareDecisionStatus.NO_ELIGIBLE_OPTION,
                selected_route=None,
                estimated_cost=None,
                currency=req_currency,
                budget_ceiling=effective_budget,
                task_type=request.task_type,
                mission_id=request.mission_id,
                task_id=request.task_id,
                execution_id=request.execution_id,
                eligible_routes=request.eligible_routes,
                route_estimates=tuple(estimates),
                cache_impact_avoided=False,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                reason_codes=tuple(primary_reasons or [CostAwareReasonCode.CAPABILITY_UNMET.value]),
                deterministic_rationale="All candidate routes failed technical eligibility, capability or quality requirements.",
            )

        # 7. Manejo de UNKNOWN Cost para tareas de alta criticidad
        is_critical_task = request.criticality in (TaskCriticality.HIGH, TaskCriticality.CRITICAL)
        if is_critical_task and not active_policy.allow_unknown_cost_for_critical:
            known_technically_valid = [e for e in technically_valid if e.is_known]
            if not known_technically_valid:
                return CostAwareDecision(
                    status=CostAwareDecisionStatus.UNKNOWN,
                    selected_route=None,
                    estimated_cost=None,
                    currency=req_currency,
                    budget_ceiling=effective_budget,
                    task_type=request.task_type,
                    mission_id=request.mission_id,
                    task_id=request.task_id,
                    execution_id=request.execution_id,
                    eligible_routes=request.eligible_routes,
                    route_estimates=tuple(estimates),
                    cache_impact_avoided=False,
                    policy_id=active_policy.policy_id,
                    policy_version=active_policy.version,
                    reason_codes=(CostAwareReasonCode.UNKNOWN_COST.value, CostAwareReasonCode.CRITICALITY_REJECTED.value),
                    deterministic_rationale="Critical task requires known cost calculation. Pricing information is missing or unknown for all valid routes.",
                )
            technically_valid = known_technically_valid

        # 8. Filtrar por presupuesto (Budget Ceiling)
        within_budget = [e for e in technically_valid if e.is_within_budget]

        if not within_budget:
            # Todas las rutas técnicamente válidas exceden el presupuesto
            return CostAwareDecision(
                status=CostAwareDecisionStatus.REJECTED,
                selected_route=None,
                estimated_cost=technically_valid[0].estimated_total_cost,
                currency=req_currency,
                budget_ceiling=effective_budget,
                task_type=request.task_type,
                mission_id=request.mission_id,
                task_id=request.task_id,
                execution_id=request.execution_id,
                eligible_routes=request.eligible_routes,
                route_estimates=tuple(estimates),
                cache_impact_avoided=False,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                reason_codes=(CostAwareReasonCode.EXCEEDS_BUDGET.value,),
                deterministic_rationale=f"All technically valid routes exceed the budget ceiling of {effective_budget} {req_currency}.",
            )

        # 9. Selección y Desempate Determinista entre rutas válidas y dentro de presupuesto
        selected_estimate = self._select_best_route(within_budget, active_policy)

        # Encontrar el objeto ModelRoute correspondiente
        selected_model_route: Optional[ModelRoute] = None
        for r in request.eligible_routes:
            if r.route_id == selected_estimate.route_id:
                selected_model_route = r
                break

        reasons = [CostAwareReasonCode.WITHIN_BUDGET.value]
        if active_policy.prefer_cheapest_among_valid:
            reasons.append(CostAwareReasonCode.CHEAPEST_VALID_SELECTED.value)
        if len(within_budget) > 1:
            reasons.append(CostAwareReasonCode.TIE_BREAK_DETERMINISTIC.value)

        rationale = (
            f"Selected route '{selected_estimate.route_id}' ({selected_estimate.provider}:{selected_estimate.model_id}) "
            f"satisfying quality/capabilities with estimated cost {selected_estimate.estimated_total_cost} {req_currency} "
            f"(budget ceiling: {effective_budget or 'UNLIMITED'} {req_currency})."
        )

        return CostAwareDecision(
            status=CostAwareDecisionStatus.APPROVED,
            selected_route=selected_model_route,
            estimated_cost=selected_estimate.estimated_total_cost,
            currency=req_currency,
            budget_ceiling=effective_budget,
            task_type=request.task_type,
            mission_id=request.mission_id,
            task_id=request.task_id,
            execution_id=request.execution_id,
            eligible_routes=request.eligible_routes,
            route_estimates=tuple(estimates),
            cache_impact_avoided=False,
            policy_id=active_policy.policy_id,
            policy_version=active_policy.version,
            reason_codes=tuple(reasons),
            deterministic_rationale=rationale,
        )

    def _estimate_route_cost(
        self,
        route: ModelRoute,
        request: CostAwareRequest,
        policy: CostAwarePolicy,
        effective_budget: Optional[Decimal],
    ) -> RouteCostEstimate:
        """
        Calcula la estimación de coste para una ruta y valida sus capacidades técnicas y calidad.
        """
        exclusion_reasons: List[str] = []

        # 1. Validar disponibilidad operativa
        if route.status == RouteStatus.UNAVAILABLE:
            exclusion_reasons.append("UNAVAILABLE")
        elif route.status == RouteStatus.UNKNOWN:
            exclusion_reasons.append("UNKNOWN_STATUS")
        elif route.status == RouteStatus.DEGRADED:
            is_critical = request.criticality in (TaskCriticality.HIGH, TaskCriticality.CRITICAL)
            if is_critical and not policy.allow_degraded_for_critical:
                exclusion_reasons.append(CostAwareReasonCode.DEGRADED_ROUTE_REJECTED.value)

        # 2. Validar capacidades técnicas requeridas (M.1 / M.5)
        if request.required_capabilities:
            if not route.has_all_capabilities(request.required_capabilities):
                exclusion_reasons.append(CostAwareReasonCode.CAPABILITY_UNMET.value)

        # 3. Validar piso de calidad (Quality First)
        effective_min_quality = request.min_quality
        if _QUALITY_ORDER.get(policy.quality_floor, 0) > _QUALITY_ORDER.get(effective_min_quality, 0):
            effective_min_quality = policy.quality_floor

        if not _is_quality_satisfactory(route.quality_class, effective_min_quality):
            exclusion_reasons.append(CostAwareReasonCode.QUALITY_UNMET.value)

        # 4. Validar latencia requerida
        if not _is_latency_satisfactory(route.latency_class, request.max_latency):
            exclusion_reasons.append("LATENCY_TOO_HIGH")

        # 5. Cálculo económico desde K.3 PricingCatalogPort o ModelRoute
        in_tokens = request.estimated_input_tokens
        out_tokens = request.estimated_output_tokens

        # Consultar tarifa en catálogo
        rate = self.pricing_catalog.get_rate(
            provider=route.provider,
            service_or_model=route.model_id,
            cost_type=CostType.INFERENCE,
        )

        in_cost: Optional[Decimal] = None
        out_cost: Optional[Decimal] = None
        flat_cost: Optional[Decimal] = None
        total_cost: Optional[Decimal] = None
        is_known = True
        pricing_source = "CATALOG"
        pricing_version = "1.0.0"

        if rate is not None:
            pricing_source = "CATALOG"
            pricing_version = rate.version
            if rate.flat_rate is not None:
                flat_cost = rate.flat_rate
                total_cost = rate.flat_rate
            else:
                scale = rate.rate_scale or Decimal("1")
                # Si tenemos tokens y rates calculamos; si falta información sobre tokens o rate pero se requiere, marcamos UNKNOWN
                if in_tokens is not None and rate.input_rate is not None:
                    in_cost = (Decimal(str(in_tokens)) / scale) * rate.input_rate
                elif rate.input_rate is not None and in_tokens is None:
                    # Tokens no estimados -> coste no computable con certeza
                    is_known = False

                if out_tokens is not None and rate.output_rate is not None:
                    out_cost = (Decimal(str(out_tokens)) / scale) * rate.output_rate
                elif rate.output_rate is not None and out_tokens is None:
                    # Output no estimado
                    pass

                if is_known:
                    tot = Decimal("0.00")
                    if in_cost is not None:
                        tot += in_cost
                    if out_cost is not None:
                        tot += out_cost
                    if in_cost is not None or out_cost is not None:
                        total_cost = tot
                    else:
                        is_known = False
        elif (
            route.estimated_cost_input_per_million is not None
            or route.estimated_cost_output_per_million is not None
            or route.flat_cost_per_request is not None
        ):
            # Fallback a pricing declarado en la ruta M.1
            pricing_source = "ROUTE_METADATA"
            if route.flat_cost_per_request is not None:
                flat_cost = route.flat_cost_per_request
                total_cost = route.flat_cost_per_request
            else:
                scale = Decimal("1000000")
                if in_tokens is not None and route.estimated_cost_input_per_million is not None:
                    in_cost = (Decimal(str(in_tokens)) / scale) * route.estimated_cost_input_per_million
                if out_tokens is not None and route.estimated_cost_output_per_million is not None:
                    out_cost = (Decimal(str(out_tokens)) / scale) * route.estimated_cost_output_per_million

                tot = Decimal("0.00")
                has_comp = False
                if in_cost is not None:
                    tot += in_cost
                    has_comp = True
                if out_cost is not None:
                    tot += out_cost
                    has_comp = True
                if has_comp:
                    total_cost = tot
                else:
                    is_known = False
        else:
            # Sin tarifa disponible -> UNKNOWN
            is_known = False
            pricing_source = "UNKNOWN"

        if not is_known:
            exclusion_reasons.append(CostAwareReasonCode.UNKNOWN_PRICING.value)

        # 6. Validar presupuesto
        is_within_budget = True
        if total_cost is not None and effective_budget is not None:
            if total_cost > effective_budget:
                is_within_budget = False
                exclusion_reasons.append(CostAwareReasonCode.EXCEEDS_BUDGET.value)

        is_technically_eligible = len(
            [r for r in exclusion_reasons if r not in (CostAwareReasonCode.EXCEEDS_BUDGET.value, CostAwareReasonCode.UNKNOWN_PRICING.value)]
        ) == 0

        return RouteCostEstimate(
            route_id=route.route_id,
            provider=route.provider,
            model_id=route.model_id,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            estimated_input_cost=in_cost,
            estimated_output_cost=out_cost,
            estimated_flat_cost=flat_cost,
            estimated_total_cost=total_cost,
            currency=request.currency,
            is_known=is_known,
            pricing_source=pricing_source,
            pricing_version=pricing_version,
            is_technically_eligible=is_technically_eligible,
            is_within_budget=is_within_budget,
            exclusion_reasons=tuple(exclusion_reasons),
            quality_class=route.quality_class,
            latency_class=route.latency_class,
            priority=route.priority,
        )

    def _select_best_route(
        self,
        candidates: Sequence[RouteCostEstimate],
        policy: CostAwarePolicy,
    ) -> RouteCostEstimate:
        """
        Selecciona determinísticamente el mejor candidato según la política.
        Criterios ordenados:
        1. Coste total estimado (si prefer_cheapest_among_valid es True; si total_cost es None, se trata con máxima penalización).
        2. Calidad (mayor calidad primero).
        3. Latencia (menor latencia primero).
        4. Prioridad base (menor número primero).
        5. route_id lexicográfico (desempate determinista absoluto).
        """
        def sort_key(e: RouteCostEstimate):
            # Coste: Si total_cost es None, le asignamos un valor infinito para no preferirlo sobre costes calculados
            cost_val = e.estimated_total_cost if e.estimated_total_cost is not None else Decimal("999999999")
            if not policy.prefer_cheapest_among_valid:
                cost_val = Decimal("0")

            quality_val = -_QUALITY_ORDER.get(e.quality_class, 0)
            latency_val = _LATENCY_ORDER.get(e.latency_class, 3)
            priority_val = e.priority
            route_id_val = e.route_id

            return (cost_val, quality_val, latency_val, priority_val, route_id_val)

        sorted_candidates = sorted(candidates, key=sort_key)
        return sorted_candidates[0]
