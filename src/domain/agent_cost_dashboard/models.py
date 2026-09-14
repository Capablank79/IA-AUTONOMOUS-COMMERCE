"""
Modelos de Dominio y ViewModels Seguros para Q.5 — Agent Cost Dashboard (Hito Q — Business Intelligence).

Principios Q.5:
1. DASHBOARD != BILLING ENGINE: Estrictamente consultivo, proyectivo y explicable.
   No factura al tenant, no cambia planes, no modifica pricing SaaS, no cobra pagos,
   no calcula invoices O.9 ni altera budgets automáticamente.
2. TENANT ISOLATION (O.1): Cada consulta opera bajo un TenantContext estricto. Cero fugas cross-tenant.
3. SAAS AUTHENTICATION & RBAC (O.3 / O.4 / N.4): Requiere sesión activa y permiso AGENT_COST_DASHBOARD_READ o BUSINESS_INTELLIGENCE_READ.
4. PRECISION MONETARIA (Decimal): Todos los importes monetarios usan Decimal con divisa explícita. Prohibido float.
5. MULTI-CURRENCY SAFE: No sumar divisas heterogéneas sin tasa FX canónica. Se agrupan totales por divisa.
6. UNKNOWN != 0: Si falta token count, provider price, currency o costo, se preserva UNKNOWN/None. Missing cost != gratis.
7. COST ATTRIBUTION: No atribuir costos a una misión/agente sin correlación real. Sin mission_id => unattributed.
8. SENSITIVE DATA DEFENSE (N.9) & NO PRIVATE CoT: Excluye claves de API, tokens, contraseñas y Chain-of-Thought privado.
9. DETERMINISMO Y TIE-BREAKING: Ordenación determinista con desempate estable por occurred_at y cost_item_id.
10. PAGINACIÓN ACOTADA: Paginación obligatoria y límites estrictos de tamaño de página (máximo 100).
11. INTEGRACIÓN CONSULTIVA: Enlace seguro hacia misiones (Q.4) sin mutar datos de ejecución.
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


class AgentCostSortField(str, Enum):
    """Campos canónicos permitidos para ordenación en el Agent Cost Dashboard."""
    OCCURRED_AT = "occurred_at"
    TOTAL_COST = "total_cost"
    TOTAL_TOKENS = "total_tokens"
    REQUEST_COUNT = "request_count"
    PROVIDER = "provider"
    MODEL = "model"
    AGENT_TYPE = "agent_type"
    MISSION_ID = "mission_id"
    ITEM_ID = "item_id"


class SortOrder(str, Enum):
    """Dirección de ordenación canónica."""
    ASC = "asc"
    DESC = "desc"


class CostConfidenceSource(str, Enum):
    """Fuente de confianza de la información de costo."""
    ACTUAL_RECORDED = "ACTUAL_RECORDED"
    ESTIMATED_METERED = "ESTIMATED_METERED"
    PRICING_CATALOG_DERIVED = "PRICING_CATALOG_DERIVED"
    UNKNOWN_UNPRICED = "UNKNOWN_UNPRICED"


@dataclass(frozen=True)
class AgentCostDashboardItem:
    """
    DTO / View Model seguro e inmutable para un registro de costo/uso en el Agent Cost Dashboard.
    Proyecta información operacional sin exponer secretos, tokens ni CoT privado.
    Preserva estrictamente UNKNOWN != 0 y Decimal para cantidades monetarias.
    """
    item_id: str
    tenant_id: str
    occurred_at: datetime
    request_status: str
    is_known_cost: bool
    is_attributed: bool
    provider: Optional[str] = None
    model: Optional[str] = None
    agent_type: Optional[str] = None
    mission_id: Optional[str] = None
    execution_id: Optional[str] = None
    task_type: Optional[str] = None
    request_count: int = 1
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    unit_cost: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None
    currency: Optional[str] = "USD"
    cost_source: CostConfidenceSource = CostConfidenceSource.UNKNOWN_UNPRICED
    correlation_id: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    unknown_fields: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.item_id, field_name="item_id")
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if self.mission_id:
            validate_safe_identifier(self.mission_id, field_name="mission_id")
        if self.execution_id:
            validate_safe_identifier(self.execution_id, field_name="execution_id")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", deep_freeze(dict(self.details)))
        if not isinstance(self.unknown_fields, tuple):
            object.__setattr__(self, "unknown_fields", tuple(self.unknown_fields))
        if self.occurred_at.tzinfo is None:
            object.__setattr__(self, "occurred_at", self.occurred_at.replace(tzinfo=timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly preservando strings para Decimal y fechas ISO."""
        return {
            "item_id": self.item_id,
            "tenant_id": self.tenant_id,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "request_status": self.request_status,
            "is_known_cost": self.is_known_cost,
            "is_attributed": self.is_attributed,
            "provider": self.provider,
            "model": self.model,
            "agent_type": self.agent_type,
            "mission_id": self.mission_id,
            "execution_id": self.execution_id,
            "task_type": self.task_type,
            "request_count": self.request_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "unit_cost": str(self.unit_cost) if self.unit_cost is not None else None,
            "total_cost": str(self.total_cost) if self.total_cost is not None else None,
            "currency": self.currency,
            "cost_source": self.cost_source.value if hasattr(self.cost_source, "value") else str(self.cost_source),
            "correlation_id": self.correlation_id,
            "details": _unwrap_mapping_proxies(self.details),
            "unknown_fields": list(self.unknown_fields),
        }


