"""
Modelos de dominio para la Política de Decisión Consciente del Coste (Cost-aware Decision Policy - Hito M.6).

Transversal M — Control de Coste e Inferencia (Hito M.6).

M.6 responde:
"Entre opciones técnicamente válidas, ¿qué decisión de inferencia cumple la política de coste sin violar
requisitos mínimos de calidad, capacidad o criticidad?"

Principios M.6:
1. Inmutabilidad estricta (frozen=True, MappingProxyType, tuplas).
2. Quality First: Preserva restricciones M.5/M.1 antes de optimizar coste.
   Una ruta sin las capacidades o calidad requerida NUNCA gana por ser barata.
3. Cost Ceiling y Presupuestos: Soporta límites por inferencia, por clase de tarea y por misión.
4. UNKNOWN != ZERO: El coste desconocido nunca se asume gratuito (UNKNOWN != 0.00).
5. Aritmética financiera en Decimal, nunca float.
6. Caching (M.4): Cache HIT confirmado evita inferencia (coste incremental 0.00) y se registra auditablemente.
7. Contexto y Compresión (M.2/M.3): Usa el conteo final de tokens tras compresión/presupuesto.
8. Distinción estricta entre ESTIMATED COST y ACTUAL COST (K.3).
9. Determinismo absoluto: Mismas entradas + misma política => Misma decisión y checksum SHA-256.
10. Sanitización estricta de secretos y exclusión total de Chain-of-Thought (CoT) o credenciales.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, List, Union

from src.domain.model_routing.models import (
    ModelRoute,
    RouteCapability,
    QualityRequirement,
    LatencyRequirement,
    TaskCriticality,
    RouteStatus,
    sanitize_routing_data,
    deep_freeze,
)
from src.domain.model_selection.models import (
    TaskComplexity,
    TaskSelectionRequirements,
    ModelSelectionResult,
)
from src.domain.context_budget.models import ContextBudgetDecision
from src.domain.prompt_compression.models import CompressionResult
from src.domain.caching.models import CacheLookupResult, CacheLookupStatus


class CostAwareDecisionStatus(str, Enum):
    """
    Estados canónicos formales de la decisión consciente del coste (M.6).
    """
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NO_ELIGIBLE_OPTION = "NO_ELIGIBLE_OPTION"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class CostAwareReasonCode(str, Enum):
    """
    Códigos canónicos estructurados de justificación para la decisión M.6.
    """
    WITHIN_BUDGET = "WITHIN_BUDGET"
    EXCEEDS_BUDGET = "EXCEEDS_BUDGET"
    NO_ELIGIBLE_ROUTES = "NO_ELIGIBLE_ROUTES"
    CAPABILITY_UNMET = "CAPABILITY_UNMET"
    QUALITY_UNMET = "QUALITY_UNMET"
    CRITICALITY_REJECTED = "CRITICALITY_REJECTED"
    UNKNOWN_COST = "UNKNOWN_COST"
    UNKNOWN_PRICING = "UNKNOWN_PRICING"
    CACHE_HIT_AVOIDED = "CACHE_HIT_AVOIDED"
    CURRENCY_DISALLOWED = "CURRENCY_DISALLOWED"
    CHEAPEST_VALID_SELECTED = "CHEAPEST_VALID_SELECTED"
    TIE_BREAK_DETERMINISTIC = "TIE_BREAK_DETERMINISTIC"
    DEGRADED_ROUTE_REJECTED = "DEGRADED_ROUTE_REJECTED"
    EVALUATION_ERROR = "EVALUATION_ERROR"


@dataclass(frozen=True)
class RouteCostEstimate:
    """
    Estimación inmutable y estructurada del coste de inferencia para una ruta candidata.
    """
    route_id: str
    provider: str
    model_id: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_input_cost: Optional[Decimal] = None
    estimated_output_cost: Optional[Decimal] = None
    estimated_flat_cost: Optional[Decimal] = None
    estimated_total_cost: Optional[Decimal] = None
    currency: str = "USD"
    is_known: bool = True
    pricing_source: str = "CATALOG"
    pricing_version: str = "1.0.0"
    is_technically_eligible: bool = True
    is_within_budget: bool = True
    exclusion_reasons: Tuple[str, ...] = field(default_factory=tuple)
    quality_class: QualityRequirement = QualityRequirement.STANDARD
    latency_class: LatencyRequirement = LatencyRequirement.NORMAL
    priority: int = 100

    def __post_init__(self):
        if not self.route_id or not self.route_id.strip():
            raise ValueError("route_id cannot be empty")
        if not self.provider or not self.provider.strip():
            raise ValueError("provider cannot be empty")
        if not self.model_id or not self.model_id.strip():
            raise ValueError("model_id cannot be empty")

        if self.estimated_input_cost is not None and not isinstance(self.estimated_input_cost, Decimal):
            object.__setattr__(self, "estimated_input_cost", Decimal(str(self.estimated_input_cost)))
        if self.estimated_output_cost is not None and not isinstance(self.estimated_output_cost, Decimal):
            object.__setattr__(self, "estimated_output_cost", Decimal(str(self.estimated_output_cost)))
        if self.estimated_flat_cost is not None and not isinstance(self.estimated_flat_cost, Decimal):
            object.__setattr__(self, "estimated_flat_cost", Decimal(str(self.estimated_flat_cost)))
        if self.estimated_total_cost is not None and not isinstance(self.estimated_total_cost, Decimal):
            object.__setattr__(self, "estimated_total_cost", Decimal(str(self.estimated_total_cost)))

        object.__setattr__(self, "exclusion_reasons", tuple(self.exclusion_reasons))


@dataclass(frozen=True)
class CostAwarePolicy:
    """
    Política declarativa, inmutable y versionada que rige la evaluación de costes y presupuestos.
    """
    policy_id: str
    version: str = "1.0.0"
    max_cost_per_inference: Optional[Decimal] = None
    max_cost_by_task_class: Mapping[str, Decimal] = field(default_factory=dict)
    max_cost_per_mission: Optional[Decimal] = None
    allowed_currencies: Tuple[str, ...] = ("USD",)
    quality_floor: QualityRequirement = QualityRequirement.STANDARD
    allow_unknown_cost_for_critical: bool = False
    allow_degraded_for_critical: bool = False
    prefer_cheapest_among_valid: bool = True
    enforce_strict_budget: bool = True
    description: str = "Deterministic cost-aware decision policy"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.policy_id or not self.policy_id.strip():
            raise ValueError("policy_id cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("version cannot be empty")

        if self.max_cost_per_inference is not None and not isinstance(self.max_cost_per_inference, Decimal):
            object.__setattr__(self, "max_cost_per_inference", Decimal(str(self.max_cost_per_inference)))
        if self.max_cost_per_mission is not None and not isinstance(self.max_cost_per_mission, Decimal):
            object.__setattr__(self, "max_cost_per_mission", Decimal(str(self.max_cost_per_mission)))

        # Normalizar mapas de costes por tarea a Decimal
        cleaned_task_costs = {}
        for k, v in self.max_cost_by_task_class.items():
            cleaned_task_costs[k] = Decimal(str(v)) if not isinstance(v, Decimal) else v
        object.__setattr__(self, "max_cost_by_task_class", MappingProxyType(cleaned_task_costs))

        object.__setattr__(self, "allowed_currencies", tuple(c.upper() for c in self.allowed_currencies))
        sanitized_meta = sanitize_routing_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    def calculate_checksum(self) -> str:
        """Calcula el checksum SHA-256 canónico de la política."""
        payload = {
            "policy_id": self.policy_id,
            "version": self.version,
            "max_cost_per_inference": str(self.max_cost_per_inference) if self.max_cost_per_inference is not None else None,
            "max_cost_by_task_class": {k: str(v) for k, v in sorted(self.max_cost_by_task_class.items())},
            "max_cost_per_mission": str(self.max_cost_per_mission) if self.max_cost_per_mission is not None else None,
            "allowed_currencies": list(self.allowed_currencies),
            "quality_floor": self.quality_floor.value,
            "allow_unknown_cost_for_critical": self.allow_unknown_cost_for_critical,
            "allow_degraded_for_critical": self.allow_degraded_for_critical,
            "prefer_cheapest_among_valid": self.prefer_cheapest_among_valid,
            "enforce_strict_budget": self.enforce_strict_budget,
        }
        canonical_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CostAwareRequest:
    """
    Solicitud inmutable para tomar una decisión de inferencia consciente del coste (M.6).
    """
    task_type: str
    complexity: Optional[Union[TaskComplexity, str]] = "STANDARD"
    criticality: TaskCriticality = TaskCriticality.MEDIUM
    min_quality: QualityRequirement = QualityRequirement.STANDARD
    max_latency: LatencyRequirement = LatencyRequirement.ANY
    required_capabilities: Tuple[RouteCapability, ...] = field(default_factory=tuple)
    eligible_routes: Tuple[ModelRoute, ...] = field(default_factory=tuple)
    estimated_input_tokens: Optional[int] = None
    estimated_output_tokens: Optional[int] = None
    budget_ceiling: Optional[Decimal] = None
    currency: str = "USD"
    mission_id: Optional[str] = None
    task_id: Optional[str] = None
    execution_id: Optional[str] = None
    cache_hit: bool = False
    compression_applied: bool = False
    policy: Optional[CostAwarePolicy] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.task_type or not self.task_type.strip():
            raise ValueError("task_type cannot be empty")
        if self.estimated_input_tokens is not None:
            if not isinstance(self.estimated_input_tokens, int) or isinstance(self.estimated_input_tokens, bool):
                raise ValueError("estimated_input_tokens must be an integer")
            if self.estimated_input_tokens < 0:
                raise ValueError("estimated_input_tokens cannot be negative")
        if self.estimated_output_tokens is not None:
            if not isinstance(self.estimated_output_tokens, int) or isinstance(self.estimated_output_tokens, bool):
                raise ValueError("estimated_output_tokens must be an integer")
            if self.estimated_output_tokens < 0:
                raise ValueError("estimated_output_tokens cannot be negative")

        if self.budget_ceiling is not None and not isinstance(self.budget_ceiling, Decimal):
            object.__setattr__(self, "budget_ceiling", Decimal(str(self.budget_ceiling)))

        caps = tuple(sorted(list(set(self.required_capabilities)), key=lambda c: c.value))
        object.__setattr__(self, "required_capabilities", caps)
        object.__setattr__(self, "eligible_routes", tuple(self.eligible_routes))
        object.__setattr__(self, "currency", self.currency.upper())

        sanitized_meta = sanitize_routing_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    @classmethod
    def from_pipeline(
        cls,
        task_type: str,
        eligible_routes: Sequence[ModelRoute],
        criticality: TaskCriticality = TaskCriticality.MEDIUM,
        min_quality: QualityRequirement = QualityRequirement.STANDARD,
        required_capabilities: Sequence[RouteCapability] = (),
        max_latency: LatencyRequirement = LatencyRequirement.ANY,
        budget_decision: Optional[ContextBudgetDecision] = None,
        compression_result: Optional[CompressionResult] = None,
        cache_result: Optional[CacheLookupResult] = None,
        budget_ceiling: Optional[Decimal] = None,
        currency: str = "USD",
        mission_id: Optional[str] = None,
        task_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        policy: Optional[CostAwarePolicy] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "CostAwareRequest":
        """
        Factory helper para construir un CostAwareRequest integrando el pipeline M.5/M.1/M.2/M.3/M.4.
        """
        # Resolver conteo final de input tokens
        input_tokens: Optional[int] = None
        compression_applied = False
        if compression_result and compression_result.final_token_count is not None:
            input_tokens = compression_result.final_token_count
            compression_applied = compression_result.tokens_saved > 0
        elif budget_decision and budget_decision.requested_input_tokens is not None:
            input_tokens = budget_decision.requested_input_tokens

        # Resolver tokens de output reservados
        output_tokens: Optional[int] = None
        if budget_decision:
            output_tokens = budget_decision.reserved_output_tokens

        # Resolver cache HIT
        cache_hit = False
        if cache_result and cache_result.status == CacheLookupStatus.HIT:
            cache_hit = True

        return cls(
            task_type=task_type,
            criticality=criticality,
            min_quality=min_quality,
            max_latency=max_latency,
            required_capabilities=tuple(required_capabilities),
            eligible_routes=tuple(eligible_routes),
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            budget_ceiling=budget_ceiling,
            currency=currency,
            mission_id=mission_id,
            task_id=task_id,
            execution_id=execution_id,
            cache_hit=cache_hit,
            compression_applied=compression_applied,
            policy=policy,
            metadata=metadata or {},
        )


@dataclass(frozen=True)
class CostAwareDecision:
    """
    Decisión formal, inmutable y determinista generada por la política consciente de coste (M.6).
    """
    status: CostAwareDecisionStatus
    selected_route: Optional[ModelRoute]
    estimated_cost: Optional[Decimal]
    currency: str = "USD"
    budget_ceiling: Optional[Decimal] = None
    task_type: str = ""
    mission_id: Optional[str] = None
    task_id: Optional[str] = None
    execution_id: Optional[str] = None
    eligible_routes: Tuple[ModelRoute, ...] = field(default_factory=tuple)
    route_estimates: Tuple[RouteCostEstimate, ...] = field(default_factory=tuple)
    cache_impact_avoided: bool = False
    policy_id: str = "default_cost_policy"
    policy_version: str = "1.0.0"
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    deterministic_rationale: str = ""
    actual_cost_record_id: Optional[str] = None
    evaluation_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if self.estimated_cost is not None and not isinstance(self.estimated_cost, Decimal):
            object.__setattr__(self, "estimated_cost", Decimal(str(self.estimated_cost)))
        if self.budget_ceiling is not None and not isinstance(self.budget_ceiling, Decimal):
            object.__setattr__(self, "budget_ceiling", Decimal(str(self.budget_ceiling)))

        object.__setattr__(self, "eligible_routes", tuple(self.eligible_routes))
        object.__setattr__(self, "route_estimates", tuple(self.route_estimates))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    @property
    def is_approved(self) -> bool:
        return self.status == CostAwareDecisionStatus.APPROVED and self.selected_route is not None

    def calculate_checksum(self) -> str:
        """Calcula el hash SHA-256 canónico de la decisión para auditoría y trazabilidad."""
        payload = {
            "status": self.status.value,
            "selected_route_id": self.selected_route.route_id if self.selected_route else None,
            "estimated_cost": str(self.estimated_cost) if self.estimated_cost is not None else "UNKNOWN",
            "currency": self.currency,
            "budget_ceiling": str(self.budget_ceiling) if self.budget_ceiling is not None else None,
            "task_type": self.task_type,
            "mission_id": self.mission_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "eligible_route_ids": [r.route_id for r in self.eligible_routes],
            "route_estimates": [
                {
                    "route_id": re.route_id,
                    "estimated_total_cost": str(re.estimated_total_cost) if re.estimated_total_cost is not None else "UNKNOWN",
                    "is_known": re.is_known,
                    "is_technically_eligible": re.is_technically_eligible,
                    "is_within_budget": re.is_within_budget,
                    "exclusion_reasons": list(re.exclusion_reasons),
                }
                for re in self.route_estimates
            ],
            "cache_impact_avoided": self.cache_impact_avoided,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reason_codes": list(self.reason_codes),
            "deterministic_rationale": self.deterministic_rationale,
        }
        canonical_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
