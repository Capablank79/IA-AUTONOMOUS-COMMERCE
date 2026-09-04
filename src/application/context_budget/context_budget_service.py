"""
Servicio de Presupuesto de Contexto (Context Budgeting Service - Hito M.2).

Transversal M — Control de Coste e Inferencia.

Responsabilidades de M.2:
- Resolver capacidad y context_window del modelo/ruta (reutilizando M.1 ModelRoute o RoutingDecision).
- Determinar o estimar tokens de entrada (requested_input_tokens o input_breakdown).
- Reservar capacidad explícita para output (reserved_output_tokens).
- Aplicar margen de seguridad (safety_margin_tokens).
- Calcular aritméticamente con enteros:
    available_input = context_window - reserved_output - safety_margin
    requested_input <= available_input -> WITHIN_BUDGET
    requested_input > available_input -> OVER_BUDGET
- Manejar casos UNKNOWN (context window desconocido o token estimate desconocido) -> UNKNOWN, NUNCA WITHIN_BUDGET.
- Rechazar valores negativos o inválidos con ERROR / OVER_BUDGET estructurado.
- NO modificar prompt, NO truncar, NO comprimir (M.3), NO cachear (M.4), NO seleccionar modelo económico (M.5/M.6).
- Garantizar determinismo y reproducibilidad matemática.
"""

from typing import Optional, Union, Tuple

from src.domain.context_budget.models import (
    ContextBudgetStatus,
    BudgetExclusionReason,
    InputTokensBreakdown,
    ContextBudgetPolicy,
    ContextBudgetRequest,
    ContextBudgetDecision,
)
from src.domain.context_budget.ports import (
    TokenEstimatorPort,
    ContextBudgetServicePort,
)
from src.domain.model_routing.models import ModelRoute, RoutingDecision, RoutingDecisionStatus
from src.domain.model_routing.ports import ModelRouteRegistryPort


