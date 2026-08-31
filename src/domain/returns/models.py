from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple

from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class ReturnStatus(str, Enum):
    """
    Estado del ciclo de vida de una devolución física o solicitud postventa.
    Estrictamente separado de OrderStatus, PaymentStatus y ShipmentStatus.
    """
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    IN_TRANSIT = "IN_TRANSIT"
    RECEIVED = "RECEIVED"
    INSPECTING = "INSPECTING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ClaimStatus(str, Enum):
    """
    Estado formal de un reclamo o disputa postventa iniciado en el marketplace.
    Aislado del estado de envío y orden.
    """
    OPENED = "OPENED"
    IN_REVIEW = "IN_REVIEW"
    MEDIATION = "MEDIATION"
    WAITING_BUYER = "WAITING_BUYER"
    WAITING_SELLER = "WAITING_SELLER"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ClaimStage(str, Enum):
    """
    Etapa procesal de una disputa/reclamo postventa.
    """
    DISPUTE = "DISPUTE"
    CLAIM = "CLAIM"
    MEDIATION = "MEDIATION"
    RESOLVED = "RESOLVED"
    UNKNOWN = "UNKNOWN"


class ReturnReason(str, Enum):
    """
    Taxonomía normalizada de motivos de devolución o reclamo postventa.
    """
    DAMAGED = "DAMAGED"
    DEFECTIVE = "DEFECTIVE"
    NOT_AS_DESCRIBED = "NOT_AS_DESCRIBED"
    WRONG_ITEM = "WRONG_ITEM"
    CHANGED_MIND = "CHANGED_MIND"
    DELIVERY_ISSUE = "DELIVERY_ISSUE"
    MISSING_PARTS = "MISSING_PARTS"
    BUYER_REGRET = "BUYER_REGRET"
    FRAUD_SUSPECTED = "FRAUD_SUSPECTED"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ReturnResolution(str, Enum):
    """
    Tipo de resolución operativa acordada o ejecutada para una devolución/reclamo.
    """
    RETURN_ONLY = "RETURN_ONLY"
    REFUND = "REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    REPLACEMENT = "REPLACEMENT"
    REJECTED = "REJECTED"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class RefundStatus(str, Enum):
    """
    Estado financiero del ciclo de vida de un reembolso postventa.
    Independiente del estado físico de recepción de la devolución.
    """
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


class ReturnErrorCategory(str, Enum):
    """
    Taxonomía determinista de errores en operaciones de devoluciones y reclamos.
    """
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    IDEMPOTENCY_VIOLATION = "IDEMPOTENCY_VIOLATION"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReturnError:
    """
    Error inmutable y estructurado derivado de operaciones de devoluciones y excepciones.
    """
    category: ReturnErrorCategory
    message: str
    code: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self):
        if not self.message or not self.message.strip():
            raise ValueError("ReturnError message cannot be empty")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class RefundDetail:
    """
    Representación inmutable de un reembolso financiero asociado a una devolución o reclamo.
    No persiste PAN, CVV, payment tokens ni datos sensibles.
    """
    refund_id: str
    external_refund_id: Optional[str] = None
    status: RefundStatus = RefundStatus.PENDING
    amount: Decimal = Decimal("0.00")
    currency: str = "USD"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    executed_at: Optional[datetime] = None
    correlation_id: str = ""
    idempotency_key: str = ""
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH
    raw_reference: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.refund_id or not self.refund_id.strip():
            raise ValueError("refund_id cannot be empty")
        if self.amount < Decimal("0.00"):
            raise ValueError("refund amount cannot be negative")
        if not isinstance(self.raw_reference, MappingProxyType):
            object.__setattr__(self, "raw_reference", MappingProxyType(dict(self.raw_reference)))

    @property
    def is_terminal(self) -> bool:
        return self.status in (RefundStatus.CONFIRMED, RefundStatus.FAILED, RefundStatus.CANCELLED)


@dataclass(frozen=True)
class ReturnEvent:
    """
    Observación inmutable y fechada de un evento en el ciclo de vida de la devolución o reclamo.
    """
    event_id: str
    return_id: str
    external_return_id: Optional[str]
    from_status: ReturnStatus
    to_status: ReturnStatus
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
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
        if not self.return_id or not self.return_id.strip():
            raise ValueError("return_id cannot be empty")
        if not isinstance(self.raw_payload, MappingProxyType):
            object.__setattr__(self, "raw_payload", MappingProxyType(dict(self.raw_payload)))


