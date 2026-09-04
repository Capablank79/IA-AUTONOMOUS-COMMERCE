"""
Implementación del Servicio de Estrategia de Enrutamiento de Modelos (M.1).

Transversal M — Control de Coste e Inferencia.

Implementa ModelRoutingStrategyPort de manera 100% determinista, reproducible y segura.

Pasos del Enrutamiento M.1:
1. Validación de entrada (Request & Policy).
2. Obtención de rutas (desde argumento o desde RegistryPort).
3. Filtrado de disponibilidad (Available vs Degraded vs Unavailable/Unknown).
4. Filtrado de capacidades requeridas (Capability Filtering).
5. Filtrado de restricciones de proveedor (Allowed / Preferred).
6. Filtrado de calidad y criticidad (Quality / Criticality constraints).
7. Filtrado de latencia (Latency constraints).
8. Filtrado de costo límite si aplica (Cost ceiling opcional sin optimización M.2/M.6).
9. Ordenamiento determinista multi-criterio:
   - Preferencia de proveedor explícito
   - Prioridad base de ruta (priority asc)
   - Calidad (en orden descendente)
   - Tie-breaking determinista y lexicográfico por route_id
10. Fallback explícito: Si la primera ruta preferida no está disponible, se selecciona la siguiente elegible documentando el fallback.
11. Emisión de RoutingDecision inmutable con rationale estructurado y checksum.
"""

from decimal import Decimal
from typing import List, Optional, Sequence, Tuple, Dict, Any
from datetime import datetime, timezone

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
)
from src.domain.model_routing.ports import (
    ModelRouteRegistryPort,
    ModelRoutingStrategyPort,
)


QUALITY_ORDER = {
    QualityRequirement.ANY: 0,
    QualityRequirement.STANDARD: 1,
    QualityRequirement.HIGH: 2,
    QualityRequirement.SUPERIOR: 3,
}

LATENCY_ORDER = {
    LatencyRequirement.REAL_TIME: 1,
    LatencyRequirement.LOW_LATENCY: 2,
    LatencyRequirement.NORMAL: 3,
    LatencyRequirement.ANY: 4,
}

CRITICALITY_MIN_QUALITY = {
    TaskCriticality.LOW: QualityRequirement.ANY,
    TaskCriticality.MEDIUM: QualityRequirement.STANDARD,
    TaskCriticality.HIGH: QualityRequirement.HIGH,
    TaskCriticality.CRITICAL: QualityRequirement.SUPERIOR,
}


