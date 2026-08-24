import pytest
from decimal import Decimal
from datetime import datetime
from src.domain.market_intelligence.models import (
    MarketListing,
    Marketplace,
    Money,
    MarketSnapshot,
    SearchCriteria
)

def test_money_valid():
    m = Money(amount=Decimal("100.0"), currency="USD")
    assert m.amount == Decimal("100.0")
    assert m.currency == "USD"

def test_money_invalid_price():
    with pytest.raises(ValueError, match="Price must be greater than zero"):
        Money(amount=Decimal("-10.0"), currency="USD")

def test_market_listing_valid():
    listing = MarketListing(
        external_id="ML-123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Product",
        price=Money(amount=Decimal("500.0"), currency="ARS"),
        sold_quantity=10,
        available_quantity=5,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={"free": True},
        category="CAT-1"
    )
    assert listing.external_id == "ML-123"
    assert listing.price.amount == Decimal("500.0")

def test_market_listing_invalid_quantities():
    with pytest.raises(ValueError, match="sold_quantity cannot be negative"):
        MarketListing(
            external_id="ML-123",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Test Product",
            price=Money(amount=Decimal("500.0"), currency="ARS"),
            sold_quantity=-1,
            available_quantity=5,
            seller_id="SELLER-1",
            condition="new",
            shipping_info={},
            category="CAT-1"
        )

def test_market_snapshot_valid():
    criteria = SearchCriteria(query="python", marketplace=Marketplace.MERCADO_LIBRE)
    snapshot = MarketSnapshot(
        snapshot_id="snap-001",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[],
        total_results=0
    )
    assert snapshot.snapshot_id == "snap-001"
    assert snapshot.total_results == 0
