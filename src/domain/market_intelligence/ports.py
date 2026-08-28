from typing import Protocol
from .models import CatalogProduct, Marketplace, MarketSnapshot, SearchCriteria, VisitSignal, ReviewSignal, CatalogListingBridge

class MarketplaceDataSource(Protocol):
    def fetch_snapshot(self, criteria: SearchCriteria) -> MarketSnapshot:
        """Fetch a snapshot of the market based on search criteria."""
        ...

class MarketSnapshotRepository(Protocol):
    def save(self, snapshot: MarketSnapshot) -> None:
        """Persist a market snapshot."""
        ...
    
    def get_by_id(self, snapshot_id: str) -> MarketSnapshot:
        """Retrieve a market snapshot by its ID."""
        ...

class ProductCatalogDataSource(Protocol):
    def search_products(
        self,
        query: str,
        marketplace: Marketplace,
        limit: int | None = None,
    ) -> list[CatalogProduct]:
        """Search catalog products in a marketplace."""
        ...

    def get_product(self, product_id: str) -> CatalogProduct:
        """Get details for a specific catalog product."""
        ...

    def get_product_items(self, product_id: str) -> CatalogListingBridge:
        """Get listing/item IDs associated with a catalog product."""
        ...

class VisitsDataSource(Protocol):
    def get_visits(self, item_id: str, window_days: int) -> VisitSignal:
        """Fetch traffic evidence for a specific item over a time window."""
        ...

class ReviewsDataSource(Protocol):
    def get_reviews(self, item_id: str, offset: int = 0, limit: int = 50) -> ReviewSignal:
        """Fetch reviews for a specific item."""
        ...
