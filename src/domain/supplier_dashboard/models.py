"""
Modelos de Dominio y ViewModels Seguros para Q.2 — Supplier Dashboard (Hito Q — Business Intelligence).

Principios Q.2:
1. DASHBOARD != SOURCING ENGINE: Estrictamente consultivo, proyectivo y explicable. No ejecuta scraping, no envía mensajes/emails, no negocia ni genera purchase orders.
2. TENANT ISOLATION: Cada consulta opera bajo un TenantContext estricto. Cero fugas cross-tenant en listado, resumen, detalle o comparación.
3. UNKNOWN != 0: Valores ausentes o inciertos (precios, MOQ, lead time, scores, costos de envío) se conservan como None o UNKNOWN, nunca como 0, 0.0 o cadenas vacías. No se asume MOQ=1 ni entrega inmediata si faltan datos.
4. MONEY EN DECIMAL: Costos unitarios, costos de envío y métricas monetarias siempre en Decimal con divisa explícita. Nunca float.
5. SENSITIVE DATA DEFENSE (N.9): Protege datos de contacto (email, teléfono, URLs internas), excluye credenciales de proveedores, tokens y contraseñas.
6. DETERMINISMO Y TIE-BREAKING: Ordenación determinista con desempate estable por supplier_id.
7. PAGINACIÓN ACOTADA: Paginación obligatoria y límites estrictos de tamaño de página (máximo 100).
8. COMPARACIÓN MULTIDIMENSIONAL: Soporte para comparar 2 o más proveedores en dimensiones clave sin inventar un ganador ficticio sin políticas reales.
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
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierStatus,
    SupplierReadiness,
    EvidenceProvenanceType,
    RiskLevel,
    MOQInfo,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
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


class SupplierSortField(str, Enum):
    """Campos canónicos permitidos para ordenación en el Supplier Dashboard."""
    SUPPLIER_SCORE = "supplier_score"
    UNIT_COST = "unit_cost"
    LEAD_TIME = "lead_time"
    RELIABILITY = "reliability"
    LAST_VERIFIED = "last_verified"
    UPDATED_AT = "updated_at"
    NAME = "name"


class SortOrder(str, Enum):
    """Dirección de ordenación canónica."""
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class SupplierDashboardItem:
    """
    DTO / View Model seguro e inmutable para un ítem del Supplier Dashboard.
    Proyecta información de negocio sin exponer secretos ni credenciales de proveedores.
    Preserva estrictamente UNKNOWN != 0 y Decimal para cantidades monetarias.
    """
    supplier_id: str
    name: str
    source: str
    country: Optional[str] = None
    marketplace_url: Optional[str] = None
    product_count: int = 0
    opportunity_count: int = 0
    unit_cost_amount: Optional[Decimal] = None
    currency: Optional[str] = None
    moq: Optional[int] = None
    lead_time_days: Optional[int] = None
    shipping_cost_amount: Optional[Decimal] = None
    reliability_score: Optional[Decimal] = None
    quality_score: Optional[Decimal] = None
    supplier_score: Optional[Decimal] = None
    confidence: Optional[str] = None
    verification_status: Optional[str] = None
    risk_level: Optional[str] = None
    last_verified_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    unknown_fields: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.supplier_id, field_name="supplier_id")
        if not isinstance(self.unknown_fields, tuple):
            object.__setattr__(self, "unknown_fields", tuple(self.unknown_fields))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly preservando strings para Decimal y fechas ISO."""
        return {
            "supplier_id": self.supplier_id,
            "name": self.name,
            "source": self.source,
            "country": self.country,
            "marketplace_url": self.marketplace_url,
            "product_count": self.product_count,
            "opportunity_count": self.opportunity_count,
            "unit_cost_amount": str(self.unit_cost_amount) if self.unit_cost_amount is not None else None,
            "currency": self.currency,
            "moq": self.moq,
            "lead_time_days": self.lead_time_days,
            "shipping_cost_amount": str(self.shipping_cost_amount) if self.shipping_cost_amount is not None else None,
            "reliability_score": str(self.reliability_score) if self.reliability_score is not None else None,
            "quality_score": str(self.quality_score) if self.quality_score is not None else None,
            "supplier_score": str(self.supplier_score) if self.supplier_score is not None else None,
            "confidence": self.confidence,
            "verification_status": self.verification_status,
            "risk_level": self.risk_level,
            "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "unknown_fields": list(self.unknown_fields),
        }


