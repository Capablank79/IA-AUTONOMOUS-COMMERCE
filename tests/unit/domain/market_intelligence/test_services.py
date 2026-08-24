import pytest
from decimal import Decimal
from datetime import datetime
from src.domain.market_intelligence.models import (
    MarketSnapshot,
    MarketListing,
    Marketplace,
    Money,
    SearchCriteria
)
from src.domain.market_intelligence.services import MarketAnalysisService

@pytest.fixture
def analysis_service():
    return MarketAnalysisService()

def test_analyze_empty_snapshot(analysis_service):
    criteria = SearchCriteria(query="none", marketplace=Marketplace.MERCADO_LIBRE)
    snapshot = MarketSnapshot(
        snapshot_id="snap-empty",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[],
        total_results=0
    )
    opportunities = analysis_service.analyze(snapshot)
    assert opportunities == []

def test_analyze_with_listings(analysis_service):
    criteria = SearchCriteria(query="laptop", marketplace=Marketplace.MERCADO_LIBRE)
    listing1 = MarketListing(
        external_id="L1",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Cheap Laptop",
        price=Money(amount=Decimal("100.0"), currency="USD"),
        sold_quantity=60, # MEDIUM demand
        available_quantity=10,
        seller_id="S1",
        condition="new",
        shipping_info={},
        category="CAT"
    )
    listing2 = MarketListing(
        external_id="L2",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Expensive Laptop",
        price=Money(amount=Decimal("200.0"), currency="USD"),
        sold_quantity=5, # NONE demand
        available_quantity=10,
        seller_id="S2",
        condition="new",
        shipping_info={},
        category="CAT"
    )
    
    # Median is 150
    # L1: 100/150 = 0.66 (UNDER_MARKET)
    # L2: 200/150 = 1.33 (OVER_MARKET)
    
    snapshot = MarketSnapshot(
        snapshot_id="snap-001",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing1, listing2],
        total_results=2
    )
    
    opportunities = analysis_service.analyze(snapshot)
    
    assert len(opportunities) == 2
    
    opp1 = next(o for o in opportunities if o.listing.external_id == "L1")
    assert opp1.demand_signal.label == "MEDIUM"
    assert opp1.price_signal.position == "UNDER_MARKET"
    
    opp2 = next(o for o in opportunities if o.listing.external_id == "L2")
    assert opp2.demand_signal.label == "NONE"
    assert opp2.price_signal.position == "OVER_MARKET"
