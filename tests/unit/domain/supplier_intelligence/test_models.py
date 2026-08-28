import pytest
from decimal import Decimal

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import SupplierData, SupplierEvidence

def test_supplier_data_creation():
    data = SupplierData(
        supplier_id="SUP-001",
        name="Test Supplier",
        country="Chile",
        status="RESEARCH"
    )
    assert data.supplier_id == "SUP-001"
    assert data.name == "Test Supplier"
    assert data.country == "Chile"
    assert data.status == "RESEARCH"

def test_supplier_evidence_valid():
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-123",
        wholesale_price=Decimal("1000.0"),
        currency="CLP",
        minimum_order_quantity=5,
        stock_available=True,
        shipping_cost=Decimal("500.0"),
        lead_time_days=2,
        confidence=Confidence.HIGH
    )
    assert evidence.supplier_id == "SUP-001"
    assert evidence.wholesale_price == Decimal("1000.0")
    assert evidence.shipping_cost == Decimal("500.0")

def test_supplier_evidence_none_values():
    # Prueba la regla crítica: None != 0 para shipping_cost y lead_time_days
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-123",
        wholesale_price=Decimal("1000.0"),
        currency="CLP",
        minimum_order_quantity=1,
        stock_available=False,
        shipping_cost=None,
        lead_time_days=None
    )
    assert evidence.shipping_cost is None
    assert evidence.lead_time_days is None

def test_supplier_evidence_zero_values():
    # Prueba la regla crítica: 0 es un valor válido y distinto de None
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-123",
        wholesale_price=Decimal("1000.0"),
        currency="CLP",
        minimum_order_quantity=1,
        stock_available=False,
        shipping_cost=Decimal("0.0"),
        lead_time_days=0
    )
    assert evidence.shipping_cost == Decimal("0.0")
    assert evidence.lead_time_days == 0

def test_supplier_evidence_invalid_price():
    with pytest.raises(ValueError, match="wholesale_price must be greater than zero"):
        SupplierEvidence(
            supplier_id="SUP-001",
            sku="SKU-123",
            wholesale_price=Decimal("0.0"),
            currency="CLP",
            minimum_order_quantity=1,
            stock_available=True,
            shipping_cost=None,
            lead_time_days=None
        )

def test_supplier_evidence_invalid_moq():
    with pytest.raises(ValueError, match="minimum_order_quantity must be at least 1"):
        SupplierEvidence(
            supplier_id="SUP-001",
            sku="SKU-123",
            wholesale_price=Decimal("100.0"),
            currency="CLP",
            minimum_order_quantity=0,
            stock_available=True,
            shipping_cost=None,
            lead_time_days=None
        )

def test_supplier_evidence_invalid_shipping():
    with pytest.raises(ValueError, match="shipping_cost cannot be negative"):
        SupplierEvidence(
            supplier_id="SUP-001",
            sku="SKU-123",
            wholesale_price=Decimal("100.0"),
            currency="CLP",
            minimum_order_quantity=1,
            stock_available=True,
            shipping_cost=Decimal("-10.0"),
            lead_time_days=None
        )

def test_supplier_evidence_invalid_lead_time():
    with pytest.raises(ValueError, match="lead_time_days cannot be negative"):
        SupplierEvidence(
            supplier_id="SUP-001",
            sku="SKU-123",
            wholesale_price=Decimal("100.0"),
            currency="CLP",
            minimum_order_quantity=1,
            stock_available=True,
            shipping_cost=None,
            lead_time_days=-1
        )
