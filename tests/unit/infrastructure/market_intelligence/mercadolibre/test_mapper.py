import pytest
from decimal import Decimal
from src.infrastructure.market_intelligence.mercadolibre.mapper import MercadoLibreMapper
from src.domain.market_intelligence.models import Marketplace

def test_mapper_to_domain():
    ml_item = {
        "id": "MLC12345",
        "title": "Producto de prueba",
        "price": 1500.50,
        "currency_id": "CLP",
        "sold_quantity": 25,
        "available_quantity": 100,
        "seller": {"id": 987654},
        "condition": "new",
        "shipping": {"free_shipping": True},
        "category_id": "MLC1055"
    }
    
    listing = MercadoLibreMapper.to_domain(ml_item)
    
    assert listing.external_id == "MLC12345"
    assert listing.marketplace == Marketplace.MERCADO_LIBRE
    assert listing.title == "Producto de prueba"
    assert listing.price.amount == Decimal("1500.50")
    assert listing.price.currency == "CLP"
    assert listing.sold_quantity == 25
    assert listing.available_quantity == 100
    assert listing.seller_id == "987654"
    assert listing.condition == "new"
    assert listing.shipping_info["free_shipping"] is True
    assert listing.category == "MLC1055"

def test_mapper_missing_fields():
    ml_item = {
        "id": "MLC1",
        "price": 10,
        "seller": {"id": 1}
    }
    listing = MercadoLibreMapper.to_domain(ml_item)
    assert listing.title == "No title"
    assert listing.condition == "new"
    assert listing.sold_quantity == 0
