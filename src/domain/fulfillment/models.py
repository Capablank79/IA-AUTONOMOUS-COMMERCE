from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple

from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import SalesChannel
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class ShipmentStatus(str, Enum):
    """
    Estado del ciclo de vida logístico y de despacho de un envío (Shipment).
    Independiente del OrderStatus y PaymentStatus.
    """
    PENDING = "PENDING"
    READY_TO_SHIP = "READY_TO_SHIP"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ShippingServiceLevel(str, Enum):
    """
    Nivel de servicio logístico reportado por el canal o transportista.
    """
    STANDARD = "STANDARD"
    EXPRESS = "EXPRESS"
    SAME_DAY = "SAME_DAY"
    NEXT_DAY = "NEXT_DAY"
    ME2_DROP_OFF = "ME2_DROP_OFF"
    ME2_CROSS_DOCKING = "ME2_CROSS_DOCKING"
    ME2_FULFILLMENT = "ME2_FULFILLMENT"
    ME2_FLEX = "ME2_FLEX"
    CUSTOM = "CUSTOM"
    UNKNOWN = "UNKNOWN"


class TrackingStatus(str, Enum):
    """
    Estado específico de un hito o evento de tracking en la cadena de transporte.
    """
    PENDING = "PENDING"
    LABEL_CREATED = "LABEL_CREATED"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    AT_DISTRIBUTION_CENTER = "AT_DISTRIBUTION_CENTER"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    DELIVERY_ATTEMPT_FAILED = "DELIVERY_ATTEMPT_FAILED"
    RETURNED_TO_SENDER = "RETURNED_TO_SENDER"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class LabelFormat(str, Enum):
    """
    Formato físico o digital de la etiqueta de despacho generada o entregada.
    """
    PDF = "PDF"
    ZPL2 = "ZPL2"
    PNG = "PNG"
    THERMAL_RAW = "THERMAL_RAW"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    UNKNOWN = "UNKNOWN"


class LabelStatus(str, Enum):
    """
    Estado de disponibilidad de la etiqueta de envío.
    """
    NOT_GENERATED = "NOT_GENERATED"
    GENERATING = "GENERATING"
    READY = "READY"
    PRINTED = "PRINTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class FulfillmentErrorCategory(str, Enum):
    """
    Taxonomía determinista de errores en operaciones de fulfillment y logística.
    """
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    IDEMPOTENCY_VIOLATION = "IDEMPOTENCY_VIOLATION"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FulfillmentError:
    """
    Error inmutable y estructurado derivado de operaciones de fulfillment.
    """
    category: FulfillmentErrorCategory
    message: str
    code: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self):
        if not self.message or not self.message.strip():
            raise ValueError("FulfillmentError message cannot be empty")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class ShippingLabel:
    """
    Representación inmutable de la etiqueta de envío cuando el canal o carrier la soporte.
    No almacena secretos ni credenciales.
    """
    label_id: str
    external_reference: Optional[str] = None
    status: LabelStatus = LabelStatus.READY
    format: LabelFormat = LabelFormat.PDF
    url: Optional[str] = None
    file_content_ref: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH

    def __post_init__(self):
        if not self.label_id or not self.label_id.strip():
            raise ValueError("label_id cannot be empty")


@dataclass(frozen=True)
class TrackingEvent:
    """
    Observación inmutable y fechada de un evento de seguimiento logístico.
    Conserva procedencia, nivel de confianza y correlación.
    """
    event_id: str
    shipment_id: str
    external_shipment_id: Optional[str]
    status: TrackingStatus
    normalized_status: ShipmentStatus
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    location: Optional[str] = None
    description: Optional[str] = None
    source: str = "CHANNEL_API"
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH
    correlation_id: str = ""
    idempotency_key: str = ""
    raw_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.event_id or not self.event_id.strip():
            raise ValueError("event_id cannot be empty")
        if not self.shipment_id or not self.shipment_id.strip():
            raise ValueError("shipment_id cannot be empty")
        if not isinstance(self.raw_payload, MappingProxyType):
            object.__setattr__(self, "raw_payload", MappingProxyType(dict(self.raw_payload)))


@dataclass(frozen=True)
class Shipment:
    """
    Entidad inmutable que modela un envío comercial (Shipment) asociado a una o más órdenes.
    """
    shipment_id: str
    external_shipment_id: str
    order_id: str
    external_order_id: str
    channel: SalesChannel
    status: ShipmentStatus
    carrier: Optional[str] = None
    service_level: ShippingServiceLevel = ShippingServiceLevel.STANDARD
    tracking_number: Optional[str] = None
    tracking_url: Optional[str] = None
    label: Optional[ShippingLabel] = None
    tracking_events: Sequence[TrackingEvent] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    correlation_id: str = ""
    idempotency_key: str = ""
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH
    raw_reference: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.shipment_id or not self.shipment_id.strip():
            raise ValueError("shipment_id cannot be empty")
        if not self.external_shipment_id or not self.external_shipment_id.strip():
            raise ValueError("external_shipment_id cannot be empty")
        if not self.order_id or not self.order_id.strip():
            raise ValueError("order_id cannot be empty")
        if not self.external_order_id or not self.external_order_id.strip():
            raise ValueError("external_order_id cannot be empty")
        if not isinstance(self.tracking_events, tuple):
            object.__setattr__(self, "tracking_events", tuple(self.tracking_events))
        if not isinstance(self.raw_reference, MappingProxyType):
            object.__setattr__(self, "raw_reference", MappingProxyType(dict(self.raw_reference)))

    @property
    def is_terminal(self) -> bool:
        """Determina si el envío llegó a un estado final."""
        return self.status in (ShipmentStatus.DELIVERED, ShipmentStatus.CANCELLED)

    @property
    def latest_tracking_event(self) -> Optional[TrackingEvent]:
        if not self.tracking_events:
            return None
        return sorted(self.tracking_events, key=lambda e: e.timestamp, reverse=True)[0]


@dataclass(frozen=True)
class ShipmentQueryResult:
    """
    Resultado estructurado de consulta de shipments desde un canal externo o búsqueda interna.
    """
    shipments: Sequence[Shipment] = field(default_factory=tuple)
    total_count: int = 0
    channel: SalesChannel = field(default_factory=lambda: SalesChannel(channel_id="GENERIC", name="Generic"))
    errors: Sequence[FulfillmentError] = field(default_factory=tuple)
    is_unknown: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.shipments, tuple):
            object.__setattr__(self, "shipments", tuple(self.shipments))
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def is_success(self) -> bool:
        return not self.is_unknown and len(self.errors) == 0


@dataclass(frozen=True)
class FulfillmentReconciliationReport:
    """
    Reporte determinista de reconciliación entre el estado logístico interno y el estado externo del marketplace/carrier.
    """
    shipment_id: str
    external_shipment_id: str
    order_id: str
    external_order_id: str
    is_reconciled: bool
    internal_status: ShipmentStatus
    external_status: ShipmentStatus
    tracking_reconciled: bool = True
    label_reconciled: bool = True
    discrepancies: Sequence[str] = field(default_factory=tuple)
    requires_action: bool = False
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH
    reconciled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = ""

    def __post_init__(self):
        if not isinstance(self.discrepancies, tuple):
            object.__setattr__(self, "discrepancies", tuple(self.discrepancies))
