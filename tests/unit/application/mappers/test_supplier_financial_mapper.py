import pytest
from decimal import Decimal

from src.domain.profit.models import FinancialData, Money
from src.domain.supplier_intelligence.models import SupplierEvidence
from src.domain.market_intelligence.models import Confidence
from src.application.mappers.supplier_financial_mapper import SupplierFinancialMapper


@pytest.fixture
def base_financial_data():
    return FinancialData(
        price=Money(amount=Decimal("1000"), currency="CLP"),
        supplier_price=Money(amount=Decimal("10"), currency="CLP"),  # Should be overridden
        commission_pct=Decimal("10"),
        shipping=Money(amount=Decimal("0"), currency="CLP"),  # Should be overridden
        other_costs=Money(amount=Decimal("5"), currency="CLP"),
        visible_sales=100
    )


def test_mapper_success(base_financial_data):
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-1",
        wholesale_price=Decimal("400"),
        currency="CLP",
        minimum_order_quantity=1,
        stock_available=True,
        shipping_cost=Decimal("50"),
        lead_time_days=3,
        confidence=Confidence.HIGH
    )

    result = SupplierFinancialMapper.map_evidence_to_financial_data(base_financial_data, evidence)

    assert result.price == base_financial_data.price
    assert result.commission_pct == base_financial_data.commission_pct
    assert result.other_costs == base_financial_data.other_costs
    assert result.visible_sales == base_financial_data.visible_sales

    # Overridden fields
    assert result.supplier_price.amount == Decimal("400")
    assert result.supplier_price.currency == "CLP"
    assert result.shipping.amount == Decimal("50")
    assert result.shipping.currency == "CLP"


def test_mapper_fails_when_shipping_cost_is_none(base_financial_data):
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-1",
        wholesale_price=Decimal("400"),
        currency="CLP",
        minimum_order_quantity=1,
        stock_available=True,
        shipping_cost=None,  # Rule: None != 0
        lead_time_days=3,
        confidence=Confidence.HIGH
    )

    with pytest.raises(ValueError, match="No se puede calcular el Profit: el shipping_cost es desconocido"):
        SupplierFinancialMapper.map_evidence_to_financial_data(base_financial_data, evidence)


def test_mapper_fails_when_wholesale_price_invalid(base_financial_data):
    # Ya está validado en el dataclass, pero podemos forzarlo con object.__setattr__ si quisieramos
    # o si se pasara un valor menor a cero. Aquí el ValueError lo lanzará el propio dataclass
    with pytest.raises(ValueError, match="wholesale_price must be greater than zero"):
        SupplierEvidence(
            supplier_id="SUP-001",
            sku="SKU-1",
            wholesale_price=Decimal("-10"),
            currency="CLP",
            minimum_order_quantity=1,
            stock_available=True,
            shipping_cost=Decimal("50"),
            lead_time_days=3,
            confidence=Confidence.HIGH
        )


def test_mapper_fails_on_currency_mismatch(base_financial_data):
    evidence = SupplierEvidence(
        supplier_id="SUP-001",
        sku="SKU-1",
        wholesale_price=Decimal("400"),
        currency="USD",  # Mismatch with CLP
        minimum_order_quantity=1,
        stock_available=True,
        shipping_cost=Decimal("50"),
        lead_time_days=3,
        confidence=Confidence.HIGH
    )

    with pytest.raises(ValueError, match="Las monedas no coinciden"):
        SupplierFinancialMapper.map_evidence_to_financial_data(base_financial_data, evidence)
