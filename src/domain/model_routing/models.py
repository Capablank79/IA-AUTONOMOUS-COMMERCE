"""
Modelos de dominio para la Estrategia de Enrutamiento de Modelos (Model Routing Strategy - M.1).

Transversal M — Control de Coste e Inferencia (Hito M.1).

Define:
- RoutingDecisionStatus: Estados canónicos de decisión (SELECTED, NO_ROUTE, UNKNOWN, ERROR).
- TaskCriticality: Nivel de criticidad de la tarea (LOW, MEDIUM, HIGH, CRITICAL).
- QualityRequirement: Nivel de requerimiento de calidad (ANY, STANDARD, HIGH, SUPERIOR).
- LatencyRequirement: Nivel de requerimiento de latencia (ANY, NORMAL, LOW_LATENCY, REAL_TIME).
- RouteCapability: Capacidades soportadas por un modelo/ruta (e.g., TOOL_USE, STRUCTURED_OUTPUT, VISION, LONG_CONTEXT, REASONING, JSON_MODE).
- RouteStatus: Estado de disponibilidad operativa de la ruta (AVAILABLE, DEGRADED, UNAVAILABLE, UNKNOWN).
- RouteExclusionReason: Razón estructurada por la cual una ruta fue descartada.
- ModelRoute: Representación inmutable de una ruta de inferencia disponible.
- RoutingRequest: Expresión inmutable de los requerimientos de inferencia para una tarea.
- RoutingPolicy: Reglas y pesos declarativos y versionados para el enrutamiento.
- ExclusionRecord: Registro inmutable de la exclusión de una ruta y su motivo estructurado.
- RoutingDecision: Decisión formal, estructurada y determinista producida por la estrategia de routing.

Principios M.1:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuplas).
- M.1 responde: "¿Qué rutas/modelos están disponibles y mediante qué estrategia estructurada puede seleccionarse una ruta de inferencia?".
- NO implementa optimizaciones de presupuesto (M.2), compresión de prompt (M.3), caching (M.4), selección por tarea compleja (M.5) ni política económica global (M.6).
- Determinismo absoluto: Mismo request + mismas rutas + misma política => Misma decisión sin random ni hash().
- Filtrado de capacidades estricto: Una ruta que carece de capacidades requeridas NUNCA es seleccionada.
- Manejo explícito de criticidad y latencia.
- Fallback explícito: Si la ruta preferida no está disponible, se evalúa y documenta la siguiente elegible.
- Sanitización estricta de secretos y exclusión total de Chain-of-Thought (CoT) o API Keys.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, List, Set, Union


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
    "chain_of_thought",
    "reasoning",
    "reasoning_tokens",
    "internal_scratchpad",
    "card_number",
    "auth_header",
    "bearer",
}


def sanitize_routing_data(val: Any) -> Any:
    """Sanitiza recursivamente estructuras de datos para eliminar secretos, tokens y CoT."""
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS) and not isinstance(v, (dict, MappingProxyType, list, tuple)):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = sanitize_routing_data(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [sanitize_routing_data(v) for v in val]
    return val


def deep_freeze(val: Any) -> Any:
    """Convierte recursivamente diccionarios en MappingProxyType y listas en tuplas."""
    if isinstance(val, (dict, MappingProxyType)):
        return MappingProxyType({k: deep_freeze(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(deep_freeze(v) for v in val)
    return val


class RoutingDecisionStatus(str, Enum):
    """Estado canónico de la decisión de enrutamiento."""
    SELECTED = "SELECTED"
    NO_ROUTE = "NO_ROUTE"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class TaskCriticality(str, Enum):
    """Nivel de criticidad de la tarea que solicita inferencia."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class QualityRequirement(str, Enum):
    """Requerimiento mínimo de calidad de inferencia."""
    ANY = "ANY"
    STANDARD = "STANDARD"
    HIGH = "HIGH"
    SUPERIOR = "SUPERIOR"


class LatencyRequirement(str, Enum):
    """Requerimiento de latencia para la inferencia."""
    ANY = "ANY"
    NORMAL = "NORMAL"
    LOW_LATENCY = "LOW_LATENCY"
    REAL_TIME = "REAL_TIME"


class RouteCapability(str, Enum):
    """Capacidades técnicas requeridas u ofrecidas por modelos."""
    TOOL_USE = "TOOL_USE"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    VISION = "VISION"
    LONG_CONTEXT = "LONG_CONTEXT"
    REASONING = "REASONING"
    JSON_MODE = "JSON_MODE"
    FUNCTION_CALLING = "FUNCTION_CALLING"


class RouteStatus(str, Enum):
    """Disponibilidad y estado operativo de la ruta."""
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class RouteExclusionReason(str, Enum):
    """Razones estructuradas y canónicas para descartar una ruta."""
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN_STATUS = "UNKNOWN_STATUS"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    INSUFFICIENT_QUALITY = "INSUFFICIENT_QUALITY"
    LATENCY_TOO_HIGH = "LATENCY_TOO_HIGH"
    COST_CEILING_EXCEEDED = "COST_CEILING_EXCEEDED"
    CRITICALITY_INADEQUATE = "CRITICALITY_INADEQUATE"
    PROVIDER_NOT_ALLOWED = "PROVIDER_NOT_ALLOWED"
    POLICY_RESTRICTION = "POLICY_RESTRICTION"
    TIE_ELIMINATED = "TIE_ELIMINATED"


