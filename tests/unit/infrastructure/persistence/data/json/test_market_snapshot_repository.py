import pytest
from pathlib import Path
from decimal import Decimal
from datetime import datetime
from src.infrastructure.persistence.data.json.market_snapshot_repository import JsonMarketSnapshotRepository
from src.domain.market_intelligence.models import (
    MarketSnapshot,
    MarketListing,
    SearchCriteria,
    Marketplace,
    Money
)

@pytest.fixture
def temp_storage(tmp_path):
    return tmp_path / "snapshots"

def test_save_and_get_snapshot(temp_storage):
    repo = JsonMarketSnapshotRepository(temp_storage)
    
    # Use fixed timestamp for comparison if needed, though fromisoformat handles it
    now = datetime.utcnow()
    
    criteria = SearchCriteria(query="python", marketplace=Marketplace.MERCADO_LIBRE, min_price=Decimal("10.0"))
    listing = MarketListing(
        external_id="L1",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="T1",
        price=Money(amount=Decimal("100.0"), currency="CLP"),
        sold_quantity=10,
        available_quantity=5,
        seller_id="S1",
        condition="new",
        shipping_info={"free": True},
        category="C1"
    )
    
    snapshot = MarketSnapshot(
        snapshot_id="snap-1",
        timestamp=now,
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing],
        total_results=1
    )
    
    repo.save(snapshot)
    
    # Verify file exists
    assert (temp_storage / "snapshot_snap-1.json").exists()
    
    loaded = repo.get_by_id("snap-1")
    
    assert loaded.snapshot_id == "snap-1"
    assert loaded.search_criteria.query == "python"
    assert loaded.search_criteria.min_price == Decimal("10.0")
    assert len(loaded.listings) == 1
    assert loaded.listings[0].external_id == "L1"
    assert loaded.listings[0].price.amount == Decimal("100.0")
    assert loaded.listings[0].shipping_info["free"] is True

def test_get_by_id_not_found(temp_storage):
    repo = JsonMarketSnapshotRepository(temp_storage)
    with pytest.raises(FileNotFoundError):
        repo.get_by_id("non-existent")
