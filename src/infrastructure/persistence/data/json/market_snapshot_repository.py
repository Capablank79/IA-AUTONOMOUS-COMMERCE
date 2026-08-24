import json
from pathlib import Path
from decimal import Decimal
from datetime import datetime
from typing import Union
from src.domain.market_intelligence.models import (
    MarketSnapshot,
    MarketListing,
    SearchCriteria,
    Marketplace,
    Money
)
from src.domain.market_intelligence.ports import MarketSnapshotRepository

class JsonMarketSnapshotRepository(MarketSnapshotRepository):
    """
    JSON-based implementation of MarketSnapshotRepository for persistence.
    """
    def __init__(self, storage_path: Union[str, Path]):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def save(self, snapshot: MarketSnapshot) -> None:
        file_path = self.storage_path / f"snapshot_{snapshot.snapshot_id}.json"
        
        data = {
            "snapshot_id": snapshot.snapshot_id,
            "timestamp": snapshot.timestamp.isoformat(),
            "marketplace": snapshot.marketplace.value,
            "total_results": snapshot.total_results,
            "search_criteria": {
                "query": snapshot.search_criteria.query,
                "marketplace": snapshot.search_criteria.marketplace.value,
                "category": snapshot.search_criteria.category,
                "limit": snapshot.search_criteria.limit,
                "min_price": str(snapshot.search_criteria.min_price) if snapshot.search_criteria.min_price else None,
                "max_price": str(snapshot.search_criteria.max_price) if snapshot.search_criteria.max_price else None,
                "condition": snapshot.search_criteria.condition
            },
            "listings": [
                {
                    "external_id": l.external_id,
                    "title": l.title,
                    "price": {"amount": str(l.price.amount), "currency": l.price.currency},
                    "sold_quantity": l.sold_quantity,
                    "available_quantity": l.available_quantity,
                    "seller_id": l.seller_id,
                    "condition": l.condition,
                    "shipping_info": l.shipping_info,
                    "category": l.category
                } for l in snapshot.listings
            ]
        }
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

    def get_by_id(self, snapshot_id: str) -> MarketSnapshot:
        file_path = self.storage_path / f"snapshot_{snapshot_id}.json"
        if not file_path.exists():
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found in {self.storage_path}")
            
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        criteria = SearchCriteria(
            query=data["search_criteria"]["query"],
            marketplace=Marketplace(data["search_criteria"]["marketplace"]),
            category=data["search_criteria"]["category"],
            limit=data["search_criteria"].get("limit"),
            min_price=Decimal(data["search_criteria"]["min_price"]) if data["search_criteria"]["min_price"] else None,
            max_price=Decimal(data["search_criteria"]["max_price"]) if data["search_criteria"]["max_price"] else None,
            condition=data["search_criteria"]["condition"]
        )
        
        listings = [
            MarketListing(
                external_id=l["external_id"],
                marketplace=Marketplace(data["marketplace"]),
                title=l["title"],
                price=Money(amount=Decimal(l["price"]["amount"]), currency=l["price"]["currency"]),
                sold_quantity=l["sold_quantity"],
                available_quantity=l["available_quantity"],
                seller_id=l["seller_id"],
                condition=l["condition"],
                shipping_info=l["shipping_info"],
                category=l["category"]
            ) for l in data["listings"]
        ]
        
        return MarketSnapshot(
            snapshot_id=data["snapshot_id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            search_criteria=criteria,
            marketplace=Marketplace(data["marketplace"]),
            listings=listings,
            total_results=data["total_results"]
        )
