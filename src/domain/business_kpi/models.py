"""
Modelos de Dominio y ViewModels Seguros para Q.6 — Business KPIs & Cross-Domain Summary (Hito Q — Business Intelligence).

Principios Q.6:
1. CONSULTATIVE & DERIVED ONLY: No es source-of-truth. Agrega y proyecta hechos desde Q.1 a Q.5.
   No ejecuta motores de pricing, scraping, orquestación ni compras.
2. TENANT ISOLATION (O.1): Aislamiento multi-tenant estricto. Tenant A jamás ve datos ni KPIs de Tenant B.
3. UNKNOWN != ZERO: Faltan inputs => UNKNOWN (None). 0 sólo cuando un conteo o ratio es legítimamente cero.
4. SAFE RATIOS & DENOMINATORS: Denominador cero o ausente => UNKNOWN. Misiones en ejecución o pendientes no entran como failure en success rate.
5. MONEY EN DECIMAL & NO CROSS-CURRENCY MIXING: Precios y costos siempre en Decimal con divisa explícita.
   Monedas heterogéneas se desglosan por divisa o se marcan NOT_COMPARABLE.
6. EXPLAINABILITY & CONFIDENCE: Todo KPI expone su id, status, valor, unidad, source, fórmula, confianza y timestamps.
7. SENSITIVE DATA DEFENSE (N.9): No expone claves API, secretos, datos privados de proveedores, PII de clientes ni CoT privado.
8. NO GATE P: No implementa compuertas ni etapas posteriores al Hito Q.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, List, Sequence, Union

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


class KPIStatus(str, Enum):
    """Estado de disponibilidad y cálculo del KPI."""
    CALCULATED = "CALCULATED"
    UNKNOWN = "UNKNOWN"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_COMPARABLE_CURRENCY = "NOT_COMPARABLE_CURRENCY"


class KPIConfidence(str, Enum):
    """Nivel de certeza y completitud de los datos subyacentes del KPI."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class KPIUnit(str, Enum):
    """Unidades canónicas de métricas KPI."""
    COUNT = "COUNT"
    PERCENTAGE = "PERCENTAGE"
    RATIO = "RATIO"
    CURRENCY = "CURRENCY"
    SCORE = "SCORE"
    SECONDS = "SECONDS"


class KPIDomain(str, Enum):
    """Dominio de negocio de procedencia de la métrica."""
    OPPORTUNITY = "OPPORTUNITY"
    SUPPLIER = "SUPPLIER"
    PROFIT = "PROFIT"
    MISSION = "MISSION"
    AGENT_COST = "AGENT_COST"
    CROSS_DOMAIN = "CROSS_DOMAIN"
    DATA_QUALITY = "DATA_QUALITY"


