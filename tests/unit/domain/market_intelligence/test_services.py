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


def test_analyze_with_trend_signal():
    criteria = SearchCriteria(
        query="aspiradora",
        marketplace=Marketplace.MERCADO_LIBRE,
    )

    listing = MarketListing(
        external_id="L1",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Aspiradora",
        price=Money(amount=Decimal("100.0"), currency="CLP"),
        sold_quantity=60,
        available_quantity=10,
        seller_id="S1",
        condition="new",
        shipping_info={},
        category="CAT",
    )

    snapshot = MarketSnapshot(
        snapshot_id="snap-trend",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing],
        total_results=1,
        trends=[
            {
                "keyword": "linterna",
                "url": "https://example.com/linterna",
                "rank": 1,
            },
            {
                "keyword": "aspiradora",
                "url": "https://example.com/aspiradora",
                "rank": 2,
            },
        ],
    )

    service = MarketAnalysisService()

    opportunities = service.analyze(snapshot)

    assert len(opportunities) == 1
    assert opportunities[0].trend_signal.keyword == "aspiradora"
    assert opportunities[0].trend_signal.rank == 2
    assert opportunities[0].trend_signal.matched is True
    assert opportunities[0].trend_signal.trend_score == Decimal("0.5")


def test_opportunity_score_increases_with_stronger_trend():
    criteria = SearchCriteria(
        query="aspiradora",
        marketplace=Marketplace.MERCADO_LIBRE,
    )

    listing = MarketListing(
        external_id="L1",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Aspiradora",
        price=Money(amount=Decimal("100.0"), currency="CLP"),
        sold_quantity=60,
        available_quantity=10,
        seller_id="S1",
        condition="new",
        shipping_info={},
        category="CAT",
    )

    trends_strong = [
        {"keyword": f"trend-{i}", "rank": i}
        for i in range(1, 51)
    ]
    trends_strong[0] = {"keyword": "aspiradora", "rank": 1}

    trends_weak = [
        {"keyword": f"trend-{i}", "rank": i}
        for i in range(1, 51)
    ]
    trends_weak[49] = {"keyword": "aspiradora", "rank": 50}

    snapshot_strong = MarketSnapshot(
        snapshot_id="snap-strong",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing],
        total_results=1,
        trends=trends_strong,
    )

    snapshot_weak = MarketSnapshot(
        snapshot_id="snap-weak",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing],
        total_results=1,
        trends=trends_weak,
    )

    service = MarketAnalysisService()

    strong = service.analyze(snapshot_strong)[0]
    weak = service.analyze(snapshot_weak)[0]

    assert strong.trend_signal.trend_score == Decimal("1.00")
    assert weak.trend_signal.trend_score == Decimal("0.02")
    assert strong.opportunity_score > weak.opportunity_score


def test_opportunity_score_without_trend_remains_valid():
    criteria = SearchCriteria(
        query="laptop",
        marketplace=Marketplace.MERCADO_LIBRE,
    )

    listing = MarketListing(
        external_id="L1",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Laptop",
        price=Money(amount=Decimal("100.0"), currency="USD"),
        sold_quantity=60,
        available_quantity=10,
        seller_id="S1",
        condition="new",
        shipping_info={},
        category="CAT",
    )

    snapshot = MarketSnapshot(
        snapshot_id="snap-no-trend",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing],
        total_results=1,
        trends=[],
    )

    service = MarketAnalysisService()

    opportunity = service.analyze(snapshot)[0]

    assert opportunity.trend_signal.matched is False
    assert opportunity.trend_signal.trend_score == Decimal("0")
    assert opportunity.opportunity_score >= Decimal("0")


def test_opportunity_score_increases_with_demand():
    criteria = SearchCriteria(
        query="producto",
        marketplace=Marketplace.MERCADO_LIBRE,
    )

    def make_listing(sold):
        return MarketListing(
            external_id=f"L{sold}",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Producto",
            price=Money(amount=Decimal("100.0"), currency="CLP"),
            sold_quantity=sold,
            available_quantity=10,
            seller_id="S1",
            condition="new",
            shipping_info={},
            category="CAT",
        )

    snapshot = MarketSnapshot(
        snapshot_id="demand-test",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[
            make_listing(5),
            make_listing(60),
            make_listing(200),
        ],
        total_results=3,
        trends=[],
    )

    opportunities = MarketAnalysisService().analyze(snapshot)

    scores = {
        o.listing.sold_quantity: o.opportunity_score
        for o in opportunities
    }

    assert scores[5] < scores[60] < scores[200]


def test_opportunity_score_with_unknown_demand():
    criteria = SearchCriteria(
        query="producto",
        marketplace=Marketplace.MERCADO_LIBRE,
    )

    listing = MarketListing(
        external_id="L-UNKNOWN",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Producto",
        price=Money(amount=Decimal("100.0"), currency="CLP"),
        sold_quantity=None,
        available_quantity=0,
        seller_id="S1",
        condition="new",
        shipping_info={},
        category="CAT",
    )

    snapshot = MarketSnapshot(
        snapshot_id="unknown-demand-test",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing],
        total_results=1,
        trends=[],
    )

    opportunities = MarketAnalysisService().analyze(snapshot)

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.demand_signal.label == "UNKNOWN"
    assert opp.demand_signal.score is None


def test_opportunity_score_increases_with_better_price():
    criteria = SearchCriteria(
        query="producto",
        marketplace=Marketplace.MERCADO_LIBRE,
    )

    def make_listing(price):
        return MarketListing(
            external_id=f"L{price}",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Producto",
            price=Money(amount=Decimal(str(price)), currency="CLP"),
            sold_quantity=60,
            available_quantity=10,
            seller_id="S1",
            condition="new",
            shipping_info={},
            category="CAT",
        )

    snapshot = MarketSnapshot(
        snapshot_id="price-test",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[
            make_listing(50),
            make_listing(100),
            make_listing(150),
        ],
        total_results=3,
        trends=[],
    )

    opportunities = MarketAnalysisService().analyze(snapshot)

    scores = {
        o.listing.price.amount: o.opportunity_score
        for o in opportunities
    }

    assert scores[150] < scores[100] < scores[50]
