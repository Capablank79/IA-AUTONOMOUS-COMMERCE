"""
Modelos de dominio para la Selección de Modelos por Tarea (Model Selection by Task - M.5).

Transversal M — Control de Coste e Inferencia (Hito M.5).

Define:
- TaskComplexity: Nivel de complejidad intrínseca de la tarea (LOW, MEDIUM, HIGH, UNKNOWN).
- SelectionStatus: Estado de la resolución del requerimiento de selección (SUCCESS, UNKNOWN_TASK, NO_PROFILE, ERROR).
- TaskTypeTaxonomy: Taxonomía canónica de tipos de tarea reales del sistema (o UNKNOWN).
- TaskModelProfile: Perfil inmutable declarativo que asocia un tipo de tarea con sus requerimientos de modelo (complejidad, criticidad, capacidades, calidad mínima, latencia máxima, preferencia de proveedor/ruta opcional).
- TaskSelectionPolicy: Conjunto versionado de perfiles de tareas y reglas de resolución deterministas.
- TaskSelectionRequest: Petición inmutable de selección de modelo para una tarea dada.
- TaskSelectionRequirements: Requerimientos resueltos para la tarea antes de ejecutar routing.
- ModelSelectionResult: Resultado inmutable estructurado que contiene el perfil resuelto, los requerimientos, la decisión de routing M.1 delegada y los reason codes auditables.

Principios M.5:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuplas).
- M.5 responde: "Dado este tipo de tarea, ¿qué requisitos de modelo necesita antes de ejecutar routing?".
- M.5 transforma: Task / TaskSelectionRequest -> TaskModelProfile / TaskSelectionRequirements -> M.1 RoutingRequest -> M.1 RoutingDecision.
- M.5 NO reemplaza M.1: Reutiliza DeterministicModelRoutingStrategy (M.1) para seleccionar la ruta concreta.
- NO implementa optimización económica global (M.6) ni "cheapest always wins".
- Preservación explícita de incertidumbre: Tarea desconocida converge a UNKNOWN / UNKNOWN_TASK / NO_PROFILE, nunca asigna un modelo default silenciosamente.
- Determinismo absoluto: Mismo request + misma policy/version + mismas rutas => Mismo resultado sin random ni hash().
- Filtrado estricto por capacidades requeridas (TOOL_USE, STRUCTURED_OUTPUT, VISION, etc.) y calidad mínima.
- Sanitización estricta de secretos y exclusión total de Chain-of-Thought (CoT) o credenciales.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, List, Set, Union

from src.domain.model_routing.models import (
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RouteCapability,
    ModelRoute,
    RoutingRequest,
    RoutingDecision,
    RoutingDecisionStatus,
    sanitize_routing_data,
    deep_freeze,
)


class TaskComplexity(str, Enum):
    """
    Nivel de complejidad intrínseca de la tarea determinista.
    """
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class SelectionStatus(str, Enum):
    """
    Estado formal de la resolución de selección de modelo por tarea.
    """
    SUCCESS = "SUCCESS"
    UNKNOWN_TASK = "UNKNOWN_TASK"
    NO_PROFILE = "NO_PROFILE"
    ROUTING_FAILED = "ROUTING_FAILED"
    ERROR = "ERROR"


class StandardTaskType(str, Enum):
    """
    Taxonomía canónica mínima de tipos de tareas basadas en operaciones REALES del sistema.
    """
    # Market Intelligence & Hunter
    MARKET_DISCOVERY = "MARKET_DISCOVERY"
    MARKET_ANALYSIS = "MARKET_ANALYSIS"
    EXTRACTION = "EXTRACTION"
    CLASSIFICATION = "CLASSIFICATION"
    
    # Supplier Intelligence
    SUPPLIER_SEARCH = "SUPPLIER_SEARCH"
    SUPPLIER_DISCOVERY = "SUPPLIER_DISCOVERY"
    SUPPLIER_ANALYSIS = "SUPPLIER_ANALYSIS"
    
    # Economics, Operating & Capital
    PROFIT_EVALUATION = "PROFIT_EVALUATION"
    CAPITAL_ALLOCATION = "CAPITAL_ALLOCATION"
    OPERATING_MODEL_EVALUATION = "OPERATING_MODEL_EVALUATION"
    COMMERCIAL_REASONING = "COMMERCIAL_REASONING"
    
    # Publication & Content Generation
    STRUCTURED_GENERATION = "STRUCTURED_GENERATION"
    COMMERCIAL_PUBLICATION = "COMMERCIAL_PUBLICATION"
    LISTING_GENERATION = "LISTING_GENERATION"
    
    # Governance & Policy
    POLICY_SENSITIVE_DECISION = "POLICY_SENSITIVE_DECISION"
    POLICY_EVALUATION = "POLICY_EVALUATION"
    
    # Tool Execution Planning & Vision
    TOOL_EXECUTION_PLANNING = "TOOL_EXECUTION_PLANNING"
    VISION_ANALYSIS = "VISION_ANALYSIS"
    
    # Fallback / Unknown
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TaskModelProfile:
    """
    Perfil declarativo inmutable que especifica los requerimientos de inferencia
    para un tipo de tarea determinado.
    """
    task_type: str
    complexity: TaskComplexity
    criticality: TaskCriticality
    min_quality: QualityRequirement
    latency_requirement: LatencyRequirement
    required_capabilities: Tuple[RouteCapability, ...] = field(default_factory=tuple)
    preferred_provider: Optional[str] = None
    preferred_route_id: Optional[str] = None
    max_cost_limit: Optional[Decimal] = None
    fallback_requirements: Optional[Tuple[RouteCapability, ...]] = None
    policy_id: str = "default_task_policy"
    policy_version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.task_type or not self.task_type.strip():
            raise ValueError("TaskModelProfile task_type cannot be empty")
        
        # Normalizar tuplas inmutables
        caps = tuple(sorted(list(set(self.required_capabilities)), key=lambda c: c.value))
        object.__setattr__(self, "required_capabilities", caps)
        
        if self.fallback_requirements is not None:
            f_caps = tuple(sorted(list(set(self.fallback_requirements)), key=lambda c: c.value))
            object.__setattr__(self, "fallback_requirements", f_caps)
            
        # Sanitizar y congelar metadata
        sanitized_meta = sanitize_routing_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    def calculate_checksum(self) -> str:
        """Calcula hash SHA-256 canónico del perfil para auditoría y reproducibilidad."""
        payload = {
            "task_type": self.task_type,
            "complexity": self.complexity.value,
            "criticality": self.criticality.value,
            "min_quality": self.min_quality.value,
            "latency_requirement": self.latency_requirement.value,
            "required_capabilities": [c.value for c in self.required_capabilities],
            "preferred_provider": self.preferred_provider,
            "preferred_route_id": self.preferred_route_id,
            "max_cost_limit": str(self.max_cost_limit) if self.max_cost_limit is not None else None,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }
        canonical_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskSelectionPolicy:
    """
    Colección versionada e inmutable de perfiles de tareas y reglas de mapeo.
    """
    policy_id: str = "task_selection_policy"
    version: str = "1.0.0"
    profiles: Mapping[str, TaskModelProfile] = field(default_factory=dict)
    allow_dynamic_override: bool = False
    strict_capability_matching: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.policy_id or not self.policy_id.strip():
            raise ValueError("TaskSelectionPolicy policy_id cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("TaskSelectionPolicy version cannot be empty")
            
        # Congelar mapping de perfiles
        profiles_dict = dict(self.profiles)
        object.__setattr__(self, "profiles", MappingProxyType(profiles_dict))
        
        sanitized_meta = sanitize_routing_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    def get_profile(self, task_type: str) -> Optional[TaskModelProfile]:
        """Obtiene el perfil configurado para una tarea específica."""
        if not task_type:
            return None
        return self.profiles.get(task_type.strip())

    def calculate_checksum(self) -> str:
        """Calcula el checksum SHA-256 de la política completa."""
        profiles_payload = {
            k: v.calculate_checksum() for k, v in sorted(self.profiles.items())
        }
        payload = {
            "policy_id": self.policy_id,
            "version": self.version,
            "allow_dynamic_override": self.allow_dynamic_override,
            "strict_capability_matching": self.strict_capability_matching,
            "profiles": profiles_payload,
        }
        canonical_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskSelectionRequest:
    """
    Solicitud inmutable para seleccionar los requerimientos de modelo y resolver
    la ruta M.1 correspondiente para una tarea.
    """
    task_type: str
    complexity_override: Optional[TaskComplexity] = None
    criticality_override: Optional[TaskCriticality] = None
    additional_capabilities: Tuple[RouteCapability, ...] = field(default_factory=tuple)
    cost_ceiling: Optional[Decimal] = None
    preferred_provider: Optional[str] = None
    preferred_route_id: Optional[str] = None
    correlation_id: Optional[str] = None
    task_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.task_type or not self.task_type.strip():
            raise ValueError("TaskSelectionRequest task_type cannot be empty")
            
        caps = tuple(sorted(list(set(self.additional_capabilities)), key=lambda c: c.value))
        object.__setattr__(self, "additional_capabilities", caps)
        
        sanitized_meta = sanitize_routing_data(dict(self.task_metadata))
        object.__setattr__(self, "task_metadata", deep_freeze(sanitized_meta))


@dataclass(frozen=True)
class TaskSelectionRequirements:
    """
    Requerimientos consolidados y estructurados producidos por M.5
    antes de invocar el enrutamiento M.1.
    """
    task_type: str
    complexity: TaskComplexity
    criticality: TaskCriticality
    min_quality: QualityRequirement
    latency_requirement: LatencyRequirement
    required_capabilities: Tuple[RouteCapability, ...]
    preferred_provider: Optional[str] = None
    preferred_route_id: Optional[str] = None
    cost_ceiling: Optional[Decimal] = None
    policy_id: str = "default_task_policy"
    policy_version: str = "1.0.0"
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        caps = tuple(sorted(list(set(self.required_capabilities)), key=lambda c: c.value))
        object.__setattr__(self, "required_capabilities", caps)
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    def to_m1_routing_request(self) -> RoutingRequest:
        """
        Transforma los requerimientos M.5 en un RoutingRequest estándar de M.1.
        """
        preferred_providers = (self.preferred_provider,) if self.preferred_provider else ()
        return RoutingRequest(
            task_type=self.task_type,
            complexity=self.complexity.value,
            criticality=self.criticality,
            required_capabilities=self.required_capabilities,
            min_quality=self.min_quality,
            max_latency=self.latency_requirement,
            cost_ceiling_per_call=self.cost_ceiling,
            preferred_providers=preferred_providers,
            context_metadata={
                "m5_complexity": self.complexity.value,
                "m5_policy_id": self.policy_id,
                "m5_policy_version": self.policy_version,
                "m5_preferred_route_id": self.preferred_route_id,
                "m5_reason_codes": list(self.reason_codes),
            },
        )


@dataclass(frozen=True)
class ModelSelectionResult:
    """
    Resultado formal, estructurado, inmutable y determinista de M.5.
    Contiene el perfil resuelto, los requerimientos consolidados, la decisión de routing
    delegada a M.1 y metadatos de auditoría sin filtrar secretos ni Chain-of-Thought.
    """
    status: SelectionStatus
    task_type: str
    resolved_profile: Optional[TaskModelProfile]
    requirements: Optional[TaskSelectionRequirements]
    routing_decision: Optional[RoutingDecision]
    selected_route: Optional[ModelRoute]
    policy_id: str
    policy_version: str
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    deterministic_rationale: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    @property
    def is_successful(self) -> bool:
        return (
            self.status == SelectionStatus.SUCCESS
            and self.selected_route is not None
            and self.routing_decision is not None
            and self.routing_decision.is_selected
        )

    def calculate_checksum(self) -> str:
        """Calcula hash SHA-256 canónico del resultado para auditoría y trazabilidad."""
        payload = {
            "status": self.status.value,
            "task_type": self.task_type,
            "profile_checksum": self.resolved_profile.calculate_checksum() if self.resolved_profile else None,
            "selected_route_id": self.selected_route.route_id if self.selected_route else None,
            "routing_decision_checksum": self.routing_decision.calculate_checksum() if self.routing_decision else None,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reason_codes": list(self.reason_codes),
            "deterministic_rationale": self.deterministic_rationale,
        }
        canonical_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
