import os
import json
import pytest
from decimal import Decimal

from src.infrastructure.suppliers.json_supplier_data_source import JsonSupplierDataSource
from src.domain.supplier_intelligence.models import SupplierData, SupplierEvidence
from src.domain.market_intelligence.models import Confidence

@pytest.fixture
def supplier_json_path(tmp_path):
    # Creamos un JSON temporal de prueba con el formato esperado
    file_path = tmp_path / "supplier_001.json"
    data = {
        "supplier_id": "SUP-001",
        "company": {
            "name": "Proveedor Test Chile",
            "country": "Chile",
            "website": "",
            "business_type": "wholesaler"
        },
        "commercial": {
            "payment_terms": "Por definir",
            "minimum_order_quantity": 1
        },
        "product": {
            "category": "storage",
            "brand": "Kingston",
            "model": "A400",
            "sku": "SA400S37/480G"
        },
        "pricing": {
            "wholesale_price_clp": 4384,
            "currency": "CLP"
        },
        "stock": {
            "available": False,
            "quantity": 0
        },
        "logistics": {
            "ships_from": "Chile",
            "shipping_cost_clp": None,
            "delivery_time_days": None
        },
        "supplier_status": "RESEARCH"
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return str(tmp_path)

@pytest.fixture
def supplier_with_quote_json_path(tmp_path):
    file_path = tmp_path / "supplier_002.json"
    data = {
        "supplier_id": "SUP-002",
        "company": {"name": "Quote Supplier", "country": "Chile", "status": "RESEARCH"},
        "product": {"sku": "SKU-QUOTE"},
        "pricing": {"wholesale_price_clp": 5000, "currency": "CLP"},
        "commercial": {"minimum_order_quantity": 1},
        "stock": {"available": True},
        "logistics": {"shipping_cost_clp": None, "delivery_time_days": None},
        "confirmed_quote": {
            "quote_id": "Q-123",
            "wholesale_price_clp": 4800,
            "shipping_cost_clp": 200,
            "delivery_time_days": 1,
            "currency": "CLP"
        }
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return str(tmp_path)

def test_get_supplier_data(supplier_json_path):
    data_source = JsonSupplierDataSource(supplier_json_path)
    data = data_source.get_supplier_data("SUP-001")

    assert data is not None
    assert isinstance(data, SupplierData)
    assert data.supplier_id == "SUP-001"
    assert data.name == "Proveedor Test Chile"
    assert data.country == "Chile"
    assert data.status == "RESEARCH"

def test_get_supplier_data_not_found(supplier_json_path):
    data_source = JsonSupplierDataSource(supplier_json_path)
    data = data_source.get_supplier_data("SUP-999")

    assert data is None

def test_get_supplier_evidence(supplier_json_path):
    data_source = JsonSupplierDataSource(supplier_json_path)
    evidence = data_source.get_supplier_evidence("SUP-001", "SA400S37/480G")

    assert evidence is not None
    assert isinstance(evidence, SupplierEvidence)
    assert evidence.supplier_id == "SUP-001"
    assert evidence.sku == "SA400S37/480G"
    assert evidence.wholesale_price == Decimal("4384")
    assert evidence.currency == "CLP"
    assert evidence.minimum_order_quantity == 1
    assert evidence.stock_available is False
    assert evidence.shipping_cost is None
    assert evidence.lead_time_days is None
    assert evidence.confidence == Confidence.HIGH

def test_get_supplier_evidence_wrong_sku(supplier_json_path):
    data_source = JsonSupplierDataSource(supplier_json_path)
    evidence = data_source.get_supplier_evidence("SUP-001", "WRONG-SKU")

    assert evidence is None

def test_get_supplier_evidence_with_quote(supplier_with_quote_json_path):
    data_source = JsonSupplierDataSource(supplier_with_quote_json_path)
    evidence = data_source.get_supplier_evidence("SUP-002", "SKU-QUOTE")

    assert evidence is not None
    assert evidence.quote is not None
    assert evidence.quote.quote_id == "Q-123"
    assert evidence.quote.wholesale_price == Decimal("4800")
    assert evidence.quote.shipping_cost == Decimal("200")
    assert evidence.quote.lead_time_days == 1
    assert evidence.quote.currency == "CLP"

def test_file_not_found():
    data_source = JsonSupplierDataSource("non_existent_file.json")
    data = data_source.get_supplier_data("SUP-001")
    assert data is None

    evidence = data_source.get_supplier_evidence("SUP-001", "SKU-123")
    assert evidence is None