@dataclass(frozen=True)
class BusinessKPIValue:
    """
    Representación inmutable y segura del valor calculado o desconocido de un KPI de negocio.
    """
    kpi_id: str
    name: str
    domain: KPIDomain
    status: KPIStatus
    value: Optional[Decimal] = None
    currency: Optional[str] = None
    unit: KPIUnit = KPIUnit.COUNT
    confidence: KPIConfidence = KPIConfidence.UNKNOWN
    source: str = ""
    formula_version: str = "1.0.0"
    formula_description: str = ""
    inputs_present: Tuple[str, ...] = field(default_factory=tuple)
    inputs_missing: Tuple[str, ...] = field(default_factory=tuple)
    calculated_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.kpi_id, field_name="kpi_id")
        if not isinstance(self.domain, KPIDomain):
            try:
                object.__setattr__(self, "domain", KPIDomain(str(self.domain).upper()))
            except ValueError:
                object.__setattr__(self, "domain", KPIDomain.CROSS_DOMAIN)
        if not isinstance(self.status, KPIStatus):
            try:
                object.__setattr__(self, "status", KPIStatus(str(self.status).upper()))
            except ValueError:
                object.__setattr__(self, "status", KPIStatus.UNKNOWN)
        if not isinstance(self.unit, KPIUnit):
            try:
                object.__setattr__(self, "unit", KPIUnit(str(self.unit).upper()))
            except ValueError:
                object.__setattr__(self, "unit", KPIUnit.COUNT)
        if not isinstance(self.confidence, KPIConfidence):
            try:
                object.__setattr__(self, "confidence", KPIConfidence(str(self.confidence).upper()))
            except ValueError:
                object.__setattr__(self, "confidence", KPIConfidence.UNKNOWN)
        if not isinstance(self.inputs_present, tuple):
            object.__setattr__(self, "inputs_present", tuple(self.inputs_present))
        if not isinstance(self.inputs_missing, tuple):
            object.__setattr__(self, "inputs_missing", tuple(self.inputs_missing))
        
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario JSON-friendly sin CoT ni credenciales."""
        return {
            "kpi_id": self.kpi_id,
            "name": self.name,
            "domain": self.domain.value,
            "status": self.status.value,
            "value": str(self.value) if self.value is not None else None,
            "currency": self.currency,
            "unit": self.unit.value,
            "confidence": self.confidence.value,
            "source": self.source,
            "formula_version": self.formula_version,
            "formula_description": self.formula_description,
            "inputs_present": list(self.inputs_present),
            "inputs_missing": list(self.inputs_missing),
            "calculated_at": self.calculated_at.isoformat() if self.calculated_at else None,
            "metadata": _unwrap_mapping_proxies(self.metadata),
        }


# Alias de modelo explícito
BusinessKPI = BusinessKPIValue


@dataclass(frozen=True)
class BusinessKPIDomainBreakdown:
    """
    Desglose estructurado de un dominio de negocio con sus KPIs asociados y estado de completitud.
    """
    domain: KPIDomain
    total_metrics: int
    calculated_metrics: int
    unknown_metrics: int
    completeness_score_pct: Optional[Decimal]
    metrics: Tuple[BusinessKPIValue, ...] = field(default_factory=tuple)
    summary_notes: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.metrics, tuple):
            object.__setattr__(self, "metrics", tuple(self.metrics))
        if not isinstance(self.summary_notes, tuple):
            object.__setattr__(self, "summary_notes", tuple(self.summary_notes))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain.value,
            "total_metrics": self.total_metrics,
            "calculated_metrics": self.calculated_metrics,
            "unknown_metrics": self.unknown_metrics,
            "completeness_score_pct": str(self.completeness_score_pct) if self.completeness_score_pct is not None else None,
            "metrics": [m.to_dict() for m in self.metrics],
            "summary_notes": list(self.summary_notes),
        }


@dataclass(frozen=True)
class BusinessKPIComparison:
    """
    Comparación temporal determinista entre período actual y anterior (o dos ventanas).
    Si faltan datos históricos o el denominador previo es 0 => delta_pct = UNKNOWN.
    """
    kpi_id: str
    current_value: Optional[Decimal]
    previous_value: Optional[Decimal]
    delta_absolute: Optional[Decimal]
    delta_pct: Optional[Decimal]
    is_comparable: bool
    status: KPIStatus
    reason_not_comparable: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kpi_id": self.kpi_id,
            "current_value": str(self.current_value) if self.current_value is not None else None,
            "previous_value": str(self.previous_value) if self.previous_value is not None else None,
            "delta_absolute": str(self.delta_absolute) if self.delta_absolute is not None else None,
            "delta_pct": str(self.delta_pct) if self.delta_pct is not None else None,
            "is_comparable": self.is_comparable,
            "status": self.status.value,
            "reason_not_comparable": self.reason_not_comparable,
        }


@dataclass(frozen=True)
class BusinessKPISummary:
    """
    Resumen ejecutivo cross-domain unificado de indicadores de negocio para una vista de C-level / Operador.
    """
    tenant_id: str
    time_window: str
    period_start: Optional[datetime]
    period_end: Optional[datetime]
    currency_breakdown: Mapping[str, Mapping[str, Any]]
    domain_breakdowns: Mapping[str, BusinessKPIDomainBreakdown]
    kpis: Tuple[BusinessKPIValue, ...]
    overall_readiness_pct: Optional[Decimal]
    data_quality_notes: Tuple[str, ...]
    generated_at: datetime
    drill_down_links: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, field_name="tenant_id")
        if not isinstance(self.kpis, tuple):
            object.__setattr__(self, "kpis", tuple(self.kpis))
        if not isinstance(self.data_quality_notes, tuple):
            object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))
        
        sanitized_curr = sanitize_security_data(dict(self.currency_breakdown))
        sanitized_links = sanitize_security_data(dict(self.drill_down_links))
        object.__setattr__(self, "currency_breakdown", deep_freeze(sanitized_curr))
        object.__setattr__(self, "domain_breakdowns", deep_freeze(dict(self.domain_breakdowns)))
        object.__setattr__(self, "drill_down_links", deep_freeze(sanitized_links))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "time_window": self.time_window,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "currency_breakdown": _unwrap_mapping_proxies(self.currency_breakdown),
            "domain_breakdowns": {k: v.to_dict() for k, v in self.domain_breakdowns.items()},
            "kpis": [k.to_dict() for k in self.kpis],
            "overall_readiness_pct": str(self.overall_readiness_pct) if self.overall_readiness_pct is not None else None,
            "data_quality_notes": list(self.data_quality_notes),
            "generated_at": self.generated_at.isoformat(),
            "drill_down_links": _unwrap_mapping_proxies(self.drill_down_links),
        }


@dataclass(frozen=True)
class BusinessKPICatalogItem:
    """
    Definición canónica en catálogo de un KPI de negocio soportado por el sistema.
    """
    kpi_id: str
    name: str
    domain: KPIDomain
    unit: KPIUnit
    source: str
    formula_version: str
    formula_description: str
    required_inputs: Tuple[str, ...]
    unknown_behavior_description: str
    is_core: bool = True

    def __post_init__(self):
        if not isinstance(self.required_inputs, tuple):
            object.__setattr__(self, "required_inputs", tuple(self.required_inputs))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kpi_id": self.kpi_id,
            "name": self.name,
            "domain": self.domain.value,
            "unit": self.unit.value,
            "source": self.source,
            "formula_version": self.formula_version,
            "formula_description": self.formula_description,
            "required_inputs": list(self.required_inputs),
            "unknown_behavior_description": self.unknown_behavior_description,
            "is_core": self.is_core,
        }


@dataclass(frozen=True)
class BusinessKPIQuery:
    """
    Parámetros de consulta y filtrado para KPIs de negocio ejecutivos.
    """
    time_window: str = "30d"  # "24h", "7d", "30d", "custom"
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    marketplace: Optional[str] = None
    category: Optional[str] = None
    mission_type: Optional[str] = None
    currency: Optional[str] = None
    domain: Optional[str] = None

    def __post_init__(self):
        cleaned_window = str(self.time_window).lower().strip()
        if cleaned_window not in {"24h", "7d", "30d", "custom"}:
            object.__setattr__(self, "time_window", "30d")
        else:
            object.__setattr__(self, "time_window", cleaned_window)