@dataclass(frozen=True)
class Claim:
    """
    Entidad inmutable que representa un reclamo o disputa postventa iniciado por el comprador.
    Aislado formalmente del modelo de Return físico.
    """
    claim_id: str
    external_claim_id: str
    order_id: str
    external_order_id: str
    channel: SalesChannel
    status: ClaimStatus
    stage: ClaimStage = ClaimStage.CLAIM
    reason: ReturnReason = ReturnReason.UNKNOWN
    resolution: Optional[ReturnResolution] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    correlation_id: str = ""
    idempotency_key: str = ""
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH
    raw_reference: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.claim_id or not self.claim_id.strip():
            raise ValueError("claim_id cannot be empty")
        if not self.external_claim_id or not self.external_claim_id.strip():
            raise ValueError("external_claim_id cannot be empty")
        if not self.order_id or not self.order_id.strip():
            raise ValueError("order_id cannot be empty")
        if not self.external_order_id or not self.external_order_id.strip():
            raise ValueError("external_order_id cannot be empty")
        if not isinstance(self.raw_reference, MappingProxyType):
            object.__setattr__(self, "raw_reference", MappingProxyType(dict(self.raw_reference)))

    @property
    def is_terminal(self) -> bool:
        return self.status in (ClaimStatus.CLOSED, ClaimStatus.CANCELLED)


@dataclass(frozen=True)
class Return:
    """
    Entidad inmutable raíz que modela una devolución comercial o excepción operativa postventa.
    Mantiene integridad referencial con Order y Shipment sin fusionar sus ciclos de vida.
    """
    return_id: str
    external_return_id: str
    order_id: str
    external_order_id: str
    channel: SalesChannel
    status: ReturnStatus
    reason: ReturnReason = ReturnReason.UNKNOWN
    resolution: ReturnResolution = ReturnResolution.UNKNOWN
    shipment_id: Optional[str] = None
    external_shipment_id: Optional[str] = None
    claim_id: Optional[str] = None
    refund: Optional[RefundDetail] = None
    events: Sequence[ReturnEvent] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    correlation_id: str = ""
    idempotency_key: str = ""
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH
    raw_reference: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.return_id or not self.return_id.strip():
            raise ValueError("return_id cannot be empty")
        if not self.external_return_id or not self.external_return_id.strip():
            raise ValueError("external_return_id cannot be empty")
        if not self.order_id or not self.order_id.strip():
            raise ValueError("order_id cannot be empty")
        if not self.external_order_id or not self.external_order_id.strip():
            raise ValueError("external_order_id cannot be empty")
        if not isinstance(self.events, tuple):
            object.__setattr__(self, "events", tuple(self.events))
        if not isinstance(self.raw_reference, MappingProxyType):
            object.__setattr__(self, "raw_reference", MappingProxyType(dict(self.raw_reference)))

    @property
    def is_terminal(self) -> bool:
        """Indica si la devolución ha finalizado su ciclo."""
        return self.status in (
            ReturnStatus.RESOLVED,
            ReturnStatus.REJECTED,
            ReturnStatus.CANCELLED,
            ReturnStatus.NOT_APPLICABLE,
        )

    @property
    def latest_event(self) -> Optional[ReturnEvent]:
        if not self.events:
            return None
        return sorted(self.events, key=lambda e: e.timestamp, reverse=True)[0]


@dataclass(frozen=True)
class ReturnQueryResult:
    """
    Resultado estructurado de consulta de devoluciones/reclamos desde un canal externo o repositorio.
    """
    returns: Sequence[Return] = field(default_factory=tuple)
    claims: Sequence[Claim] = field(default_factory=tuple)
    total_count: int = 0
    channel: SalesChannel = field(default_factory=lambda: SalesChannel(channel_id="GENERIC", channel_type=SalesChannelType.OTHER, name="Generic"))
    errors: Sequence[ReturnError] = field(default_factory=tuple)
    is_unknown: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.returns, tuple):
            object.__setattr__(self, "returns", tuple(self.returns))
        if not isinstance(self.claims, tuple):
            object.__setattr__(self, "claims", tuple(self.claims))
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def is_success(self) -> bool:
        return not self.is_unknown and len(self.errors) == 0


@dataclass(frozen=True)
class ReturnReconciliationReport:
    """
    Reporte determinista de reconciliación entre el estado interno persistido y el estado externo del marketplace.
    """
    return_id: str
    external_return_id: str
    order_id: str
    external_order_id: str
    is_reconciled: bool
    internal_status: ReturnStatus
    external_status: ReturnStatus
    internal_refund_status: Optional[RefundStatus] = None
    external_refund_status: Optional[RefundStatus] = None
    refund_reconciled: bool = True
    discrepancies: Sequence[str] = field(default_factory=tuple)
    requires_action: bool = False
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    confidence: Confidence = Confidence.HIGH
    reconciled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = ""

    def __post_init__(self):
        if not isinstance(self.discrepancies, tuple):
            object.__setattr__(self, "discrepancies", tuple(self.discrepancies))
