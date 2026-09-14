"""
Modelos de Dominio y ViewModels Seguros para Q.4 — Mission Dashboard (Hito Q — Business Intelligence).

Principios Q.4:
1. DASHBOARD != ORCHESTRATOR / EXECUTION ENGINE: Estrictamente consultivo, proyectivo y explicable.
   No inicia misiones, no muta estados, no reintenta ejecuciones ni invoca herramientas o scrapers.
2. TENANT ISOLATION (O.1): Cada consulta opera bajo un TenantContext estricto. Cero fugas cross-tenant.
3. UNKNOWN != 0 / UNKNOWN != SUCCESS: Duración desconocida != 0s. Progreso desconocido != 0%.
   Valores ausentes o estados intermedios se conservan como None o UNKNOWN, nunca como 0, 0.0 o valores inventados.
4. SENSITIVE DATA DEFENSE (N.9) & NO PRIVATE CoT: Excluye tokens, contraseñas, claves de API y Chain-of-Thought (CoT) privado.
5. DETERMINISMO Y TIE-BREAKING: Ordenación determinista con desempate estable por mission_id.
6. PAGINACIÓN ACOTADA: Paginación obligatoria y límites estrictos de tamaño de página (máximo 100).
7. TIMELINE AUDITABLE Y EXPLICABLE: Reconstrucción estructurada y cronológica de eventos de ejecución K.1 y K.2.
8. CROSS-DOMAIN LINKING: Enlaces consultivos seguros hacia Oportunidades (Q.1), Proveedores (Q.2) y Rentabilidad (Q.3).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, List, Sequence, Union
import uuid

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.mission.models import (
    MissionType,
    MissionStatus,
    MissionPriority,
    LoopAction,
)


def _unwrap_mapping_proxies(obj: Any) -> Any:
    """Convierte recursivamente mappingproxy y tuplas en dicts y listas JSON-serializables."""
    if isinstance(obj, (MappingProxyType, dict)):
        return {k: _unwrap_mapping_proxies(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_unwrap_mapping_proxies(x) for x in obj]
    elif isinstance(obj, Decimal):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    return obj


class MissionSortField(str, Enum):
    """Campos canónicos permitidos para ordenación en el Mission Dashboard."""
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    STATUS = "status"
    PRIORITY = "priority"
    DURATION = "duration"
    MISSION_ID = "mission_id"
    TYPE = "type"


class SortOrder(str, Enum):
    """Dirección de ordenación canónica."""
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class MissionDashboardItem:
    """
    DTO / View Model seguro e inmutable para un ítem del Mission Dashboard.
    Proyecta información de la misión sin exponer secretos, tokens ni CoT privado.
    Preserva estrictamente UNKNOWN != 0 y semántica de incertidumbre.
    """
    mission_id: str
    mission_type: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    progress_pct: Optional[float] = None
    iteration_count: Optional[int] = None
    goal: Optional[str] = None
    target: Optional[str] = None
    opportunity_id: Optional[str] = None
    supplier_id: Optional[str] = None
    product_id: Optional[str] = None
    marketplace: Optional[str] = None
    category: Optional[str] = None
    error_count: int = 0
    block_count: int = 0
    evidence_count: int = 0
    decision_count: int = 0
    has_errors: bool = False
    outcome_summary: Optional[str] = None
    result_summary: Optional[str] = None
    parameters_summary: Mapping[str, Any] = field(default_factory=dict)
    unknown_fields: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.mission_id, field_name="mission_id")
        if self.opportunity_id:
            validate_safe_identifier(self.opportunity_id, field_name="opportunity_id")
        if self.supplier_id:
            validate_safe_identifier(self.supplier_id, field_name="supplier_id")
        if self.product_id:
            validate_safe_identifier(self.product_id, field_name="product_id")
        if not isinstance(self.parameters_summary, MappingProxyType):
            object.__setattr__(self, "parameters_summary", deep_freeze(dict(self.parameters_summary)))
        if not isinstance(self.unknown_fields, tuple):
            object.__setattr__(self, "unknown_fields", tuple(self.unknown_fields))
        if self.outcome_summary and not self.result_summary:
            object.__setattr__(self, "result_summary", self.outcome_summary)
        elif self.result_summary and not self.outcome_summary:
            object.__setattr__(self, "outcome_summary", self.result_summary)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "mission_id": self.mission_id,
            "mission_type": self.mission_type,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "progress_pct": self.progress_pct,
            "iteration_count": self.iteration_count,
            "goal": self.goal,
            "target": self.target,
            "opportunity_id": self.opportunity_id,
            "supplier_id": self.supplier_id,
            "product_id": self.product_id,
            "marketplace": self.marketplace,
            "category": self.category,
            "error_count": self.error_count,
            "block_count": self.block_count,
            "evidence_count": self.evidence_count,
            "decision_count": self.decision_count,
            "has_errors": self.has_errors,
            "outcome_summary": self.outcome_summary,
            "result_summary": self.result_summary,
            "parameters_summary": _unwrap_mapping_proxies(dict(self.parameters_summary)),
            "unknown_fields": list(self.unknown_fields),
        }


@dataclass(frozen=True)
class MissionDashboardSummary:
    """
    Resumen agregado del estado de misiones para el tenant consultado.
    Calculado determinísticamente a partir de registros reales.
    """
    tenant_id: str
    total_missions: int
    pending_count: int
    running_count: int
    completed_count: int
    failed_count: int
    blocked_count: int
    aborted_count: int
    average_duration_seconds: Optional[float]
    shortest_duration_seconds: Optional[float]
    longest_duration_seconds: Optional[float]
    missions_by_type: Mapping[str, int]
    missions_by_status: Mapping[str, int]
    missions_by_priority: Mapping[str, int]
    success_rate_pct: Optional[float] = None
    missions_with_errors: int = 0
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.missions_by_type, MappingProxyType):
            object.__setattr__(self, "missions_by_type", deep_freeze(dict(self.missions_by_type)))
        if not isinstance(self.missions_by_status, MappingProxyType):
            object.__setattr__(self, "missions_by_status", deep_freeze(dict(self.missions_by_status)))
        if not isinstance(self.missions_by_priority, MappingProxyType):
            object.__setattr__(self, "missions_by_priority", deep_freeze(dict(self.missions_by_priority)))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "tenant_id": self.tenant_id,
            "total_missions": self.total_missions,
            "pending_count": self.pending_count,
            "running_count": self.running_count,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "blocked_count": self.blocked_count,
            "aborted_count": self.aborted_count,
            "average_duration_seconds": self.average_duration_seconds,
            "shortest_duration_seconds": self.shortest_duration_seconds,
            "longest_duration_seconds": self.longest_duration_seconds,
            "missions_by_type": dict(self.missions_by_type),
            "missions_by_status": dict(self.missions_by_status),
            "missions_by_priority": dict(self.missions_by_priority),
            "success_rate_pct": self.success_rate_pct,
            "missions_with_errors": self.missions_with_errors,
            "generated_at": self.generated_at.isoformat(),
        }


@dataclass(frozen=True)
class MissionTimelineEntry:
    """
    Entrada individual e inmutable en la línea de tiempo de la misión.
    Representa un paso, decisión, acción o evento auditable.
    """
    timestamp: datetime
    step_type: str
    status: str
    actor_type: str = "SYSTEM"
    actor_id: str = "system"
    action: Optional[str] = None
    description: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    duration_seconds: Optional[float] = None
    iteration: Optional[int] = None

    def __post_init__(self):
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", deep_freeze(dict(self.details)))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "step_type": self.step_type,
            "status": self.status,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "action": self.action,
            "description": self.description,
            "details": _unwrap_mapping_proxies(dict(self.details)),
            "duration_seconds": self.duration_seconds,
            "iteration": self.iteration,
        }


@dataclass(frozen=True)
class MissionDashboardDetail:
    """
    Vista detallada y segura de una misión.
    Incluye desglose completo de traza, decisiones, evidencias, bloques, errores y enlaces cross-domain.
    """
    item: MissionDashboardItem
    timeline: Tuple[MissionTimelineEntry, ...] = field(default_factory=tuple)
    decisions: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    evidences: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    blocks: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    errors: Tuple[str, ...] = field(default_factory=tuple)
    output: Mapping[str, Any] = field(default_factory=dict)
    associated_opportunity: Optional[Dict[str, Any]] = None
    associated_supplier: Optional[Dict[str, Any]] = None
    associated_profit: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not isinstance(self.timeline, tuple):
            object.__setattr__(self, "timeline", tuple(self.timeline))
        if not isinstance(self.decisions, tuple):
            object.__setattr__(self, "decisions", tuple(self.decisions))
        if not isinstance(self.evidences, tuple):
            object.__setattr__(self, "evidences", tuple(self.evidences))
        if not isinstance(self.blocks, tuple):
            object.__setattr__(self, "blocks", tuple(self.blocks))
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))
        if not isinstance(self.output, MappingProxyType):
            object.__setattr__(self, "output", deep_freeze(dict(self.output)))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "item": self.item.to_dict(),
            "timeline": [entry.to_dict() for entry in self.timeline],
            "decisions": [_unwrap_mapping_proxies(d) for d in self.decisions],
            "evidences": [_unwrap_mapping_proxies(e) for e in self.evidences],
            "blocks": [_unwrap_mapping_proxies(b) for b in self.blocks],
            "errors": list(self.errors),
            "output": _unwrap_mapping_proxies(dict(self.output)),
            "associated_opportunity": _unwrap_mapping_proxies(self.associated_opportunity) if self.associated_opportunity else None,
            "associated_supplier": _unwrap_mapping_proxies(self.associated_supplier) if self.associated_supplier else None,
            "associated_profit": _unwrap_mapping_proxies(self.associated_profit) if self.associated_profit else None,
        }


@dataclass(frozen=True)
class MissionDashboardQuery:
    """Parámetros de consulta estructurados y sanitizados para el Mission Dashboard."""
    mission_type: Optional[Union[MissionType, str]] = None
    status: Optional[Union[MissionStatus, str]] = None
    priority: Optional[Union[MissionPriority, str]] = None
    opportunity_id: Optional[str] = None
    supplier_id: Optional[str] = None
    product_id: Optional[str] = None
    marketplace: Optional[str] = None
    category: Optional[str] = None
    has_errors: Optional[bool] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    search_text: Optional[str] = None
    sort_by: MissionSortField = MissionSortField.CREATED_AT
    sort_order: SortOrder = SortOrder.DESC
    page: int = 1
    page_size: int = 20

    def __post_init__(self):
        if self.page < 1:
            object.__setattr__(self, "page", 1)
        if self.page_size < 1:
            object.__setattr__(self, "page_size", 20)
        elif self.page_size > 100:
            object.__setattr__(self, "page_size", 100)


@dataclass(frozen=True)
class MissionDashboardPage:
    """Página paginada de resultados del Mission Dashboard."""
    items: Sequence[MissionDashboardItem]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool = False
    has_previous: bool = False

    def __post_init__(self):
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "has_next", self.page < self.total_pages)
        object.__setattr__(self, "has_previous", self.page > 1)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": self.total_count,
            "page": self.page,
            "page_size": self.page_size,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_previous": self.has_previous,
        }
