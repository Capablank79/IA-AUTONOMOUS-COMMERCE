from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Union

from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import SalesChannel
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class OrderStatus(str, Enum):
    """
    Estado del ciclo de vida comercial general de la orden.
    Separado explícitamente del estado del pago y de la logística.
    """
    RECEIVED = "RECEIVED"
    CONFIRMED = "CONFIRMED"
    PAID = "PAID"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class PaymentStatus(str, Enum):
    """
    Estado financiero del pago de la orden reportado por el canal.
    """
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    UNKNOWN = "UNKNOWN"


class FulfillmentStatus(str, Enum):
    """
    Estado logístico/fulfillment de la orden (referencia de alto nivel para G.6;
    el ciclo operativo completo de empaque, etiquetas y envíos pertenece a G.7).
    """
    PENDING = "PENDING"
    READY_TO_SHIP = "READY_TO_SHIP"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class OrderEventType(str, Enum):
    """
    Tipos deterministas de eventos de órdenes recibidos o derivados.
    """
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_UPDATED = "ORDER_UPDATED"
    ORDER_PAID = "ORDER_PAID"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    PAYMENT_STATUS_CHANGED = "PAYMENT_STATUS_CHANGED"
    FULFILLMENT_STATUS_CHANGED = "FULFILLMENT_STATUS_CHANGED"


class OrderErrorCategory(str, Enum):
    """
    Categorías taxonómicas deterministas de errores en operaciones de órdenes.
    """
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    IDEMPOTENCY_VIOLATION = "IDEMPOTENCY_VIOLATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OrderError:
    """
    Error estructurado resultante de consulta, ingestión o procesamiento de órdenes.
    """
    category: OrderErrorCategory
    message: str
    code: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self):
        if not self.message or not self.message.strip():
            raise ValueError("OrderError message cannot be empty")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class BuyerReference:
    """
    Referencia mínima y anonimizada del comprador.
    Minimización de PII: no almacena datos de pago sensibles ni identificadores innecesarios.
    """
    buyer_id: str
    nickname: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country_id: Optional[str] = None

    def __post_init__(self):
        if not self.buyer_id or not self.buyer_id.strip():
            raise ValueError("buyer_id cannot be empty")


@dataclass(frozen=True)
class OrderItem:
    """
    Línea de ítem/producto dentro de una orden normalizada.
    Inmutable y con precisión decimal para importes económicos.
    """
    item_id: str
    title: str
    quantity: int
    unit_price: Decimal
    currency: str
    external_item_id: Optional[str] = None
    sku: Optional[str] = None
    listing_id: Optional[str] = None
    variation_id: Optional[str] = None

    def __post_init__(self):
        if not self.item_id or not self.item_id.strip():
            raise ValueError("item_id cannot be empty")
        if not self.title or not self.title.strip():
            raise ValueError("title cannot be empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be strictly positive (> 0)")
        if self.unit_price < Decimal("0"):
            raise ValueError("unit_price cannot be negative")
        if not self.currency or not self.currency.strip():
            raise ValueError("currency cannot be empty")

    @property
    def total_amount(self) -> Decimal:
        return self.unit_price * Decimal(self.quantity)


@dataclass(frozen=True)
class ShipmentReference:
    """
    Referencia de envío asociada a la orden (frontera de G.6).
    No contiene lógica de empaque ni generación de etiquetas (G.7).
    """
    shipment_id: Optional[str] = None
    shipping_mode: Optional[str] = None
    logistic_type: Optional[str] = None
    status: FulfillmentStatus = FulfillmentStatus.PENDING
    tracking_number: Optional[str] = None


@dataclass(frozen=True)
class Order:
    """
    Entidad de dominio central e inmutable que representa una orden comercial normalizada.
    Source of Truth para el estado interno y trazabilidad de pedidos.
    """
    order_id: str
    external_order_id: str
    channel: SalesChannel
    status: OrderStatus
    payment_status: PaymentStatus
    fulfillment_status: FulfillmentStatus
    items: Sequence[OrderItem]
    total_amount: Decimal
    currency: str
    buyer: BuyerReference
    shipment: Optional[ShipmentReference] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None
    idempotency_key: str = ""
    correlation_id: str = ""
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH
    raw_reference: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.order_id or not self.order_id.strip():
            raise ValueError("order_id cannot be empty")
        if not self.external_order_id or not self.external_order_id.strip():
            raise ValueError("external_order_id cannot be empty")
        if not self.items or len(self.items) == 0:
            raise ValueError("Order must contain at least one OrderItem")
        if self.total_amount < Decimal("0"):
            raise ValueError("total_amount cannot be negative")
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))
        if not isinstance(self.raw_reference, MappingProxyType):
            object.__setattr__(self, "raw_reference", MappingProxyType(dict(self.raw_reference)))

    @property
    def total_units(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def is_confirmed_and_paid(self) -> bool:
        """Determina si la orden está en estado firme para requerir deducción de inventario."""
        return self.status in (OrderStatus.PAID, OrderStatus.CONFIRMED) and self.payment_status in (
            PaymentStatus.APPROVED,
            PaymentStatus.AUTHORIZED,
        )

    @property
    def is_cancelled(self) -> bool:
        return self.status == OrderStatus.CANCELLED or self.payment_status == PaymentStatus.CANCELLED


@dataclass(frozen=True)
class OrderEvent:
    """
    Evento inmutable de orden recibido o derivado (ingestion / webhook / polling).
    Permite deduplicación estricta y trazabilidad de cambios de estado.
    """
    event_id: str
    event_type: OrderEventType
    external_order_id: str
    channel: SalesChannel
    order: Optional[Order] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    idempotency_key: str = ""
    correlation_id: str = ""
    raw_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.event_id or not self.event_id.strip():
            raise ValueError("event_id cannot be empty")
        if not self.external_order_id or not self.external_order_id.strip():
            raise ValueError("external_order_id cannot be empty")
        if not isinstance(self.raw_payload, MappingProxyType):
            object.__setattr__(self, "raw_payload", MappingProxyType(dict(self.raw_payload)))


@dataclass(frozen=True)
class OrderQueryResult:
    """
    Resultado estructurado e inmutable de una consulta de órdenes (polling o búsqueda por ID).
    """
    orders: Sequence[Order] = field(default_factory=tuple)
    total_count: int = 0
    channel: SalesChannel = field(default_factory=lambda: SalesChannel(channel_id="GENERIC", name="Generic"))
    errors: Sequence[OrderError] = field(default_factory=tuple)
    is_unknown: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.orders, tuple):
            object.__setattr__(self, "orders", tuple(self.orders))
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def is_success(self) -> bool:
        return not self.is_unknown and len(self.errors) == 0


@dataclass(frozen=True)
class OrderReconciliationReport:
    """
    Reporte determinista de reconciliación entre el estado interno y el estado externo del canal.
    """
    order_id: str
    external_order_id: str
    is_reconciled: bool
    internal_status: OrderStatus
    external_status: OrderStatus
    internal_payment_status: PaymentStatus
    external_payment_status: PaymentStatus
    discrepancies: Sequence[str] = field(default_factory=tuple)
    requires_action: bool = False
    reconciled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.discrepancies, tuple):
            object.__setattr__(self, "discrepancies", tuple(self.discrepancies))