@dataclass(frozen=True)
class SupplierDashboardSummary:
    """
    Resumen agregado del estado del ecosistema de proveedores para el tenant consultado.
    Calculado determinísticamente a partir de registros reales.
    """
    tenant_id: str
    total_suppliers: int
    verified_suppliers: int
    high_rated_suppliers: int
    suppliers_by_country: Mapping[str, int]
    suppliers_by_source: Mapping[str, int]
    suppliers_by_verification: Mapping[str, int]
    suppliers_by_risk: Mapping[str, int]
    average_supplier_score: Optional[Decimal]
    suppliers_with_unknown_critical_fields: int
    newest_updated_at: Optional[datetime] = None
    oldest_updated_at: Optional[datetime] = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.suppliers_by_country, MappingProxyType):
            object.__setattr__(self, "suppliers_by_country", deep_freeze(dict(self.suppliers_by_country)))
        if not isinstance(self.suppliers_by_source, MappingProxyType):
            object.__setattr__(self, "suppliers_by_source", deep_freeze(dict(self.suppliers_by_source)))
        if not isinstance(self.suppliers_by_verification, MappingProxyType):
            object.__setattr__(self, "suppliers_by_verification", deep_freeze(dict(self.suppliers_by_verification)))
        if not isinstance(self.suppliers_by_risk, MappingProxyType):
            object.__setattr__(self, "suppliers_by_risk", deep_freeze(dict(self.suppliers_by_risk)))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly."""
        return {
            "tenant_id": self.tenant_id,
            "total_suppliers": self.total_suppliers,
            "verified_suppliers": self.verified_suppliers,
            "high_rated_suppliers": self.high_rated_suppliers,
            "suppliers_by_country": dict(self.suppliers_by_country),
            "suppliers_by_source": dict(self.suppliers_by_source),
            "suppliers_by_verification": dict(self.suppliers_by_verification),
            "suppliers_by_risk": dict(self.suppliers_by_risk),
            "average_supplier_score": str(self.average_supplier_score) if self.average_supplier_score is not None else None,
            "suppliers_with_unknown_critical_fields": self.suppliers_with_unknown_critical_fields,
            "newest_updated_at": self.newest_updated_at.isoformat() if self.newest_updated_at else None,
            "oldest_updated_at": self.oldest_updated_at.isoformat() if self.oldest_updated_at else None,
            "generated_at": self.generated_at.isoformat(),
        }


@dataclass(frozen=True)
class SupplierDashboardDetail:
    """
    Vista detallada y segura de un proveedor.
    Incluye hechos observados vs derivados, explicabilidad de scoring, riesgos y datos de contacto sanitizados.
    """
    item: SupplierDashboardItem
    contact_name: Optional[str] = None
    contact_email_masked: Optional[str] = None
    contact_phone_masked: Optional[str] = None
    contact_website: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    associated_opportunities: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    associated_products: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    scoring_facts: Tuple[str, ...] = field(default_factory=tuple)
    verification_facts: Tuple[str, ...] = field(default_factory=tuple)
    reliability_facts: Tuple[str, ...] = field(default_factory=tuple)
    risk_facts: Tuple[str, ...] = field(default_factory=tuple)
    unknowns: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def contact(self) -> Optional[Dict[str, Any]]:
        """Retorna diccionario de contacto sanitizado y enmascarado."""
        if not (self.contact_name or self.contact_email_masked or self.contact_phone_masked or self.contact_website):
            return None
        return {
            "name": self.contact_name,
            "email": self.contact_email_masked,
            "phone": self.contact_phone_masked,
            "website": self.contact_website,
        }

    def __post_init__(self):
        if not isinstance(self.associated_opportunities, tuple):
            object.__setattr__(self, "associated_opportunities", tuple(self.associated_opportunities))
        if not isinstance(self.associated_products, tuple):
            object.__setattr__(self, "associated_products", tuple(self.associated_products))
        if not isinstance(self.scoring_facts, tuple):
            object.__setattr__(self, "scoring_facts", tuple(self.scoring_facts))
        if not isinstance(self.verification_facts, tuple):
            object.__setattr__(self, "verification_facts", tuple(self.verification_facts))
        if not isinstance(self.reliability_facts, tuple):
            object.__setattr__(self, "reliability_facts", tuple(self.reliability_facts))
        if not isinstance(self.risk_facts, tuple):
            object.__setattr__(self, "risk_facts", tuple(self.risk_facts))
        if not isinstance(self.unknowns, tuple):
            object.__setattr__(self, "unknowns", tuple(self.unknowns))
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly exponiendo campos de item en raíz y en 'item'."""
        d = self.item.to_dict()
        d.update({
            "item": self.item.to_dict(),
            "contact": self.contact,
            "contact_name": self.contact_name,
            "contact_email_masked": self.contact_email_masked,
            "contact_phone_masked": self.contact_phone_masked,
            "contact_website": self.contact_website,
            "city": self.city,
            "region": self.region,
            "associated_opportunities": list(self.associated_opportunities),
            "associated_products": list(self.associated_products),
            "scoring_facts": list(self.scoring_facts),
            "verification_facts": list(self.verification_facts),
            "reliability_facts": list(self.reliability_facts),
            "risk_facts": list(self.risk_facts),
            "unknowns": list(self.unknowns),
            "metadata": _unwrap_mapping_proxies(self.metadata),
        })
        return d


