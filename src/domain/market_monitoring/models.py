from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Dict

from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType


class ObservationSourceType(str, Enum):
    MARKETPLACE_API = "MARKETPLACE_API"
    CATALOG_API = "CATALOG_API"
    SEARCH_API = "SEARCH_API"
    SCRAPER = "SCRAPER"
    FIXTURE = "FIXTURE"
    MOCK = "MOCK"
    SIMULATED = "SIMULATED"


class ObservationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    SOURCE_FAILURE = "SOURCE_FAILURE"
    TIMEOUT = "TIMEOUT"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class NormalizedPrice:
    amount: Decimal
    currency: str

    def __post_init__(self):
        if not self.currency or not isinstance(self.currency, str):
            raise ValueError("currency must be a non-empty string")
        if self.amount < Decimal("0"):
            raise ValueError("Price amount cannot be negative")


@dataclass(frozen=True)
class ObservedSellerInfo:
    seller_id: Optional[str] = None
    seller_name: Optional[str] = None
    reputation_level: Optional[str] = None
    is_official_store: Optional[bool] = None
    raw_seller_data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.raw_seller_data, MappingProxyType):
            object.__setattr__(self, "raw_seller_data", MappingProxyType(dict(self.raw_seller_data)))


@dataclass(frozen=True)
class ObservedCompetitionInfo:
    total_competitors: Optional[int] = None
    buy_box_winner_price: Optional[NormalizedPrice] = None
    lowest_competitor_price: Optional[NormalizedPrice] = None
    has_buy_box: Optional[bool] = None

    def __post_init__(self):
        if self.total_competitors is not None and self.total_competitors < 0:
            raise ValueError("total_competitors cannot be negative")


@dataclass(frozen=True)
class MarketObservation:
    """
    Entidad inmutable de Dominio para una Observación de Mercado (Hito J.2).
    Representa hechos capturados directamente de fuentes de mercado o datos normalizados
    asociados, manteniendo rigurosa separación entre datos observados y derivados.
    UNKNOWN != 0 y no inventa datos faltantes.
    """
    observation_id: str
    source: str
    source_type: ObservationSourceType
    observed_at: datetime
    collected_at: datetime
    marketplace: Marketplace
    entity_id: str
    status: ObservationStatus = ObservationStatus.SUCCESS
    product_sku: Optional[str] = None
    category: Optional[str] = None
    title: Optional[str] = None
    price: Optional[NormalizedPrice] = None
    availability: Optional[str] = None
    stock: Optional[int] = None
    sold_quantity: Optional[int] = None
    seller_info: Optional[ObservedSellerInfo] = None
    competition_info: Optional[ObservedCompetitionInfo] = None
    provenance: str = "LIVE"
    confidence: Confidence = Confidence.HIGH
    signal_type: SignalType = SignalType.OBSERVED
    correlation_id: str = "default-correlation"
    idempotency_key: str = ""
    error_message: Optional[str] = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.observation_id or not isinstance(self.observation_id, str):
            raise ValueError("observation_id must be a non-empty string")
        if not self.source or not isinstance(self.source, str):
            raise ValueError("source must be a non-empty string")
        if not self.entity_id or not isinstance(self.entity_id, str):
            raise ValueError("entity_id must be a non-empty string")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware (UTC)")
        if self.collected_at.tzinfo is None:
            raise ValueError("collected_at must be timezone-aware (UTC)")
        if self.stock is not None and self.stock < 0:
            raise ValueError("stock cannot be negative")
        if self.sold_quantity is not None and self.sold_quantity < 0:
            raise ValueError("sold_quantity cannot be negative")

        if not self.idempotency_key:
            # Auto-generar clave determinista de idempotencia
            key = f"{self.source}::{self.marketplace.value}::{self.entity_id}::{self.observed_at.isoformat()}::{self.correlation_id}"
            object.__setattr__(self, "idempotency_key", key)

        if not isinstance(self.raw_payload, MappingProxyType):
            object.__setattr__(self, "raw_payload", MappingProxyType(dict(self.raw_payload)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
