import pytest
from unittest.mock import MagicMock
from decimal import Decimal
from src.domain.market_intelligence.models import Marketplace
from src.infrastructure.mercadolibre.product_catalog_data_source import MercadoLibreProductCatalogDataSource

@pytest.fixture
def mock_api_client():
    return MagicMock()

@pytest.fixture
def data_source(mock_api_client):
    return MercadoLibreProductCatalogDataSource(mock_api_client)

def test_get_product_success(data_source, mock_api_client):
    # Mock response for /products/MLC123
    mock_response = {
        "id": "MLC123",
        "name": "Test Product",
        "domain_id": "MLC-TEST",
        "status": "active",
        "attributes": [
            {"id": "BRAND", "value_name": "TestBrand"},
            {"id": "MODEL", "value_name": "TestModel"}
        ],
        "pictures": [{"url": "http://example.com/img.jpg"}],
        "parent_id": "MLCPARENT",
        "children_ids": ["MLC123", "MLC456"],
        "pickers": [
            {
                "picker_id": "COLOR",
                "picker_name": "Color",
                "products": [
                    {
                        "product_id": "MLC123",
                        "picker_label": "Red",
                        "changes": [["COLOR", "Red"]]
                    }
                ]
            }
        ],
        "buy_box_winner": {
            "id": "MLC_ITEM_123",
            "title": "Item Title",
            "price": 100.5,
            "currency_id": "CLP",
            "available_quantity": 10,
            "sold_quantity": 5,
            "condition": "new",
            "seller": {"id": 999},
            "shipping": {"logistic_type": "full"}
        }
    }
    mock_api_client.get.return_value = mock_response

    product = data_source.get_product("MLC123")

    assert product.product_id == "MLC123"
    assert product.title == "Test Product"
    assert product.brand == "TestBrand"
    assert product.model == "TestModel"
    assert product.parent_id == "MLCPARENT"
    assert len(product.children_ids) == 2
    assert len(product.pickers) == 1
    assert product.pickers[0].variants[0].picker_label == "Red"
    assert product.buy_box_winner.external_id == "MLC_ITEM_123"
    assert product.buy_box_winner.price.amount == Decimal("100.5")
    assert product.buy_box_winner.seller_id == "999"

def test_search_products_success(data_source, mock_api_client):
    mock_api_client.get.return_value = {
        "results": [
            {
                "id": "MLC123",
                "name": "Test Product",
                "domain_id": "MLC-TEST",
                "status": "active",
                "attributes": [],
                "pictures": []
            }
        ]
    }

    products = data_source.search_products("query", Marketplace.MERCADO_LIBRE)

    assert len(products) == 1
    assert products[0].product_id == "MLC123"
    mock_api_client.get.assert_called_once()
    assert "/products/search" in mock_api_client.get.call_args[0][0]
