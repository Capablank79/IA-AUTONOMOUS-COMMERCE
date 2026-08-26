import json
from pathlib import Path

from commerce_lab.server import (
    get_project_status,
    get_experiment_status,
    calculate_profit,
    get_supplier
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SNAPSHOTS_FILE = FIXTURES_DIR / "snapshots.json"

def load_snapshots():
    with open(SNAPSHOTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

snapshots = load_snapshots()

def test_get_project_status_behavior():
    result = get_project_status()
    assert isinstance(result, str)
    assert result == snapshots["get_project_status"]

def test_get_experiment_status_behavior():
    result = get_experiment_status()
    assert isinstance(result, str)
    assert result == snapshots["get_experiment_status"]

def test_calculate_profit_behavior():
    result = calculate_profit()
    assert isinstance(result, str)
    assert result == snapshots["calculate_profit"]


def test_get_supplier_behavior():
    result = get_supplier("001")
    assert isinstance(result, str)
    assert result == snapshots["get_supplier_001"]

def test_get_supplier_with_full_id_behavior():
    # The current implementation uses split("-")[-1], so "supplier-001" and "001" behave the same
    result = get_supplier("supplier-001")
    assert isinstance(result, str)
    assert result == snapshots["get_supplier_001"]

def test_discover_products_behavior(monkeypatch):
    from commerce_lab.server import discover_products
    
    # Mockear el use case para no hacer llamadas reales a Mercado Libre
    from unittest.mock import MagicMock
    mock_use_case = MagicMock()
    monkeypatch.setattr("commerce_lab.server.discover_products_use_case", mock_use_case)
    
    from src.domain.market_intelligence.models import MarketOpportunity, MarketListing, Marketplace, Money, DemandSignal, PriceSignal, TrendSignal
    from datetime import datetime
    from decimal import Decimal
    
    mock_opp = MarketOpportunity(
        snapshot_id="snap-mock",
        listing=MarketListing(
            external_id="MLC123",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Mocked SSD",
            price=Money(amount=Decimal("50000"), currency="CLP"),
            sold_quantity=200,
            available_quantity=10,
            seller_id="SELLER1",
            condition="new",
            shipping_info={"free": True},
            category="CAT1"
        ),
        demand_signal=DemandSignal(score=Decimal("1.0"), label="HIGH"),
        price_signal=PriceSignal(ratio=Decimal("0.9"), position="UNDER_MARKET"),
        trend_signal=TrendSignal(
            keyword="ssd",
            rank=1,
            matched=True,
            trend_score=Decimal("1.0"),
        ),
        opportunity_score=Decimal("111.11"),
        detected_at=datetime.utcnow()
    )
    
    mock_use_case.execute.return_value = [mock_opp]
    
    result = discover_products(query="ssd sata 480gb")
    
    assert isinstance(result, str)
    assert "PRODUCT HUNTER RESULTS: 'ssd sata 480gb'" in result
    assert "Mocked SSD" in result
    assert "MLC123" in result
    assert "Score: 111.11" in result
    assert "Snapshot ID: snap-mock" in result