@dataclass(frozen=True)
class CurrencyCostBreakdown:
    """Desglose de costos por divisa individual garantizando no mezclar monedas."""
    currency: str
    total_known_cost: Decimal
    known_cost_items_count: int
    avg_cost_per_request: Optional[Decimal] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "currency": self.currency,
            "total_known_cost": str(self.total_known_cost),
            "known_cost_items_count": self.known_cost_items_count,
            "avg_cost_per_request": str(self.avg_cost_per_request) if self.avg_cost_per_request is not None else None,
        }


@dataclass(frozen=True)
class DimensionCostBreakdown:
    """Agregado por dimensión (provider, model, agent, mission)."""
    dimension_key: str
    request_count: int
    total_tokens: Optional[int]
    cost_by_currency: Mapping[str, Decimal] = field(default_factory=dict)
    unknown_cost_events_count: int = 0

    def __post_init__(self):
        if not isinstance(self.cost_by_currency, MappingProxyType):
            object.__setattr__(self, "cost_by_currency", deep_freeze(dict(self.cost_by_currency)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension_key": self.dimension_key,
            "request_count": self.request_count,
            "total_tokens": self.total_tokens,
            "cost_by_currency": {k: str(v) for k, v in self.cost_by_currency.items()},
            "unknown_cost_events_count": self.unknown_cost_events_count,
        }


@dataclass(frozen=True)
class MissionCostSummaryItem:
    """Resumen de costo para una misión específica con enlace seguro."""
    mission_id: str
    request_count: int
    total_tokens: Optional[int]
    cost_by_currency: Mapping[str, Decimal] = field(default_factory=dict)
    unknown_cost_events_count: int = 0
    mission_link: str = ""

    def __post_init__(self):
        if not isinstance(self.cost_by_currency, MappingProxyType):
            object.__setattr__(self, "cost_by_currency", deep_freeze(dict(self.cost_by_currency)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "request_count": self.request_count,
            "total_tokens": self.total_tokens,
            "cost_by_currency": {k: str(v) for k, v in self.cost_by_currency.items()},
            "unknown_cost_events_count": self.unknown_cost_events_count,
            "mission_link": self.mission_link,
        }


@dataclass(frozen=True)
class AgentCostDashboardSummary:
    """
    Resumen estadístico y financiero global del Agent Cost Dashboard.
    Multi-currency safe y con contabilidad estricta de eventos desconocidos y no atribuidos.
    """
    tenant_id: str
    generated_at: datetime
    total_requests: int
    total_input_tokens: Optional[int]
    total_output_tokens: Optional[int]
    total_cached_tokens: Optional[int]
    total_tokens: Optional[int]
    total_known_cost_by_currency: Mapping[str, Decimal] = field(default_factory=dict)
    currency_breakdowns: Tuple[CurrencyCostBreakdown, ...] = field(default_factory=tuple)
    unknown_cost_events_count: int = 0
    attributed_cost_events_count: int = 0
    unattributed_cost_events_count: int = 0
    cost_by_provider: Mapping[str, DimensionCostBreakdown] = field(default_factory=dict)
    cost_by_model: Mapping[str, DimensionCostBreakdown] = field(default_factory=dict)
    cost_by_agent: Mapping[str, DimensionCostBreakdown] = field(default_factory=dict)
    top_missions_by_cost: Tuple[MissionCostSummaryItem, ...] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.total_known_cost_by_currency, MappingProxyType):
            object.__setattr__(self, "total_known_cost_by_currency", deep_freeze(dict(self.total_known_cost_by_currency)))
        if not isinstance(self.currency_breakdowns, tuple):
            object.__setattr__(self, "currency_breakdowns", tuple(self.currency_breakdowns))
        if not isinstance(self.cost_by_provider, MappingProxyType):
            object.__setattr__(self, "cost_by_provider", deep_freeze(dict(self.cost_by_provider)))
        if not isinstance(self.cost_by_model, MappingProxyType):
            object.__setattr__(self, "cost_by_model", deep_freeze(dict(self.cost_by_model)))
        if not isinstance(self.cost_by_agent, MappingProxyType):
            object.__setattr__(self, "cost_by_agent", deep_freeze(dict(self.cost_by_agent)))
        if not isinstance(self.top_missions_by_cost, tuple):
            object.__setattr__(self, "top_missions_by_cost", tuple(self.top_missions_by_cost))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "total_requests": self.total_requests,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_tokens": self.total_tokens,
            "total_known_cost_by_currency": {k: str(v) for k, v in self.total_known_cost_by_currency.items()},
            "currency_breakdowns": [b.to_dict() for b in self.currency_breakdowns],
            "unknown_cost_events_count": self.unknown_cost_events_count,
            "attributed_cost_events_count": self.attributed_cost_events_count,
            "unattributed_cost_events_count": self.unattributed_cost_events_count,
            "cost_by_provider": {k: v.to_dict() for k, v in self.cost_by_provider.items()},
            "cost_by_model": {k: v.to_dict() for k, v in self.cost_by_model.items()},
            "cost_by_agent": {k: v.to_dict() for k, v in self.cost_by_agent.items()},
            "top_missions_by_cost": [m.to_dict() for m in self.top_missions_by_cost],
        }