class ContextBudgetService(ContextBudgetServicePort):
    """
    Implementación del servicio de evaluación de presupuesto de contexto M.2.
    """

    def __init__(
        self,
        route_registry: Optional[ModelRouteRegistryPort] = None,
        token_estimator: Optional[TokenEstimatorPort] = None,
        default_policy: Optional[ContextBudgetPolicy] = None,
    ):
        self._route_registry = route_registry
        self._token_estimator = token_estimator
        self._default_policy = default_policy or ContextBudgetPolicy(
            policy_id="default_deterministic_m2_policy",
            version="1.0.0",
            default_reserved_output_tokens=1024,
            safety_margin_tokens=256,
        )

    def _resolve_model_route(
        self, route_ref: Union[ModelRoute, RoutingDecision, str]
    ) -> Tuple[Optional[ModelRoute], Optional[str], Optional[BudgetExclusionReason], str]:
        """
        Resuelve la ModelRoute a partir de una entidad ModelRoute, una RoutingDecision o un route_id string.
        """
        if isinstance(route_ref, ModelRoute):
            return route_ref, route_ref.route_id, None, ""

        if isinstance(route_ref, RoutingDecision):
            if route_ref.status != RoutingDecisionStatus.SELECTED or route_ref.selected_route is None:
                return (
                    None,
                    None,
                    BudgetExclusionReason.MODEL_CONTEXT_UNKNOWN,
                    f"RoutingDecision has status '{route_ref.status.value}' without a selected route",
                )
            return route_ref.selected_route, route_ref.selected_route.route_id, None, ""

        if isinstance(route_ref, str):
            if not route_ref.strip():
                return None, None, BudgetExclusionReason.INVALID_PARAMETERS, "route_id cannot be empty"
            if self._route_registry is not None:
                resolved = self._route_registry.get_route(route_ref.strip())
                if resolved is not None:
                    return resolved, resolved.route_id, None, ""
            return (
                None,
                route_ref.strip(),
                BudgetExclusionReason.MODEL_CONTEXT_UNKNOWN,
                f"Route '{route_ref}' not found in registry",
            )

        return None, None, BudgetExclusionReason.INVALID_PARAMETERS, f"Unsupported route reference type: {type(route_ref)}"

    def assess_budget(
        self,
        request: ContextBudgetRequest,
        policy: Optional[ContextBudgetPolicy] = None,
    ) -> ContextBudgetDecision:
        """
        Evalúa el presupuesto de contexto de forma estrictamente determinista y entera.
        """
        active_policy = policy or self._default_policy

        # 1. Resolver política de reservas
        reserved_output = (
            request.reserved_output_tokens
            if request.reserved_output_tokens is not None
            else active_policy.default_reserved_output_tokens
        )
        safety_margin = (
            request.safety_margin_tokens
            if request.safety_margin_tokens is not None
            else active_policy.safety_margin_tokens
        )

        # 2. Resolver la ruta
        model_route, route_id, err_reason, err_msg = self._resolve_model_route(request.route)
        if err_reason is not None or model_route is None:
            return ContextBudgetDecision(
                status=ContextBudgetStatus.UNKNOWN if err_reason == BudgetExclusionReason.MODEL_CONTEXT_UNKNOWN else ContextBudgetStatus.ERROR,
                route_id=route_id,
                model_id=None,
                context_window=None,
                requested_input_tokens=request.requested_input_tokens,
                reserved_output_tokens=reserved_output,
                safety_margin_tokens=safety_margin,
                available_input_tokens=None,
                estimated_total_tokens=None,
                reason_code=err_reason,
                rationale=err_msg,
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                input_breakdown=request.input_breakdown,
            )

        model_id = model_route.model_id
        context_window = model_route.context_window

        # 3. Validar context_window
        if context_window is None or context_window <= 0:
            return ContextBudgetDecision(
                status=ContextBudgetStatus.UNKNOWN,
                route_id=model_route.route_id,
                model_id=model_id,
                context_window=None,
                requested_input_tokens=request.requested_input_tokens,
                reserved_output_tokens=reserved_output,
                safety_margin_tokens=safety_margin,
                available_input_tokens=None,
                estimated_total_tokens=None,
                reason_code=BudgetExclusionReason.MODEL_CONTEXT_UNKNOWN,
                rationale=f"Context window is unknown for route '{model_route.route_id}' (model '{model_id}')",
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                input_breakdown=request.input_breakdown,
            )

        # 4. Determinar input tokens solicitados
        input_tokens = request.requested_input_tokens
        breakdown = request.input_breakdown

        if input_tokens is None:
            if breakdown is not None:
                input_tokens = breakdown.total_input_tokens
            else:
                # No se proveyó conteo directo ni breakdown
                return ContextBudgetDecision(
                    status=ContextBudgetStatus.UNKNOWN,
                    route_id=model_route.route_id,
                    model_id=model_id,
                    context_window=context_window,
                    requested_input_tokens=None,
                    reserved_output_tokens=reserved_output,
                    safety_margin_tokens=safety_margin,
                    available_input_tokens=None,
                    estimated_total_tokens=None,
                    reason_code=BudgetExclusionReason.TOKEN_ESTIMATE_UNKNOWN,
                    rationale="Input token count is unknown (no requested_input_tokens or breakdown provided)",
                    policy_id=active_policy.policy_id,
                    policy_version=active_policy.version,
                    input_breakdown=None,
                )

        # 5. Aritmética de presupuesto canónica (enteros, no floats)
        # available_input = context_window - reserved_output - safety_margin
        available_input = context_window - reserved_output - safety_margin
        estimated_total = input_tokens + reserved_output + safety_margin

        # Caso 5.1: La suma de reserved_output + safety_margin supera o agota el context_window
        if available_input < 0:
            return ContextBudgetDecision(
                status=ContextBudgetStatus.OVER_BUDGET,
                route_id=model_route.route_id,
                model_id=model_id,
                context_window=context_window,
                requested_input_tokens=input_tokens,
                reserved_output_tokens=reserved_output,
                safety_margin_tokens=safety_margin,
                available_input_tokens=available_input,
                estimated_total_tokens=estimated_total,
                reason_code=BudgetExclusionReason.OUTPUT_RESERVATION_EXCEEDED,
                rationale=(
                    f"Reserved output ({reserved_output}) + safety margin ({safety_margin}) = "
                    f"{reserved_output + safety_margin} exceeds total context window ({context_window})"
                ),
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                input_breakdown=breakdown,
            )

        # Caso 5.2: requested_input > available_input -> OVER_BUDGET
        if input_tokens > available_input:
            return ContextBudgetDecision(
                status=ContextBudgetStatus.OVER_BUDGET,
                route_id=model_route.route_id,
                model_id=model_id,
                context_window=context_window,
                requested_input_tokens=input_tokens,
                reserved_output_tokens=reserved_output,
                safety_margin_tokens=safety_margin,
                available_input_tokens=available_input,
                estimated_total_tokens=estimated_total,
                reason_code=BudgetExclusionReason.INPUT_TOO_LARGE,
                rationale=(
                    f"Requested input ({input_tokens} tokens) exceeds available input budget "
                    f"({available_input} tokens) for context window {context_window}"
                ),
                policy_id=active_policy.policy_id,
                policy_version=active_policy.version,
                input_breakdown=breakdown,
            )

        # Caso 5.3: requested_input <= available_input -> WITHIN_BUDGET
        return ContextBudgetDecision(
            status=ContextBudgetStatus.WITHIN_BUDGET,
            route_id=model_route.route_id,
            model_id=model_id,
            context_window=context_window,
            requested_input_tokens=input_tokens,
            reserved_output_tokens=reserved_output,
            safety_margin_tokens=safety_margin,
            available_input_tokens=available_input,
            estimated_total_tokens=estimated_total,
            reason_code=None,
            rationale=(
                f"Requested input ({input_tokens} tokens) is within budget. "
                f"Available input: {available_input} tokens (Context window: {context_window}, "
                f"Reserved output: {reserved_output}, Safety margin: {safety_margin})"
            ),
            policy_id=active_policy.policy_id,
            policy_version=active_policy.version,
            input_breakdown=breakdown,
        )
