"""
Modelos de Dominio y ViewModels Seguros para Q.3 — Profit Dashboard (Hito Q — Business Intelligence).

Principios Q.3:
1. DASHBOARD != PRICING ENGINE: Estrictamente consultivo, proyectivo y explicable. No cambia precios, no publica listings, no negocia cotizaciones con proveedores ni crea estrategias de repricing.
2. TENANT ISOLATION (O.1): Cada consulta opera bajo un TenantContext estricto. Cero fugas cross-tenant en listado, resumen, detalle o comparación.
3. UNKNOWN != 0: Costo ausente != 0. Fee desconocido != 0. Shipping desconocido != free. Tax desconocido != 0. Valores ausentes o inciertos se conservan como None o UNKNOWN, nunca como 0, 0.0 o cadenas vacías. Si faltan componentes necesarios, profit/margin queda UNKNOWN o parcial.
4. MONEY EN DECIMAL: Precios, costos, comisiones, impuestos, ganancias y márgenes siempre en Decimal con divisa explícita. Prohibido float.
5. NO CROSS-CURRENCY ARITHMETIC SIN FX: Si la moneda del precio de venta difiere de los costos y no hay tipo de cambio verificado, la completitud es NOT_COMPARABLE_CURRENCY y los totales no se mezclan.
6. SENSITIVE DATA DEFENSE (N.9): Excluye credenciales de proveedores, tokens de marketplace, secretos internos, datos de pago y CoT privado.
7. DETERMINISMO Y TIE-BREAKING: Ordenación determinista con desempate estable por item_id.
8. PAGINACIÓN ACOTADA: Paginación obligatoria y límites estrictos de tamaño de página (máximo 100).
9. GROSS VS CONTRIBUTION/NET PROFIT: Distinción canónica y honesta entre Gross Profit (Revenue - Landed Cost) y Contribution/Net Profit (Gross Profit - Fees - Taxes - Variable Costs).
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
from src.domain.profit.models import (
    CostComponentType,
    CostComponentStatus,
    LandedCostStatus,
    ProfitStatus,
    ExchangeRate,
    CostComponent,
    SalePrice,
    Revenue,
    LandedCost,
    ProfitResult,
    MarginResult,
    BreakEvenResult,
    UnitEconomics,
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


class ProfitCompleteness(str, Enum):
    """Nivel de completitud financiera canónica del ítem de rentabilidad."""
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_COMPARABLE_CURRENCY = "NOT_COMPARABLE_CURRENCY"


class ProfitSortField(str, Enum):
    """Campos canónicos permitidos para ordenación en el Profit Dashboard."""
    MARGIN = "margin"
    PROFIT = "profit"
    SALE_PRICE = "sale_price"
    UNIT_COST = "unit_cost"
    UPDATED_AT = "updated_at"
    CALCULATED_AT = "calculated_at"
    ITEM_ID = "item_id"


class SortOrder(str, Enum):
    """Dirección de ordenación canónica."""
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class ProfitDashboardItem:
    """
    DTO / View Model seguro e inmutable para un ítem del Profit Dashboard.
    Proyecta información de rentabilidad sin inventar costos ausentes.
    Preserva estrictamente UNKNOWN != 0 y Decimal para cantidades monetarias.
    """
    item_id: str
    product_id: str
    marketplace: str
    currency: str
    completeness: ProfitCompleteness
    calculated_at: datetime
    opportunity_id: Optional[str] = None
    supplier_id: Optional[str] = None
    supplier_name: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    product_sku: Optional[str] = None
    sale_price: Optional[Decimal] = None
    unit_cost: Optional[Decimal] = None
    shipping_cost: Optional[Decimal] = None
    marketplace_fee: Optional[Decimal] = None
    payment_fee: Optional[Decimal] = None
    tax_cost: Optional[Decimal] = None
    other_costs: Optional[Decimal] = None
    total_known_cost: Optional[Decimal] = None
    gross_profit: Optional[Decimal] = None
    contribution_profit: Optional[Decimal] = None
    margin_pct: Optional[Decimal] = None
    markup_pct: Optional[Decimal] = None
    break_even_sale_price: Optional[Decimal] = None
    confidence: Optional[str] = None
    missing_cost_components: Tuple[str, ...] = field(default_factory=tuple)
    unknown_fields: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.item_id, field_name="item_id")
        validate_safe_identifier(self.product_id, field_name="product_id")
        if self.opportunity_id:
            validate_safe_identifier(self.opportunity_id, field_name="opportunity_id")
        if self.supplier_id:
            validate_safe_identifier(self.supplier_id, field_name="supplier_id")
        if not isinstance(self.missing_cost_components, tuple):
            object.__setattr__(self, "missing_cost_components", tuple(self.missing_cost_components))
        if not isinstance(self.unknown_fields, tuple):
            object.__setattr__(self, "unknown_fields", tuple(self.unknown_fields))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly preservando strings para Decimal y fechas ISO."""
        return {
            "item_id": self.item_id,
            "product_id": self.product_id,
            "opportunity_id": self.opportunity_id,
            "supplier_id": self.supplier_id,
            "supplier_name": self.supplier_name,
            "marketplace": self.marketplace,
            "title": self.title,
            "category": self.category,
            "product_sku": self.product_sku,
            "currency": self.currency,
            "completeness": self.completeness.value if hasattr(self.completeness, "value") else str(self.completeness),
            "calculated_at": self.calculated_at.isoformat() if self.calculated_at else None,
            "sale_price": str(self.sale_price) if self.sale_price is not None else None,
            "unit_cost": str(self.unit_cost) if self.unit_cost is not None else None,
            "shipping_cost": str(self.shipping_cost) if self.shipping_cost is not None else None,
            "marketplace_fee": str(self.marketplace_fee) if self.marketplace_fee is not None else None,
            "payment_fee": str(self.payment_fee) if self.payment_fee is not None else None,
            "tax_cost": str(self.tax_cost) if self.tax_cost is not None else None,
            "other_costs": str(self.other_costs) if self.other_costs is not None else None,
            "total_known_cost": str(self.total_known_cost) if self.total_known_cost is not None else None,
            "gross_profit": str(self.gross_profit) if self.gross_profit is not None else None,
            "contribution_profit": str(self.contribution_profit) if self.contribution_profit is not None else None,
            "margin_pct": str(self.margin_pct) if self.margin_pct is not None else None,
            "markup_pct": str(self.markup_pct) if self.markup_pct is not None else None,
            "break_even_sale_price": str(self.break_even_sale_price) if self.break_even_sale_price is not None else None,
            "confidence": self.confidence,
            "missing_cost_components": list(self.missing_cost_components),
            "unknown_fields": list(self.unknown_fields),
        }


