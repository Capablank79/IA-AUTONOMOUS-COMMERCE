from typing import Protocol
from .models import MarketSnapshot, SearchCriteria

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