@dataclass(frozen=True)
class SupplierDashboardQuery:
    """
    Parámetros de filtrado, ordenación y paginación para el Supplier Dashboard.
    Paginación acotada estrictamente (1 <= page, 1 <= page_size <= 100).
    """
    source: Optional[str] = None
    country: Optional[str] = None
    verification_status: Optional[str] = None
    risk_level: Optional[str] = None
    confidence: Optional[str] = None
    min_score: Optional[Decimal] = None
    max_score: Optional[Decimal] = None
    min_supplier_score: Optional[Decimal] = None
    max_supplier_score: Optional[Decimal] = None
    max_unit_cost: Optional[Decimal] = None
    max_moq: Optional[int] = None
    max_lead_time: Optional[int] = None
    max_lead_time_days: Optional[int] = None
    min_confidence: Optional[str] = None
    opportunity_id: Optional[str] = None
    product_reference: Optional[str] = None
    product_sku: Optional[str] = None
    search_text: Optional[str] = None
    sort_by: SupplierSortField = SupplierSortField.SUPPLIER_SCORE
    sort_order: SortOrder = SortOrder.DESC
    page: int = 1
    page_size: int = 20

    def __post_init__(self):
        if self.confidence is not None and self.min_confidence is None:
            object.__setattr__(self, "min_confidence", self.confidence)
        elif self.min_confidence is not None and self.confidence is None:
            object.__setattr__(self, "confidence", self.min_confidence)

        if self.product_reference is not None and self.product_sku is None:
            object.__setattr__(self, "product_sku", self.product_reference)
        elif self.product_sku is not None and self.product_reference is None:
            object.__setattr__(self, "product_reference", self.product_sku)
        if self.min_supplier_score is not None and self.min_score is None:
            object.__setattr__(self, "min_score", self.min_supplier_score)
        elif self.min_score is not None and self.min_supplier_score is None:
            object.__setattr__(self, "min_supplier_score", self.min_score)

        if self.max_supplier_score is not None and self.max_score is None:
            object.__setattr__(self, "max_score", self.max_supplier_score)
        elif self.max_score is not None and self.max_supplier_score is None:
            object.__setattr__(self, "max_supplier_score", self.max_score)

        if self.max_lead_time_days is not None and self.max_lead_time is None:
            object.__setattr__(self, "max_lead_time", self.max_lead_time_days)
        elif self.max_lead_time is not None and self.max_lead_time_days is None:
            object.__setattr__(self, "max_lead_time_days", self.max_lead_time)

        if self.page < 1:
            object.__setattr__(self, "page", 1)
        if self.page_size < 1:
            object.__setattr__(self, "page_size", 20)
        elif self.page_size > 100:
            object.__setattr__(self, "page_size", 100)

        if not isinstance(self.sort_by, SupplierSortField):
            try:
                object.__setattr__(self, "sort_by", SupplierSortField(str(self.sort_by).lower()))
            except ValueError:
                object.__setattr__(self, "sort_by", SupplierSortField.SUPPLIER_SCORE)

        if not isinstance(self.sort_order, SortOrder):
            try:
                object.__setattr__(self, "sort_order", SortOrder(str(self.sort_order).lower()))
            except ValueError:
                object.__setattr__(self, "sort_order", SortOrder.DESC)

        if self.search_text is not None:
            cleaned_search = self.search_text.strip()
            object.__setattr__(self, "search_text", cleaned_search if cleaned_search else None)


@dataclass(frozen=True)
class SupplierDashboardPage:
    """
    Página estructurada de resultados paginados de proveedores.
    """
    items: Tuple[SupplierDashboardItem, ...]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_previous: bool

    @property
    def has_prev(self) -> bool:
        return self.has_previous

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
            "has_prev": self.has_prev,
        }


@dataclass(frozen=True)
class SupplierComparisonDimension:
    """
    Dimensión individual de comparación entre proveedores.
    """
    dimension_name: str
    dimension_label: str
    values: Mapping[str, Any]  # supplier_id -> value
    notes: Optional[str] = None

    @property
    def field(self) -> str:
        return self.dimension_name

    def __post_init__(self):
        if not isinstance(self.values, MappingProxyType):
            object.__setattr__(self, "values", deep_freeze(dict(self.values)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension_name": self.dimension_name,
            "dimension_label": self.dimension_label,
            "field": self.field,
            "values": _unwrap_mapping_proxies(self.values),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class SupplierComparisonView:
    """
    Vista de comparación estructurada entre 2 o más proveedores.
    Compara dimensiones clave respetando UNKNOWN semantics y sin crear vencedores artificiales sin policy.
    """
    supplier_ids: Tuple[str, ...]
    dimensions: Tuple[SupplierComparisonDimension, ...]
    comparison_summary: str
    items: Tuple[SupplierDashboardItem, ...]
    compared_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def supplier_names(self) -> Tuple[str, ...]:
        return tuple(it.name for it in self.items)

    def __post_init__(self):
        if not isinstance(self.supplier_ids, tuple):
            object.__setattr__(self, "supplier_ids", tuple(self.supplier_ids))
        if not isinstance(self.dimensions, tuple):
            object.__setattr__(self, "dimensions", tuple(self.dimensions))
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la vista de comparación a diccionario JSON."""
        return {
            "supplier_ids": list(self.supplier_ids),
            "supplier_names": list(self.supplier_names),
            "dimensions": [dim.to_dict() for dim in self.dimensions],
            "comparison_summary": self.comparison_summary,
            "items": [item.to_dict() for item in self.items],
            "compared_at": self.compared_at.isoformat(),
        }