@dataclass(frozen=True)
class ProfitDashboardSummary:
    """
    Resumen agregado del estado de rentabilidad para el tenant consultado.
    Calculado determinísticamente a partir de registros reales.
    """
    tenant_id: str
    total_items: int
    complete_profitability_count: int
    partial_profitability_count: int
    insufficient_data_count: int
    negative_margin_count: int
    positive_margin_count: int
    average_contribution_margin_pct: Optional[Decimal]
    highest_margin_pct: Optional[Decimal]
    lowest_margin_pct: Optional[Decimal]
    items_by_marketplace: Mapping[str, int]
    items_by_category: Mapping[str, int]
    items_by_completeness: Mapping[str, int]
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.items_by_marketplace, MappingProxyType):
            object.__setattr__(self, "items_by_marketplace", deep_freeze(dict(self.items_by_marketplace)))
        if not isinstance(self.items_by_category, MappingProxyType):
            object.__setattr__(self, "items_by_category", deep_freeze(dict(self.items_by_category)))
        if not isinstance(self.items_by_completeness, MappingProxyType):
            object.__setattr__(self, "items_by_completeness", deep_freeze(dict(self.items_by_completeness)))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "tenant_id": self.tenant_id,
            "total_items": self.total_items,
            "complete_profitability_count": self.complete_profitability_count,
            "partial_profitability_count": self.partial_profitability_count,
            "insufficient_data_count": self.insufficient_data_count,
            "negative_margin_count": self.negative_margin_count,
            "positive_margin_count": self.positive_margin_count,
            "average_contribution_margin_pct": str(self.average_contribution_margin_pct) if self.average_contribution_margin_pct is not None else None,
            "highest_margin_pct": str(self.highest_margin_pct) if self.highest_margin_pct is not None else None,
            "lowest_margin_pct": str(self.lowest_margin_pct) if self.lowest_margin_pct is not None else None,
            "items_by_marketplace": dict(self.items_by_marketplace),
            "items_by_category": dict(self.items_by_category),
            "items_by_completeness": dict(self.items_by_completeness),
            "generated_at": self.generated_at.isoformat(),
        }


