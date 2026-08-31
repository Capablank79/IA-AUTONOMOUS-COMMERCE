from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple, Mapping, Any, Sequence
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence


class SalesChannelType(str, Enum):
    """
    Tipo o categoría del canal comercial.
    Representa un canal abstracto sin URLs, credenciales ni HTTP.
    """
    MARKETPLACE = "MARKETPLACE"
    DIRECT_STORE = "DIRECT_STORE"
    SOCIAL_COMMERCE = "SOCIAL_COMMERCE"
    B2B_PORTAL = "B2B_PORTAL"
    OTHER = "OTHER"


@dataclass(frozen=True)
class SalesChannel:
    """
    Representa un canal comercial de venta independiente del proveedor o marketplace.
    No incluye URLs, tokens, credenciales ni configuración HTTP.
    """
    channel_id: str
    channel_type: SalesChannelType
    name: str
    region: Optional[str] = None
    currency: str = "CLP"
    is_active: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.channel_id or not self.channel_id.strip():
            raise ValueError("channel_id cannot be empty")
        if not self.name or not self.name.strip():
            raise ValueError("name cannot be empty")
        if not self.currency or not self.currency.strip():
            raise ValueError("currency cannot be empty")
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class PublicationStatus(str, Enum):
    """
    Ciclo de vida de una publicación comercial.
    UNKNOWN es un estado de primera clase: un timeout o respuesta ambigua
    no implica FAILED y permite recuperación e idempotencia futura.
    """
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class PublicationErrorCategory(str, Enum):
    """
    Categoría estructurada del error de publicación.
    """
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PublicationError:
    """
    Representa un error estructurado durante el proceso o intento de publicación.
    """
    category: PublicationErrorCategory
    message: str
    code: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self):
        if not self.message or not self.message.strip():
            raise ValueError("PublicationError message cannot be empty")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class ListingDraft:
    """
    Representa una publicación comercial antes de existir externamente en cualquier canal.
    Totalmente desacoplada de esquemas específicos de marketplaces.
    """
    draft_id: str
    product_reference_id: str
    title: str
    description: str
    price: Decimal
    currency: str
    available_quantity: int
    channel: SalesChannel
    images: Tuple[str, ...] = field(default_factory=tuple)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    sku: Optional[str] = None
    category_id: Optional[str] = None
    condition: str = "new"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.draft_id or not self.draft_id.strip():
            raise ValueError("draft_id cannot be empty")
        if not self.product_reference_id or not self.product_reference_id.strip():
            raise ValueError("product_reference_id cannot be empty")
        if not self.title or not self.title.strip():
            raise ValueError("title cannot be empty")
        if self.price <= Decimal("0"):
            raise ValueError("price must be greater than zero")
        if not self.currency or not self.currency.strip():
            raise ValueError("currency cannot be empty")
        if self.available_quantity < 0:
            raise ValueError("available_quantity cannot be negative")
        if not isinstance(self.attributes, MappingProxyType):
            object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if not isinstance(self.images, tuple):
            object.__setattr__(self, "images", tuple(self.images))


@dataclass(frozen=True)
class PublicationRequest:
    """
    Representa la intención explícita e inmutable de publicar:
    WHAT (ListingDraft) + WHERE (SalesChannel) + WITH WHAT DATA / CONTROL (request_id, correlation_id, idempotency_key).
    """
    request_id: str
    draft: ListingDraft
    channel: SalesChannel
    idempotency_key: Optional[str] = None
    correlation_id: Optional[str] = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.request_id or not self.request_id.strip():
            raise ValueError("request_id cannot be empty")
        if self.draft.channel.channel_id != self.channel.channel_id:
            raise ValueError(
                f"Draft channel ({self.draft.channel.channel_id}) must match publication request channel ({self.channel.channel_id})"
            )
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class PublicationResult:
    """
    Resultado formal e inmutable de una operación de publicación.
    Soporta explícitamente PUBLISHED, FAILED y UNKNOWN.
    """
    publication_id: Optional[str]
    channel: SalesChannel
    status: PublicationStatus
    external_reference: Optional[str] = None
    permalink: Optional[str] = None
    published_at: Optional[datetime] = None
    errors: Tuple[PublicationError, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.HIGH

    def __post_init__(self):
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

        # Reglas de consistencia de dominio
        if self.status == PublicationStatus.PUBLISHED:
            if not self.publication_id and not self.external_reference:
                raise ValueError("PUBLISHED status requires publication_id or external_reference")
        elif self.status == PublicationStatus.FAILED:
            if len(self.errors) == 0:
                raise ValueError("FAILED status requires at least one PublicationError")

    @property
    def is_success(self) -> bool:
        return self.status == PublicationStatus.PUBLISHED

    @property
    def is_unknown(self) -> bool:
        return self.status == PublicationStatus.UNKNOWN

    @property
    def is_failed(self) -> bool:
        return self.status in (PublicationStatus.FAILED, PublicationStatus.REJECTED)