@dataclass(frozen=True)
class AgentCostDashboardQuery:
    """
    Parámetros de consulta y filtrado para el Agent Cost Dashboard.
    """
    provider: Optional[str] = None
    model: Optional[str] = None
    agent_type: Optional[str] = None
    mission_id: Optional[str] = None
    currency: Optional[str] = None
    is_attributed: Optional[bool] = None
    is_known_cost: Optional[bool] = None
    min_cost: Optional[Decimal] = None
    max_cost: Optional[Decimal] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    search_text: Optional[str] = None
    sort_by: AgentCostSortField = AgentCostSortField.OCCURRED_AT
    sort_order: SortOrder = SortOrder.DESC
    page: int = 1
    page_size: int = 20

    def __post_init__(self):
        if self.page < 1:
            object.__setattr__(self, "page", 1)
        if self.page_size < 1:
            object.__setattr__(self, "page_size", 1)
        if self.page_size > 100:
            object.__setattr__(self, "page_size", 100)
        if self.min_cost is not None and not isinstance(self.min_cost, Decimal):
            object.__setattr__(self, "min_cost", Decimal(str(self.min_cost)))
        if self.max_cost is not None and not isinstance(self.max_cost, Decimal):
            object.__setattr__(self, "max_cost", Decimal(str(self.max_cost)))
        if self.date_from is not None and self.date_from.tzinfo is None:
            object.__setattr__(self, "date_from", self.date_from.replace(tzinfo=timezone.utc))
        if self.date_to is not None and self.date_to.tzinfo is None:
            object.__setattr__(self, "date_to", self.date_to.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class AgentCostDashboardPage:
    """
    Resultado paginado seguro del Agent Cost Dashboard.
    """
    items: Tuple[AgentCostDashboardItem, ...]
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_previous: bool

    def __post_init__(self):
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "page": self.page,
            "page_size": self.page_size,
            "total_items": self.total_items,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_previous": self.has_previous,
        }
