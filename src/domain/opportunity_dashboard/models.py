"""
Modelos de Dominio y ViewModels Seguros para Q.1 — Opportunity Dashboard (Hito Q — Business Intelligence).

Principios Q.1:
1. DASHBOARD != ENGINE: Estrictamente consultivo, proyectivo y explicable. No ejecuta scraping, ni compras, ni llamadas a marketplaces.
2. TENANT ISOLATION: Cada consulta opera bajo un TenantContext estricto. Cero fugas cross-tenant.
3. UNKNOWN != 0: Valores ausentes o inciertos se conservan como None o UNKNOWN, nunca como 0, 0.0 o cadenas vacías.
4. MONEY EN DECIMAL: Precios, márgenes y métricas monetarias siempre en Decimal con divisa explícita. Nunca float.
5. SENSITIVE DATA DEFENSE (N.9): Excluye secretos de proveedores, tokens, credenciales y CoT privado.
6. DETERMINISMO Y TIE-BREAKING: Ordenación determinista con desempate estable por detected_at y opportunity_id.
7. PAGINACIÓN ACOTADA: Paginación obligatoria y límites estrictos de tamaño de página (máximo 100).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, List, Sequence
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
from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
)
from src.domain.opportunity.models import (
    OpportunityExplanation,
    OpportunityComparisonDimension,
    OpportunityComparisonResult,
)


class OpportunitySortField(str, Enum):
    """Campos canónicos permitidos para ordenación en el Opportunity Dashboard."""
    OPPORTUNITY_SCORE = "opportunity_score"
    DETECTED_AT = "detected_at"
    POTENTIAL_MARGIN = "potential_margin"
    CONFIDENCE = "confidence"
    TITLE = "title"


class SortOrder(str, Enum):
    """Dirección de ordenación canónica."""
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class OpportunityDashboardItem:
    """
    DTO / View Model seguro e inmutable para un ítem del Opportunity Dashboard.
    Proyecta información de negocio sin exponer secretos ni datos privados de proveedores.
    Preserva estrictamente UNKNOWN != 0 y Decimal para cantidades monetarias.
    """
    opportunity_id: str
    canonical_product_id: str
    marketplace: str
    opportunity_type: str
    status: str
    confidence: str
    detected_at: datetime
    title: Optional[str] = None
    category: Optional[str] = None
    product_sku: Optional[str] = None
    opportunity_score: Optional[Decimal] = None
    demand_intensity: Optional[str] = None
    competition_density: Optional[str] = None
    estimated_price_amount: Optional[Decimal] = None
    estimated_price_currency: Optional[str] = None
    lowest_competitor_price_amount: Optional[Decimal] = None
    lowest_competitor_price_currency: Optional[str] = None
    buy_box_price_amount: Optional[Decimal] = None
    buy_box_price_currency: Optional[str] = None
    potential_margin_ratio: Optional[Decimal] = None
    price_gap_amount: Optional[Decimal] = None
    price_gap_ratio: Optional[Decimal] = None
    observed_sold_quantity: Optional[int] = None
    observed_stock: Optional[int] = None
    observed_competitor_count: Optional[int] = None
    supplier_count: Optional[int] = None
    source_observations_count: int = 1
    unknown_fields: Tuple[str, ...] = field(default_factory=tuple)
    scoring_rationale: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.opportunity_id, field_name="opportunity_id")
        validate_safe_identifier(self.canonical_product_id, field_name="canonical_product_id")
        if not isinstance(self.unknown_fields, tuple):
            object.__setattr__(self, "unknown_fields", tuple(self.unknown_fields))
        if not isinstance(self.scoring_rationale, tuple):
            object.__setattr__(self, "scoring_rationale", tuple(self.scoring_rationale))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly preservando strings para Decimal y fechas ISO."""
        return {
            "opportunity_id": self.opportunity_id,
            "canonical_product_id": self.canonical_product_id,
            "marketplace": self.marketplace,
            "opportunity_type": self.opportunity_type,
            "status": self.status,
            "confidence": self.confidence,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "title": self.title,
            "category": self.category,
            "product_sku": self.product_sku,
            "opportunity_score": str(self.opportunity_score) if self.opportunity_score is not None else None,
            "demand_intensity": self.demand_intensity,
            "competition_density": self.competition_density,
            "estimated_price_amount": str(self.estimated_price_amount) if self.estimated_price_amount is not None else None,
            "estimated_price_currency": self.estimated_price_currency,
            "lowest_competitor_price_amount": str(self.lowest_competitor_price_amount) if self.lowest_competitor_price_amount is not None else None,
            "lowest_competitor_price_currency": self.lowest_competitor_price_currency,
            "buy_box_price_amount": str(self.buy_box_price_amount) if self.buy_box_price_amount is not None else None,
            "buy_box_price_currency": self.buy_box_price_currency,
            "potential_margin_ratio": str(self.potential_margin_ratio) if self.potential_margin_ratio is not None else None,
            "price_gap_amount": str(self.price_gap_amount) if self.price_gap_amount is not None else None,
            "price_gap_ratio": str(self.price_gap_ratio) if self.price_gap_ratio is not None else None,
            "observed_sold_quantity": self.observed_sold_quantity,
            "observed_stock": self.observed_stock,
            "observed_competitor_count": self.observed_competitor_count,
            "supplier_count": self.supplier_count,
            "source_observations_count": self.source_observations_count,
            "unknown_fields": list(self.unknown_fields),
            "scoring_rationale": list(self.scoring_rationale),
        }