@dataclass(frozen=True)
class ModelRoute:
    """
    Representa una ruta o endpoint de inferencia disponible en el sistema.
    Inmutable y sanitizada (sin llaves de API ni credenciales).
    """
    route_id: str
    provider: str
    model_id: str
    capabilities: Tuple[RouteCapability, ...] = field(default_factory=tuple)
    status: RouteStatus = RouteStatus.AVAILABLE
    context_window: Optional[int] = None
    estimated_cost_input_per_million: Optional[Decimal] = None
    estimated_cost_output_per_million: Optional[Decimal] = None
    flat_cost_per_request: Optional[Decimal] = None
    latency_class: LatencyRequirement = LatencyRequirement.NORMAL
    quality_class: QualityRequirement = QualityRequirement.STANDARD
    supported_task_types: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 100  # Menor número = mayor preferencia base

    def __post_init__(self):
        if not self.route_id or not self.route_id.strip():
            raise ValueError("route_id cannot be empty")
        if not self.provider or not self.provider.strip():
            raise ValueError("provider cannot be empty")
        if not self.model_id or not self.model_id.strip():
            raise ValueError("model_id cannot be empty")
        if self.context_window is not None:
            if not isinstance(self.context_window, int) or isinstance(self.context_window, bool):
                raise ValueError("context_window must be an integer")
            if self.context_window <= 0:
                raise ValueError("context_window must be positive")

        clean_meta = sanitize_routing_data(self.metadata)
        object.__setattr__(self, "metadata", deep_freeze(clean_meta))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "supported_task_types", tuple(self.supported_task_types))

    def has_capability(self, capability: RouteCapability) -> bool:
        return capability in self.capabilities

    def has_all_capabilities(self, required: Sequence[RouteCapability]) -> bool:
        return all(cap in self.capabilities for cap in required)


@dataclass(frozen=True)
class RoutingRequest:
    """
    Solicitud estructurada para enrutar una inferencia.
    Expresa los requerimientos y restricciones de la tarea.
    """
    task_type: str
    complexity: Optional[str] = "STANDARD"
    criticality: TaskCriticality = TaskCriticality.MEDIUM
    required_capabilities: Tuple[RouteCapability, ...] = field(default_factory=tuple)
    min_quality: QualityRequirement = QualityRequirement.STANDARD
    max_latency: LatencyRequirement = LatencyRequirement.ANY
    cost_ceiling_per_call: Optional[Decimal] = None
    preferred_providers: Tuple[str, ...] = field(default_factory=tuple)
    allowed_providers: Tuple[str, ...] = field(default_factory=tuple)
    context_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.task_type or not self.task_type.strip():
            raise ValueError("task_type cannot be empty")

        clean_meta = sanitize_routing_data(self.context_metadata)
        object.__setattr__(self, "context_metadata", deep_freeze(clean_meta))
        object.__setattr__(self, "required_capabilities", tuple(self.required_capabilities))
        object.__setattr__(self, "preferred_providers", tuple(p.lower() for p in self.preferred_providers))
        object.__setattr__(self, "allowed_providers", tuple(p.lower() for p in self.allowed_providers))


@dataclass(frozen=True)
class ExclusionRecord:
    """
    Detalle inmutable de una ruta descartada durante el filtrado.
    """
    route_id: str
    reason_code: RouteExclusionReason
    message: str


@dataclass(frozen=True)
class RoutingPolicy:
    """
    Política declarativa y versionada que rige el proceso de selección y ordenamiento.
    """
    policy_id: str
    version: str = "1.0.0"
    allow_degraded_fallback: bool = False
    strict_criticality_filter: bool = True
    prefer_lower_cost_when_quality_met: bool = False
    description: str = "Default deterministic model routing policy"

    def __post_init__(self):
        if not self.policy_id or not self.policy_id.strip():
            raise ValueError("policy_id cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("version cannot be empty")


@dataclass(frozen=True)
class RoutingDecision:
    """
    Resultado formal y estructurado de la estrategia de enrutamiento M.1.
    Garantiza reproducibilidad, determinismo y trazabilidad.
    """
    status: RoutingDecisionStatus
    selected_route: Optional[ModelRoute]
    eligible_routes: Tuple[ModelRoute, ...] = field(default_factory=tuple)
    excluded_routes: Tuple[ExclusionRecord, ...] = field(default_factory=tuple)
    policy_id: str = "default_policy"
    policy_version: str = "1.0.0"
    deterministic_rationale: str = ""
    fallback_applied: bool = False
    evaluation_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        object.__setattr__(self, "eligible_routes", tuple(self.eligible_routes))
        object.__setattr__(self, "excluded_routes", tuple(self.excluded_routes))

    @property
    def is_selected(self) -> bool:
        return self.status == RoutingDecisionStatus.SELECTED and self.selected_route is not None

    def calculate_checksum(self) -> str:
        """Calcula un hash SHA-256 canónico de la decisión para auditoría y verificación."""
        payload = {
            "status": self.status.value,
            "selected_route_id": self.selected_route.route_id if self.selected_route else None,
            "eligible_route_ids": [r.route_id for r in self.eligible_routes],
            "excluded_routes": [{"route_id": e.route_id, "reason": e.reason_code.value} for e in self.excluded_routes],
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "deterministic_rationale": self.deterministic_rationale,
            "fallback_applied": self.fallback_applied,
        }
        canonical_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
