from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from enum import Enum

@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        if self.amount <= 0:
            raise ValueError("Price must be greater than zero")

class Marketplace(str, Enum):
    MERCADO_LIBRE = "MERCADO_LIBRE"
    AMAZON = "AMAZON"
    GENERIC = "GENERIC"

class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"

class SignalType(str, Enum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    ESTIMATED = "ESTIMATED"
    INFERRED = "INFERRED"
    RECOMMENDED = "RECOMMENDED"

@dataclass(frozen=True)
class MarketListing:
    external_id: str
    marketplace: Marketplace
    title: str
    price: Money
    sold_quantity: Optional[int]
    available_quantity: int
    seller_id: str
    condition: str
    shipping_info: dict
    category: str

    def __post_init__(self):
        if not self.external_id:
            raise ValueError("external_id must be valid")
        if self.sold_quantity is not None and self.sold_quantity < 0:
            raise ValueError("sold_quantity cannot be negative")
        if self.available_quantity < 0:
            raise ValueError("available_quantity cannot be negative")

@dataclass(frozen=True)
class SearchCriteria:
    query: str
    marketplace: Marketplace
    category: Optional[str] = None
    limit: Optional[int] = None
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    condition: Optional[str] = None

@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_id: str
    timestamp: datetime
    search_criteria: SearchCriteria
    marketplace: Marketplace
    listings: List[MarketListing]
    total_results: int
    trends: List[dict] = field(default_factory=list)

@dataclass(frozen=True)
class DemandSignal:
    score: Optional[Decimal]
    label: str
    confidence: Confidence = Confidence.UNKNOWN
    signal_type: SignalType = SignalType.DERIVED

@dataclass(frozen=True)
class PriceSignal:
    ratio: Decimal
    position: str


@dataclass(frozen=True)
class TrendSignal:
    keyword: str
    rank: int
    matched: bool
    trend_score: Decimal

    def __post_init__(self):
        if self.rank < 0:
            raise ValueError("rank cannot be negative")
        if self.trend_score < Decimal("0") or self.trend_score > Decimal("1"):
            raise ValueError("trend_score must be between 0 and 1")

@dataclass(frozen=True)
class VisitSignal:
    item_id: str
    window: str
    total_visits: Optional[int]
    observed_days: int
    coverage_ratio: float
    source: str
    observed_at: datetime
    confidence: Confidence = Confidence.UNKNOWN
    average_daily_visits: Optional[float] = None
    momentum: Optional[float] = None
    acceleration: Optional[float] = None

    def __post_init__(self):
        if not self.item_id:
            raise ValueError("item_id must be valid")
        if self.total_visits is not None and self.total_visits < 0:
            raise ValueError("total_visits cannot be negative")
        if self.observed_days < 0:
            raise ValueError("observed_days cannot be negative")
        if not (0 <= self.coverage_ratio <= 1):
            raise ValueError("coverage_ratio must be between 0 and 1")
        if self.average_daily_visits is not None and self.average_daily_visits < 0:
            raise ValueError("average_daily_visits cannot be negative")

    @property
    def daily_average(self) -> Optional[float]:
        """Calcula el promedio diario si los datos están presentes."""
        if self.total_visits is not None and self.observed_days > 0:
            return float(self.total_visits) / self.observed_days
        return None

@dataclass(frozen=True)
class Review:
    external_id: str
    rating: int
    text: str
    date: datetime
    reviewable_object: str
    secondary_key: Optional[str] = None
    status: str = "active"

@dataclass(frozen=True)
class ReviewSignal:
    item_id: str
    total_reviews: int
    average_rating: float
    reviews: List[Review]
    paging: dict
    observed_at: datetime
    confidence: Confidence = Confidence.UNKNOWN
    signal_type: SignalType = SignalType.OBSERVED

@dataclass(frozen=True)
class MarketOpportunity:
    snapshot_id: str
    listing: MarketListing
    demand_signal: DemandSignal
    price_signal: PriceSignal
    trend_signal: TrendSignal
    opportunity_score: Decimal
    detected_at: datetime

@dataclass(frozen=True)
class ProductVariant:
    product_id: str
    picker_label: str
    thumbnail: Optional[str] = None
    permalink: Optional[str] = None
    attributes: dict = field(default_factory=dict)

@dataclass(frozen=True)
class ProductPicker:
    picker_id: str
    picker_name: str
    variants: List[ProductVariant]

@dataclass(frozen=True)
class CatalogProduct:
    product_id: str
    marketplace: Marketplace
    title: str
    domain_id: str
    brand: Optional[str]
    model: Optional[str]
    attributes: dict
    thumbnail: Optional[str]
    status: str
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    pickers: List[ProductPicker] = field(default_factory=list)
    buy_box_winner: Optional[MarketListing] = None

    def __post_init__(self):
        if not self.product_id:
            raise ValueError("product_id must be valid")
        if not self.title:
            raise ValueError("title must be valid")
        if not self.domain_id:
            raise ValueError("domain_id must be valid")

@dataclass(frozen=True)
class CatalogListingBridge:
    catalog_product_id: str
    item_ids: List[str]
    observed_at: datetime
    signal_type: SignalType = SignalType.OBSERVED

@dataclass(frozen=True)
class MarketEvidence:
    """
    Agregado conceptual que transporta evidencia de mercado sin tomar decisiones.
    No es un veredicto comercial (opportunity score), sino un input para el Opportunity Engine.
    """
    listing: MarketListing
    traffic_signals: List[VisitSignal] = field(default_factory=list)
    trend_signals: List[TrendSignal] = field(default_factory=list)
    price_signals: List[PriceSignal] = field(default_factory=list)
    demand_signals: List[DemandSignal] = field(default_factory=list)
    review_signals: List[ReviewSignal] = field(default_factory=list)
    confidence: Confidence = Confidence.UNKNOWN