@dataclass(frozen=True)
class ProfitDashboardDetail:
    """
    Vista detallada y segura de la rentabilidad de un ítem.
    Incluye desglose completo de costos, hechos financieros y lista de componentes faltantes.
    """
    item: ProfitDashboardItem
    cost_breakdown: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    financial_facts: Tuple[str, ...] = field(default_factory=tuple)
    missing_components: Tuple[str, ...] = field(default_factory=tuple)
    formula_breakdown: Optional[str] = None
    associated_opportunity: Optional[Dict[str, Any]] = None
    associated_supplier: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not isinstance(self.cost_breakdown, tuple):
            object.__setattr__(self, "cost_breakdown", tuple(self.cost_breakdown))
        if not isinstance(self.financial_facts, tuple):
            object.__setattr__(self, "financial_facts", tuple(self.financial_facts))
        if not isinstance(self.missing_components, tuple):
            object.__setattr__(self, "missing_components", tuple(self.missing_components))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "item": self.item.to_dict(),
            "cost_breakdown": [_unwrap_mapping_proxies(c) for c in self.cost_breakdown],
            "financial_facts": list(self.financial_facts),
            "missing_components": list(self.missing_components),
            "formula_breakdown": self.formula_breakdown,
            "associated_opportunity": _unwrap_mapping_proxies(self.associated_opportunity) if self.associated_opportunity else None,
            "associated_supplier": _unwrap_mapping_proxies(self.associated_supplier) if self.associated_supplier else None,
        }


@dataclass(frozen=True)
class ProfitComparisonDimension:
    """Dimensión individual de comparación financiera."""
    dimension_name: str
    description: str
    values_by_item: Mapping[str, Any]
    unit: str = ""

    def __post_init__(self):
        if not isinstance(self.values_by_item, MappingProxyType):
            object.__setattr__(self, "values_by_item", deep_freeze(dict(self.values_by_item)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension_name": self.dimension_name,
            "description": self.description,
            "values_by_item": _unwrap_mapping_proxies(dict(self.values_by_item)),
            "unit": self.unit,
        }


@dataclass(frozen=True)
class ProfitComparisonView:
    """
    Vista comparativa multidimensional determinista entre 2 o más oportunidades / variantes financieras.
    No declara ganadores ficticios si los inputs están incompletos.
    """
    item_ids: Tuple[str, ...]
    items: Tuple[ProfitDashboardItem, ...]
    dimensions: Tuple[ProfitComparisonDimension, ...]
    summary_comparison: Mapping[str, Any]
    compared_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.item_ids, tuple):
            object.__setattr__(self, "item_ids", tuple(self.item_ids))
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))
        if not isinstance(self.dimensions, tuple):
            object.__setattr__(self, "dimensions", tuple(self.dimensions))
        if not isinstance(self.summary_comparison, MappingProxyType):
            object.__setattr__(self, "summary_comparison", deep_freeze(dict(self.summary_comparison)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_ids": list(self.item_ids),
            "items": [item.to_dict() for item in self.items],
            "dimensions": [dim.to_dict() for dim in self.dimensions],
            "summary_comparison": _unwrap_mapping_proxies(dict(self.summary_comparison)),
            "compared_at": self.compared_at.isoformat(),
        }


@dataclass(frozen=True)
class ProfitDashboardQuery:
    """Parámetros de consulta estructurados y sanitizados para el Profit Dashboard."""
    marketplace: Optional[str] = None
    category: Optional[str] = None
    supplier_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    currency: Optional[str] = None
    completeness: Optional[ProfitCompleteness] = None
    min_margin_pct: Optional[Decimal] = None
    max_margin_pct: Optional[Decimal] = None
    min_profit_amount: Optional[Decimal] = None
    max_profit_amount: Optional[Decimal] = None
    min_sale_price: Optional[Decimal] = None
    max_sale_price: Optional[Decimal] = None
    search_text: Optional[str] = None
    sort_by: ProfitSortField = ProfitSortField.MARGIN
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
class ProfitDashboardPage:
    """Página paginada de resultados del Profit Dashboard."""
    items: Sequence[ProfitDashboardItem]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_previous: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total_count": self.total_count,
            "page": self.page,
            "page_size": self.page_size,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_previous": self.has_previous,
        }
