import pytest
from decimal import Decimal
from src.domain.supplier_intelligence.models import SupplierEvidence, ConfirmedQuote
from src.domain.market_intelligence.models import Confidence
from src.domain.profit.models import FinancialData, Money
from src.application.mappers.supplier_financial_mapper import SupplierFinancialMapper

def test_confirmed_quote_creation():
    quote = ConfirmedQuote(
        quote_id="Q-123",
        wholesale_price=Decimal("4000"),
        shipping_cost=Decimal("500"),
        lead_time_days=2,
        currency="CLP"
    )
    assert quote.quote_id == "Q-123"
    assert quote.wholesale_price == Decimal("4000")
    assert quote.shipping_cost == Decimal("500")
    assert quote.lead_time_days == 2

def test_confirmed_quote_invalid_values():
    with pytest.raises(ValueError, match="wholesale_price must be greater than zero"):
        ConfirmedQuote("Q-1", Decimal("0"), Decimal("100"), 1)
    
    with pytest.raises(ValueError, match="shipping_cost cannot be negative"):
        ConfirmedQuote("Q-1", Decimal("100"), Decimal("-1"), 1)
        
    with pytest.raises(ValueError, match="lead_time_days cannot be negative"):
        ConfirmedQuote("Q-1", Decimal("100"), Decimal("100"), -1)

def test_supplier_evidence_with_quote():
    quote = ConfirmedQuote("Q-123", Decimal("4000"), Decimal("500"), 2)
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-1",
        wholesale_price=Decimal("4384"),
        currency="CLP",
        minimum_order_quantity=1,
        stock_available=True,
        shipping_cost=None,
        lead_time_days=None,
        confidence=Confidence.HIGH,
        quote=quote
    )
    assert evidence.quote == quote
    assert evidence.shipping_cost is None # El campo base sigue siendo None

def test_mapper_completes_missing_shipping_from_quote():
    base_financial = FinancialData(
        price=Money(Decimal("10000"), "CLP"),
        supplier_price=Money(Decimal("0"), "CLP"),
        commission_pct=Decimal("10"),
        shipping=Money(Decimal("0"), "CLP"),
        other_costs=Money(Decimal("0"), "CLP"),
        visible_sales=100
    )
    
    quote = ConfirmedQuote("Q-123", Decimal("4000"), Decimal("500"), 2)
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-1",
        wholesale_price=Decimal("4000"),
        currency="CLP",
        minimum_order_quantity=1,
        stock_available=True,
        shipping_cost=None, # Missing
        lead_time_days=None,
        confidence=Confidence.HIGH,
        quote=quote
    )
    
    result = SupplierFinancialMapper.map_evidence_to_financial_data(base_financial, evidence)
    
    assert result.shipping.amount == Decimal("500")
    assert result.supplier_price.amount == Decimal("4000")

def test_mapper_still_fails_if_no_shipping_and_no_quote():
    base_financial = FinancialData(
        price=Money(Decimal("10000"), "CLP"),
        supplier_price=Money(Decimal("0"), "CLP"),
        commission_pct=Decimal("10"),
        shipping=Money(Decimal("0"), "CLP"),
        other_costs=Money(Decimal("0"), "CLP"),
        visible_sales=100
    )
    
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-1",
        wholesale_price=Decimal("4000"),
        currency="CLP",
        minimum_order_quantity=1,
        stock_available=True,
        shipping_cost=None,
        lead_time_days=None,
        confidence=Confidence.HIGH,
        quote=None
    )
    
    with pytest.raises(ValueError, match="shipping_cost es desconocido"):
        SupplierFinancialMapper.map_evidence_to_financial_data(base_financial, evidence)

def test_mapper_fails_on_quote_currency_mismatch():
    base_financial = FinancialData(
        price=Money(Decimal("10000"), "CLP"),
        supplier_price=Money(Decimal("0"), "CLP"),
        commission_pct=Decimal("10"),
        shipping=Money(Decimal("0"), "CLP"),
        other_costs=Money(Decimal("0"), "CLP"),
        visible_sales=100
    )
    
    quote = ConfirmedQuote("Q-123", Decimal("4000"), Decimal("500"), 2, currency="USD")
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-1",
        wholesale_price=Decimal("4000"),
        currency="CLP",
        minimum_order_quantity=1,
        stock_available=True,
        shipping_cost=None,
        lead_time_days=None,
        confidence=Confidence.HIGH,
        quote=quote
    )
    
    with pytest.raises(ValueError, match="Moneda de cotización USD no coincide con evidencia CLP"):
        SupplierFinancialMapper.map_evidence_to_financial_data(base_financial, evidence)