class DeterministicModelRoutingStrategy(ModelRoutingStrategyPort):
    """
    Estrategia determinista de enrutamiento de modelos para M.1.
    """

    def __init__(self, registry: Optional[ModelRouteRegistryPort] = None):
        self._registry = registry

    def route(
        self,
        request: RoutingRequest,
        available_routes: Optional[Sequence[ModelRoute]] = None,
        policy: Optional[RoutingPolicy] = None,
    ) -> RoutingDecision:
        """
        Ejecuta el pipeline de filtrado, ordenamiento y fallback determinista.
        """
        active_policy = policy or RoutingPolicy(policy_id="default_deterministic_v1", version="1.0.0")

        # 1. Resolver rutas candidatas
        candidates: Sequence[ModelRoute] = []
        if available_routes is not None:
            candidates = available_routes
        elif self._registry is not None:
            candidates = self._registry.list_routes()

        if not candidates:
            return RoutingDecision(
                status=RoutingDecisionStatus.NO_ROUTE,
                selected_route=None,
                eligible_routes=(),
                excluded_routes=(),
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                deterministic_rationale="No candidate routes provided or registered in registry",
            )

        eligible: List[ModelRoute] = []
        excluded: List[ExclusionRecord] = []

        # Determinar nivel de calidad mínimo requerido (intersección request vs criticidad)
        min_quality_req = request.min_quality
        if active_policy.strict_criticality_filter:
            crit_quality = CRITICALITY_MIN_QUALITY.get(request.criticality, QualityRequirement.STANDARD)
            if QUALITY_ORDER[crit_quality] > QUALITY_ORDER[min_quality_req]:
                min_quality_req = crit_quality

        # 2. Pipeline de Filtrado
        for route in candidates:
            # A. Disponibilidad operativa
            if route.status == RouteStatus.UNAVAILABLE:
                excluded.append(
                    ExclusionRecord(
                        route_id=route.route_id,
                        reason_code=RouteExclusionReason.UNAVAILABLE,
                        message=f"Route '{route.route_id}' is marked as UNAVAILABLE",
                    )
                )
                continue

            if route.status == RouteStatus.UNKNOWN:
                excluded.append(
                    ExclusionRecord(
                        route_id=route.route_id,
                        reason_code=RouteExclusionReason.UNKNOWN_STATUS,
                        message=f"Route '{route.route_id}' status is UNKNOWN (uncertain health)",
                    )
                )
                continue

            if route.status == RouteStatus.DEGRADED and not active_policy.allow_degraded_fallback:
                excluded.append(
                    ExclusionRecord(
                        route_id=route.route_id,
                        reason_code=RouteExclusionReason.UNAVAILABLE,
                        message=f"Route '{route.route_id}' is DEGRADED and policy disallows degraded routes",
                    )
                )
                continue

            # B. Proveedor permitido (Allowed Providers whitelist)
            if request.allowed_providers:
                if route.provider.lower() not in request.allowed_providers:
                    excluded.append(
                        ExclusionRecord(
                            route_id=route.route_id,
                            reason_code=RouteExclusionReason.PROVIDER_NOT_ALLOWED,
                            message=f"Provider '{route.provider}' not in allowed_providers {request.allowed_providers}",
                        )
                    )
                    continue

            # C. Filtrado de Capacidades (Capability Filtering)
            missing_caps = [cap for cap in request.required_capabilities if not route.has_capability(cap)]
            if missing_caps:
                excluded.append(
                    ExclusionRecord(
                        route_id=route.route_id,
                        reason_code=RouteExclusionReason.MISSING_CAPABILITY,
                        message=f"Route '{route.route_id}' missing required capabilities: {[c.value for c in missing_caps]}",
                    )
                )
                continue

            # D. Filtrado de Calidad / Criticidad
            if QUALITY_ORDER[route.quality_class] < QUALITY_ORDER[min_quality_req]:
                excluded.append(
                    ExclusionRecord(
                        route_id=route.route_id,
                        reason_code=RouteExclusionReason.INSUFFICIENT_QUALITY,
                        message=f"Route quality '{route.quality_class.value}' below required threshold '{min_quality_req.value}'",
                    )
                )
                continue

            # E. Filtrado de Latencia
            if request.max_latency != LatencyRequirement.ANY:
                # Si la ruta tiene una latencia más lenta que la requerida
                if LATENCY_ORDER[route.latency_class] > LATENCY_ORDER[request.max_latency]:
                    excluded.append(
                        ExclusionRecord(
                            route_id=route.route_id,
                            reason_code=RouteExclusionReason.LATENCY_TOO_HIGH,
                            message=f"Route latency '{route.latency_class.value}' exceeds requirement '{request.max_latency.value}'",
                        )
                    )
                    continue

            # F. Filtrado de Techo de Coste opcional (sin optimizaciones M.2/M.6)
            if request.cost_ceiling_per_call is not None:
                route_flat_cost = route.flat_cost_per_request or Decimal("0.00")
                if route_flat_cost > request.cost_ceiling_per_call:
                    excluded.append(
                        ExclusionRecord(
                            route_id=route.route_id,
                            reason_code=RouteExclusionReason.COST_CEILING_EXCEEDED,
                            message=f"Route flat cost {route_flat_cost} exceeds ceiling {request.cost_ceiling_per_call}",
                        )
                    )
                    continue

            # Si pasa todos los filtros, es elegible
            eligible.append(route)

        if not eligible:
            return RoutingDecision(
                status=RoutingDecisionStatus.NO_ROUTE,
                selected_route=None,
                eligible_routes=(),
                excluded_routes=tuple(excluded),
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                deterministic_rationale=f"0 of {len(candidates)} candidate routes matched requirements",
            )

        # 3. Ordenamiento determinista multi-criterio
        # Clave de ordenamiento determinista:
        # 1. Preferred provider index (0 si está en preferred, 1 si no)
        # 2. Priority asc (menor número = más prioritario)
        # 3. Quality desc (mayor calidad = más prioritario)
        # 4. Route ID asc (desempate determinista lexicográfico)
        def sort_key(r: ModelRoute):
            is_preferred = 0 if r.provider.lower() in request.preferred_providers else 1
            quality_rank = -QUALITY_ORDER[r.quality_class]
            return (is_preferred, r.priority, quality_rank, r.route_id)

        sorted_eligible = sorted(eligible, key=sort_key)
        selected = sorted_eligible[0]

        # Ordenar candidatos inicialmente de forma canónica por (preferred, priority, -quality, route_id)
        # para que la detección de fallback no dependa del orden arbitrario en que la lista fue pasada.
        fallback_applied = False
        canonical_candidates = sorted(candidates, key=sort_key)
        # Si la ruta teóricamente más preferida entre todos los candidatos no es la seleccionada (ej. por exclusión/unavailability)
        if canonical_candidates and canonical_candidates[0].route_id != selected.route_id:
            fallback_applied = True

        rationale = (
            f"Selected route '{selected.route_id}' ({selected.provider}/{selected.model_id}) "
            f"with quality '{selected.quality_class.value}', priority {selected.priority}. "
            f"Eligible: {len(sorted_eligible)}, Excluded: {len(excluded)}."
        )

        return RoutingDecision(
            status=RoutingDecisionStatus.SELECTED,
            selected_route=selected,
            eligible_routes=tuple(sorted_eligible),
            excluded_routes=tuple(excluded),
            policy_id=active_policy.policy_id,
            policy_version=active_policy.version,
            deterministic_rationale=rationale,
            fallback_applied=fallback_applied,
        )