@dataclass(frozen=True)
class OpportunityDashboardSummary:
    """
    Resumen agregado del estado del mercado para el tenant consultado.
    Calculado determinísticamente a partir de registros reales.
    """
    tenant_id: str
    total_opportunities: int
    high_potential_count: int
    medium_potential_count: int
    low_potential_count: int
    average_opportunity_score: Optional[Decimal]
    opportunities_by_marketplace: Mapping[str, int]
    opportunities_by_category: Mapping[str, int]
    opportunities_by_type: Mapping[str, int]
    opportunities_by_status: Mapping[str, int]
    opportunities_by_confidence: Mapping[str, int]
    newest_detected_at: Optional[datetime] = None
    oldest_detected_at: Optional[datetime] = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.opportunities_by_marketplace, MappingProxyType):
            object.__setattr__(self, "opportunities_by_marketplace", deep_freeze(dict(self.opportunities_by_marketplace)))
        if not isinstance(self.opportunities_by_category, MappingProxyType):
            object.__setattr__(self, "opportunities_by_category", deep_freeze(dict(self.opportunities_by_category)))
        if not isinstance(self.opportunities_by_type, MappingProxyType):
            object.__setattr__(self, "opportunities_by_type", deep_freeze(dict(self.opportunities_by_type)))
        if not isinstance(self.opportunities_by_status, MappingProxyType):
            object.__setattr__(self, "opportunities_by_status", deep_freeze(dict(self.opportunities_by_status)))
        if not isinstance(self.opportunities_by_confidence, MappingProxyType):
            object.__setattr__(self, "opportunities_by_confidence", deep_freeze(dict(self.opportunities_by_confidence)))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "tenant_id": self.tenant_id,
            "total_opportunities": self.total_opportunities,
            "high_potential_count": self.high_potential_count,
            "medium_potential_count": self.medium_potential_count,
            "low_potential_count": self.low_potential_count,
            "average_opportunity_score": str(self.average_opportunity_score) if self.average_opportunity_score is not None else None,
            "opportunities_by_marketplace": dict(self.opportunities_by_marketplace),
            "opportunities_by_category": dict(self.opportunities_by_category),
            "opportunities_by_type": dict(self.opportunities_by_type),
            "opportunities_by_status": dict(self.opportunities_by_status),
            "opportunities_by_confidence": dict(self.opportunities_by_confidence),
            "newest_detected_at": self.newest_detected_at.isoformat() if self.newest_detected_at else None,
            "oldest_detected_at": self.oldest_detected_at.isoformat() if self.oldest_detected_at else None,
            "generated_at": self.generated_at.isoformat(),
        }


