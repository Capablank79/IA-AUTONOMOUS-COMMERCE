from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Mapping, Any, Tuple, Union
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel
from src.domain.profit.models import UnitEconomics
from src.domain.publication.models import SalesChannel


class PriceChangeReason(str, Enum):
    """
    Razón comercial/estratégica fundamentada para el cambio de precio.
    """
    COMPETITIVE_MATCH = "COMPETITIVE_MATCH"
    MARGIN_OPTIMIZATION = "MARGIN_OPTIMIZATION"
    PROMOTION = "PROMOTION"
    CLEARANCE = "CLEARANCE"
    COST_INCREASE = "COST_INCREASE"
    POLICY_CORRECTION = "POLICY_CORRECTION"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    DEMAND_SHIFT = "DEMAND_SHIFT"


class PricingStatus(str, Enum):
    """
    Ciclo de vida y estado de una acción de fijación o cambio de precio.
    UNKNOWN es un estado de primera clase: timeout o respuesta ambigua
    requiere verificación previa (VERIFY_CURRENT_PRICE) antes de reintentar.
    """
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class PricingErrorCategory(str, Enum):
    """
    Categorías taxonómicas deterministas de error en operaciones de pricing.
    """
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PricingError:
    """
    Error estructurado resultante de un intento de consulta o cambio de precio.
    """
    category: PricingErrorCategory
    message: str
    code: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self):
        if not self.message or not self.message.strip():
            raise ValueError("PricingError message cannot be empty")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class PricingDecision:
    """
    Representación estructurada e inmutable de una decisión de fijación o ajuste de precio.
    
    Aislamiento y Principios:
    - No guarda un simple número: contiene contexto económico, justificación, evidencia y riesgos.
    - Respeta el price floor determinista (minimum_allowed_price).
    - Permite auditoría de por qué y con qué evidencia se tomó la decisión.
    """
    decision_id: str
    listing_id: str
    channel: SalesChannel
    current_price: Decimal
    proposed_price: Decimal
    minimum_allowed_price: Decimal
    target_price: Optional[Decimal] = None
    currency: str = "CLP"
    product_id: Optional[str] = None
    unit_economics: Optional[UnitEconomics] = None
    expected_margin_pct: Optional[Decimal] = None
    expected_profit_amount: Optional[Decimal] = None
    rationale: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.HIGH
    risk_level: RiskLevel = RiskLevel.LOW
    constraints: Mapping[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.decision_id or not self.decision_id.strip():
            raise ValueError("decision_id cannot be empty")
        if not self.listing_id or not self.listing_id.strip():
            raise ValueError("listing_id cannot be empty")
        if self.current_price <= Decimal("0"):
            raise ValueError("current_price must be greater than zero")
        if self.proposed_price <= Decimal("0"):
            raise ValueError("proposed_price must be greater than zero")
        if self.minimum_allowed_price <= Decimal("0"):
            raise ValueError("minimum_allowed_price must be greater than zero")
        if not self.currency or not self.currency.strip():
            raise ValueError("currency cannot be empty")
        if not isinstance(self.evidence, MappingProxyType):
            object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        if not isinstance(self.constraints, MappingProxyType):
            object.__setattr__(self, "constraints", MappingProxyType(dict(self.constraints)))

    @property
    def price_delta(self) -> Decimal:
        return self.proposed_price - self.current_price

    @property
    def price_delta_amount(self) -> Decimal:
        return self.proposed_price - self.current_price

    @property
    def price_delta_pct(self) -> Decimal:
        if self.current_price == Decimal("0"):
            return Decimal("0")
        return (self.proposed_price - self.current_price) / self.current_price

    @property
    def price_change_percentage(self) -> Decimal:
        return self.price_delta_pct * Decimal("100")

    @property
    def is_below_floor(self) -> bool:
        return self.proposed_price < self.minimum_allowed_price


@dataclass(frozen=True)
class PricingAction:
    """
    Intención explícita de acción de fijación o cambio de precio ejecutable por ActionExecutor.
    Desacoplada del motor de análisis y de la infraestructura HTTP/API.
    """
    action_id: str
    decision_id: str
    listing_id: str
    channel: SalesChannel
    proposed_price: Optional[Decimal] = None
    current_price: Optional[Decimal] = None
    old_price: Optional[Decimal] = None
    new_price: Optional[Decimal] = None
    currency: str = "CLP"
    reason: Union[PriceChangeReason, str] = ""
    request_id: str = ""
    idempotency_key: str = ""
    correlation_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.action_id or not self.action_id.strip():
            raise ValueError("action_id cannot be empty")
        if not self.listing_id or not self.listing_id.strip():
            raise ValueError("listing_id cannot be empty")
        
        target_new = self.new_price if self.new_price is not None else self.proposed_price
        if target_new is None or target_new <= Decimal("0"):
            raise ValueError("proposed_price/new_price must be greater than zero")
            
        if self.new_price is None and self.proposed_price is not None:
            object.__setattr__(self, "new_price", self.proposed_price)
        if self.proposed_price is None and self.new_price is not None:
            object.__setattr__(self, "proposed_price", self.new_price)
        if self.old_price is None and self.current_price is not None:
            object.__setattr__(self, "old_price", self.current_price)
        if self.current_price is None and self.old_price is not None:
            object.__setattr__(self, "current_price", self.old_price)


@dataclass(frozen=True)
class PricingRequest:
    """
    Petición estructurada dirigida al puerto de pricing del marketplace (PricingPort).
    """
    request_id: str
    listing_id: str
    proposed_price: Decimal
    channel: SalesChannel
    current_price: Optional[Decimal] = None
    currency: str = "CLP"
    idempotency_key: str = ""
    correlation_id: str = ""
    action: Optional[PricingAction] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.request_id:
            raise ValueError("request_id is required for PricingRequest")
        if not self.listing_id or not self.listing_id.strip():
            raise ValueError("listing_id cannot be empty")
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class PricingResult:
    """
    Resultado inmutable y estructurado devuelto por el PricingPort.
    """
    pricing_id: Optional[str]
    channel: SalesChannel
    status: PricingStatus
    listing_id: str
    applied_price: Optional[Decimal] = None
    previous_price: Optional[Decimal] = None
    currency: str = "CLP"
    applied_at: Optional[datetime] = None
    request_id: Optional[str] = None
    correlation_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    errors: Tuple[PricingError, ...] = field(default_factory=tuple)
    raw_response: Mapping[str, Any] = field(default_factory=dict)
    raw_response_summary: Mapping[str, Any] = field(default_factory=dict)
    reobservation_hint: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.listing_id or not self.listing_id.strip():
            raise ValueError("listing_id cannot be empty")
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))
        if not isinstance(self.raw_response, MappingProxyType):
            object.__setattr__(self, "raw_response", MappingProxyType(dict(self.raw_response)))
        if not isinstance(self.raw_response_summary, MappingProxyType):
            object.__setattr__(self, "raw_response_summary", MappingProxyType(dict(self.raw_response_summary)))
        if not isinstance(self.reobservation_hint, MappingProxyType):
            object.__setattr__(self, "reobservation_hint", MappingProxyType(dict(self.reobservation_hint)))

    @property
    def is_success(self) -> bool:
        return self.status == PricingStatus.APPLIED

    @property
    def is_unknown(self) -> bool:
        return self.status == PricingStatus.UNKNOWN

    @property
    def old_price(self) -> Optional[Decimal]:
        return self.previous_price

    @property
    def new_price(self) -> Optional[Decimal]:
        return self.applied_price
