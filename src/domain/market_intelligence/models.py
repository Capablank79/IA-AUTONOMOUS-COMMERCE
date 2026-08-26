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

@dataclass(frozen=True)
class MarketListing:
    external_id: str
    marketplace: Marketplace
    title: str
    price: Money
    sold_quantity: int
    available_quantity: int
    seller_id: str
    condition: str
    shipping_info: dict
    category: str

    def __post_init__(self):
        if not self.external_id:
            raise ValueError("external_id must be valid")
        if self.sold_quantity < 0:
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

@dataclass(frozen=True)
class DemandSignal:
    score: Decimal
    label: str

@dataclass(frozen=True)
class PriceSignal:
    ratio: Decimal
    position: str

@dataclass(frozen=True)
class MarketOpportunity:
    snapshot_id: str
    listing: MarketListing
    demand_signal: DemandSignal
    price_signal: PriceSignal
    opportunity_score: Decimal
    detected_at: datetime

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

    def __post_init__(self):
        if not self.product_id:
            raise ValueError("product_id must be valid")
        if not self.title:
            raise ValueError("title must be valid")
        if not self.domain_id:
            raise ValueError("domain_id must be valid")
