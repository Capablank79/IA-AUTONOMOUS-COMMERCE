from src.domain.market_intelligence.models import (
    CatalogProduct,
    Marketplace,
    ProductPicker,
    ProductVariant,
    MarketListing,
    Money
)
from decimal import Decimal


def test_catalog_product_valid():
    product = CatalogProduct(
        product_id="MLC123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Aspiradora portátil",
        domain_id="MLC-VACUUM_AND_STEAM_CLEANERS",
        brand="Arcashopping",
        model="ABC-123",
        attributes={"color": "negro"},
        thumbnail="https://example.com/image.jpg",
        status="active",
        parent_id="MLCPARENT",
        children_ids=["MLC123", "MLC456"],
        pickers=[
            ProductPicker(
                picker_id="COLOR",
                picker_name="Color",
                variants=[
                    ProductVariant(product_id="MLC123", picker_label="Negro"),
                    ProductVariant(product_id="MLC456", picker_label="Azul"),
                ]
            )
        ],
        buy_box_winner=MarketListing(
            external_id="MLC_ITEM_123",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Aspiradora portátil - Oferta",
            price=Money(amount=Decimal("15000"), currency="CLP"),
            sold_quantity=100,
            available_quantity=50,
            seller_id="SELLER_123",
            condition="new",
            shipping_info={},
            category="MLC74192"
        )
    )

    assert product.product_id == "MLC123"
    assert product.marketplace == Marketplace.MERCADO_LIBRE
    assert product.title == "Aspiradora portátil"
    assert product.domain_id == "MLC-VACUUM_AND_STEAM_CLEANERS"
    assert product.brand == "Arcashopping"
    assert product.model == "ABC-123"
    assert product.attributes["color"] == "negro"
    assert product.status == "active"
    assert product.parent_id == "MLCPARENT"
    assert len(product.children_ids) == 2
    assert len(product.pickers) == 1
    assert product.buy_box_winner.external_id == "MLC_ITEM_123"


def test_catalog_product_requires_product_id():
    try:
        CatalogProduct(
            product_id="",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Aspiradora portátil",
            domain_id="MLC-VACUUM_AND_STEAM_CLEANERS",
            brand=None,
            model=None,
            attributes={},
            thumbnail=None,
            status="active",
        )
        assert False
    except ValueError:
        assert True
