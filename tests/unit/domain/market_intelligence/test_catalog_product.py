from src.domain.market_intelligence.models import CatalogProduct, Marketplace


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
    )

    assert product.product_id == "MLC123"
    assert product.marketplace == Marketplace.MERCADO_LIBRE
    assert product.title == "Aspiradora portátil"
    assert product.domain_id == "MLC-VACUUM_AND_STEAM_CLEANERS"
    assert product.brand == "Arcashopping"
    assert product.model == "ABC-123"
    assert product.attributes["color"] == "negro"
    assert product.status == "active"


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
