import uuid
from datetime import datetime
from src.domain.market_intelligence.models import (
    SearchCriteria,
    MarketSnapshot,
    Marketplace
)
from src.domain.market_intelligence.ports import MarketplaceDataSource
from .client import MercadoLibreClient
from .mapper import MercadoLibreMapper

class MercadoLibreAdapter(MarketplaceDataSource):
    """
    Adapter for Mercado Libre implementation of MarketplaceDataSource.
    """
    def __init__(self, client: MercadoLibreClient):
        self.client = client
        self.mapper = MercadoLibreMapper()

    def fetch_snapshot(self, criteria: SearchCriteria) -> MarketSnapshot:
        try:
            kwargs = {
                "q": criteria.query,
                "category": criteria.category
            }
            if criteria.limit is not None:
                kwargs["limit"] = criteria.limit
                
            raw_data = self.client.search(**kwargs)
            
            listings = [
                self.mapper.to_domain(item)
                for item in raw_data.get("results", [])
            ]
            
            paging = raw_data.get("paging", {})
            total_results = int(paging.get("total", len(listings)))
            
            return MarketSnapshot(
                snapshot_id=str(uuid.uuid4()),
                timestamp=datetime.utcnow(),
                search_criteria=criteria,
                marketplace=Marketplace.MERCADO_LIBRE,
                listings=listings,
                total_results=total_results
            )
        except Exception as e:
            # Re-wrap or handle errors as needed for the port contract
            raise RuntimeError(f"MercadoLibreAdapter failed to fetch snapshot: {e}")
