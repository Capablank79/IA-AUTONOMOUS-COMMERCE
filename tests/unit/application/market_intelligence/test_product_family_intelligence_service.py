import pytest
from unittest.mock import MagicMock
from src.domain.market_intelligence.models import CatalogProduct, Marketplace, ProductPicker, ProductVariant
from src.application.market_intelligence.product_family_intelligence_service import ProductFamilyIntelligenceService

@pytest.fixture
def mock_catalog_source():
    return MagicMock()

@pytest.fixture
def service(mock_catalog_source):
    return ProductFamilyIntelligenceService(mock_catalog_source)

def test_get_family_intelligence_simple(service, mock_catalog_source):
    # Mock a simple product without parent
    main_product = CatalogProduct(
        product_id="MLC123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Product 123",
        domain_id="DOMAIN",
        brand="Brand",
        model="Model",
        attributes={},
        thumbnail=None,
        status="active"
    )
    mock_catalog_source.get_product.return_value = main_product
    
    intel = service.get_family_intelligence("MLC123")
    
    assert intel.main_product.product_id == "MLC123"
    assert intel.parent_product is None
    assert intel.siblings == []
    assert intel.related_catalog_ids == ["MLC123"]

def test_get_family_intelligence_with_parent_and_siblings(service, mock_catalog_source):
    # Mock main product with parent
    main_product = CatalogProduct(
        product_id="MLC123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Product 123",
        domain_id="DOMAIN",
        brand="Brand",
        model="Model",
        attributes={},
        thumbnail=None,
        status="active",
        parent_id="MLCPARENT"
    )
    
    parent_product = CatalogProduct(
        product_id="MLCPARENT",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Parent Product",
        domain_id="DOMAIN",
        brand="Brand",
        model="Model",
        attributes={},
        thumbnail=None,
        status="inactive",
        children_ids=["MLC123", "MLC456"]
    )
    
    sibling_product = CatalogProduct(
        product_id="MLC456",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Product 456",
        domain_id="DOMAIN",
        brand="Brand",
        model="Model",
        attributes={},
        thumbnail=None,
        status="active"
    )
    
    def side_effect(product_id):
        if product_id == "MLC123": return main_product
        if product_id == "MLCPARENT": return parent_product
        if product_id == "MLC456": return sibling_product
        return None
        
    mock_catalog_source.get_product.side_effect = side_effect
    
    intel = service.get_family_intelligence("MLC123")
    
    assert intel.main_product.product_id == "MLC123"
    assert intel.parent_product.product_id == "MLCPARENT"
    assert len(intel.siblings) == 1
    assert intel.siblings[0].product_id == "MLC456"
    assert "MLC123" in intel.related_catalog_ids
    assert "MLC456" in intel.related_catalog_ids
    assert "MLCPARENT" in intel.related_catalog_ids

def test_get_family_intelligence_with_variants(service, mock_catalog_source):
    # Mock product with variants in pickers
    main_product = CatalogProduct(
        product_id="MLC123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Product 123",
        domain_id="DOMAIN",
        brand="Brand",
        model="Model",
        attributes={},
        thumbnail=None,
        status="active",
        pickers=[
            ProductPicker(
                picker_id="COLOR",
                picker_name="Color",
                variants=[
                    ProductVariant(product_id="MLC123", picker_label="Negro"),
                    ProductVariant(product_id="MLC789", picker_label="Azul")
                ]
            )
        ]
    )
    mock_catalog_source.get_product.return_value = main_product
    
    intel = service.get_family_intelligence("MLC123")
    
    assert intel.main_product.product_id == "MLC123"
    assert len(intel.variants) == 2
    assert "MLC123" in intel.related_catalog_ids
    assert "MLC789" in intel.related_catalog_ids
