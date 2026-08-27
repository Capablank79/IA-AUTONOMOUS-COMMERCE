import pytest
from decimal import Decimal
from datetime import datetime
from src.domain.market_intelligence.models import (
    MarketListing,
    Marketplace,
    Money,
    MarketSnapshot,
    SearchCriteria,
    TrendSignal
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

def test_market_listing_none_quantities():
    listing = MarketListing(
        external_id="ML-123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Product",
        price=Money(amount=Decimal("500.0"), currency="ARS"),
        sold_quantity=None,
        available_quantity=0,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={"free": True},
        category="CAT-1"
    )
    assert listing.sold_quantity is None
    assert listing.available_quantity == 0

def test_market_listing_zero_sold_quantity():
    listing = MarketListing(
        external_id="ML-123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Product",
        price=Money(amount=Decimal("500.0"), currency="ARS"),
        sold_quantity=0,
        available_quantity=5,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={"free": True},
        category="CAT-1"
    )
    assert listing.sold_quantity == 0

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


def test_trend_signal_valid():
    signal = TrendSignal(
        keyword="aspiradora",
        rank=2,
        matched=True,
        trend_score=Decimal("0.98"),
    )

    assert signal.keyword == "aspiradora"
    assert signal.rank == 2
    assert signal.matched is True
    assert signal.trend_score == Decimal("0.98")


def test_trend_signal_rejects_invalid_score():
    with pytest.raises(ValueError):
        TrendSignal(
            keyword="aspiradora",
            rank=2,
            matched=True,
            trend_score=Decimal("1.5"),
        )