@dataclass(frozen=True)
class OpportunityDashboardDetail:
    """
    Vista detallada de una oportunidad de mercado.
    Incluye métricas observadas vs derivadas y desglose de explicabilidad estructurada.
    """
    item: OpportunityDashboardItem
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    scoring_rationale: Tuple[str, ...] = field(default_factory=tuple)
    observed_metrics_detail: Mapping[str, Any] = field(default_factory=dict)
    derived_metrics_detail: Mapping[str, Any] = field(default_factory=dict)
    explanation: Optional[Dict[str, Any]] = None
    provenance: str = "LIVE"
    correlation_id: str = "default-correlation"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))
        if not isinstance(self.scoring_rationale, tuple):
            object.__setattr__(self, "scoring_rationale", tuple(self.scoring_rationale))
        sanitized_obs = sanitize_security_data(dict(self.observed_metrics_detail))
        sanitized_der = sanitize_security_data(dict(self.derived_metrics_detail))
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "observed_metrics_detail", deep_freeze(sanitized_obs))
        object.__setattr__(self, "derived_metrics_detail", deep_freeze(sanitized_der))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "item": self.item.to_dict(),
            "reasons": list(self.reasons),
            "scoring_rationale": list(self.scoring_rationale),
            "observed_metrics_detail": _unwrap_mapping_proxies(self.observed_metrics_detail),
            "derived_metrics_detail": _unwrap_mapping_proxies(self.derived_metrics_detail),
            "explanation": _unwrap_mapping_proxies(self.explanation) if self.explanation else None,
            "provenance": self.provenance,
            "correlation_id": self.correlation_id,
            "metadata": _unwrap_mapping_proxies(self.metadata),
        }


@dataclass(frozen=True)
class OpportunityDashboardQuery:
    """
    Parámetros de filtrado, ordenación y paginación para el Opportunity Dashboard.
    Paginación acotada estrictamente (1 <= page, 1 <= page_size <= 100).
    """
    category: Optional[str] = None
    marketplace: Optional[str] = None
    opportunity_type: Optional[str] = None
    status: Optional[str] = None
    confidence: Optional[str] = None
    min_score: Optional[Decimal] = None
    max_score: Optional[Decimal] = None
    min_margin: Optional[Decimal] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    search_text: Optional[str] = None
    sort_by: OpportunitySortField = OpportunitySortField.OPPORTUNITY_SCORE
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

        if not isinstance(self.sort_by, OpportunitySortField):
            try:
                object.__setattr__(self, "sort_by", OpportunitySortField(str(self.sort_by).lower()))
            except ValueError:
                object.__setattr__(self, "sort_by", OpportunitySortField.OPPORTUNITY_SCORE)

        if not isinstance(self.sort_order, SortOrder):
            try:
                object.__setattr__(self, "sort_order", SortOrder(str(self.sort_order).lower()))
            except ValueError:
                object.__setattr__(self, "sort_order", SortOrder.DESC)

        if self.search_text is not None:
            cleaned_search = self.search_text.strip()
            object.__setattr__(self, "search_text", cleaned_search if cleaned_search else None)


@dataclass(frozen=True)
class OpportunityDashboardPage:
    """
    Página estructurada de resultados paginados del dashboard.
    """
    items: Tuple[OpportunityDashboardItem, ...]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_previous: bool

    def __post_init__(self):
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la página a diccionario JSON."""
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": self.total_count,
            "page": self.page,
            "page_size": self.page_size,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_previous": self.has_previous,
        }


@dataclass(frozen=True)
class OpportunityComparisonView:
    """
    Vista de comparación estructurada entre 2 o más oportunidades de mercado.
    """
    candidate_ids: Tuple[str, ...]
    best_candidate_id: Optional[str]
    dimensions: Tuple[Dict[str, Any], ...]
    comparison_summary: str
    why_winner: str
    items: Tuple[OpportunityDashboardItem, ...]
    compared_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.candidate_ids, tuple):
            object.__setattr__(self, "candidate_ids", tuple(self.candidate_ids))
        if not isinstance(self.dimensions, tuple):
            object.__setattr__(self, "dimensions", tuple(self.dimensions))
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la vista de comparación a diccionario JSON."""
        return {
            "candidate_ids": list(self.candidate_ids),
            "best_candidate_id": self.best_candidate_id,
            "dimensions": list(self.dimensions),
            "comparison_summary": self.comparison_summary,
            "why_winner": self.why_winner,
            "items": [item.to_dict() for item in self.items],
            "compared_at": self.compared_at.isoformat(),
        }
