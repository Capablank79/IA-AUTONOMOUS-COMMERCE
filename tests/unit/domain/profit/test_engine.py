from decimal import Decimal
import pytest

from src.domain.profit.models import (
    FinancialData,
    DecisionRules,
    Money,
    Decision
)
from src.domain.profit.engine import ProfitEngine

def test_profit_engine_strong_buy():
    """Reproduce el caso del snapshot EXP-001."""
    engine = ProfitEngine()
    
    data = FinancialData(
        price=Money(amount=Decimal('99990'), currency='CLP'),
        supplier_price=Money(amount=Decimal('4384'), currency='CLP'),
        commission_pct=Decimal('13.0'),
        shipping=Money(amount=Decimal('0'), currency='CLP'),
        other_costs=Money(amount=Decimal('0'), currency='CLP'),
        visible_sales=10000
    )
    
    rules = DecisionRules(
        minimum_margin_pct=Decimal('25.00'),
        excellent_margin_pct=Decimal('40.00'),
        minimum_sales=100
    )
    
    result = engine.calculate(data, rules)
    
    # 99990 * 0.13 = 12998.7
    assert result.commission.amount == Decimal('12998.7')
    # 99990 - 12998.7 - 4384 = 82607.3
    assert result.net_profit.amount == Decimal('82607.3')
    
    # (82607.3 / 99990) * 100 = 82.61556155615561556155615562
    # Comparamos con redondeo a 2 decimales para la prueba, aunque el modelo tiene más precisión
    assert round(result.net_margin_pct, 2) == Decimal('82.62')
    
    assert result.decision == Decision.STRONG_BUY
    assert result.market_demand_ok is True


def test_profit_engine_buy_decision():
    """Margen entre el mínimo y el excelente."""
    engine = ProfitEngine()
    
    # Reducimos precio para que el margen quede entre 25% y 40%
    # Si margin es 30%: net_profit = 3000
    # price = 10000, commission = 1000 (10%), cost = 6000
    data = FinancialData(
        price=Money(amount=Decimal('10000'), currency='CLP'),
        supplier_price=Money(amount=Decimal('6000'), currency='CLP'),
        commission_pct=Decimal('10.0'),
        shipping=Money(amount=Decimal('0'), currency='CLP'),
        other_costs=Money(amount=Decimal('0'), currency='CLP'),
        visible_sales=100
    )
    
    rules = DecisionRules(
        minimum_margin_pct=Decimal('25.0'),
        excellent_margin_pct=Decimal('40.0'),
        minimum_sales=100
    )
    
    result = engine.calculate(data, rules)
    
    assert result.net_profit.amount == Decimal('3000')
    assert result.net_margin_pct == Decimal('30.0')
    assert result.decision == Decision.BUY


def test_profit_engine_reject_due_to_margin():
    """Margen por debajo del mínimo."""
    engine = ProfitEngine()
    
    data = FinancialData(
        price=Money(amount=Decimal('10000'), currency='CLP'),
        supplier_price=Money(amount=Decimal('8000'), currency='CLP'), # Costo muy alto
        commission_pct=Decimal('10.0'),
        shipping=Money(amount=Decimal('0'), currency='CLP'),
        other_costs=Money(amount=Decimal('0'), currency='CLP'),
        visible_sales=500
    )
    
    rules = DecisionRules(
        minimum_margin_pct=Decimal('25.0'),
        excellent_margin_pct=Decimal('40.0'),
        minimum_sales=100
    )
    
    result = engine.calculate(data, rules)
    
    assert result.net_profit.amount == Decimal('1000')
    assert result.net_margin_pct == Decimal('10.0')
    assert result.decision == Decision.REJECT


def test_profit_engine_reject_due_to_sales():
    """Ventas insuficientes a pesar de buen margen."""
    engine = ProfitEngine()
    
    data = FinancialData(
        price=Money(amount=Decimal('10000'), currency='CLP'),
        supplier_price=Money(amount=Decimal('2000'), currency='CLP'),
        commission_pct=Decimal('10.0'),
        shipping=Money(amount=Decimal('0'), currency='CLP'),
        other_costs=Money(amount=Decimal('0'), currency='CLP'),
        visible_sales=50 # Debajo del mínimo de 100
    )
    
    rules = DecisionRules(
        minimum_margin_pct=Decimal('25.0'),
        excellent_margin_pct=Decimal('40.0'),
        minimum_sales=100
    )
    
    result = engine.calculate(data, rules)
    
    assert result.net_margin_pct == Decimal('70.0')
    assert result.market_demand_ok is False
    assert result.decision == Decision.REJECT


def test_profit_engine_negative_margin():
    """Costo mayor al precio produce pérdida."""
    engine = ProfitEngine()
    
    data = FinancialData(
        price=Money(amount=Decimal('1000'), currency='CLP'),
        supplier_price=Money(amount=Decimal('2000'), currency='CLP'),
        commission_pct=Decimal('10.0'),
        shipping=Money(amount=Decimal('0'), currency='CLP'),
        other_costs=Money(amount=Decimal('0'), currency='CLP'),
        visible_sales=500
    )
    
    rules = DecisionRules(
        minimum_margin_pct=Decimal('25.0'),
        excellent_margin_pct=Decimal('40.0'),
        minimum_sales=100
    )
    
    result = engine.calculate(data, rules)
    
    assert result.net_profit.amount == Decimal('-1100')
    assert result.net_margin_pct == Decimal('-110.0')
    assert result.decision == Decision.REJECT


def test_profit_engine_currency_mismatch():
    """Debe fallar si las monedas no coinciden."""
    engine = ProfitEngine()
    
    data = FinancialData(
        price=Money(amount=Decimal('10000'), currency='CLP'),
        supplier_price=Money(amount=Decimal('10'), currency='USD'), # Distinta moneda
        commission_pct=Decimal('10.0'),
        shipping=Money(amount=Decimal('0'), currency='CLP'),
        other_costs=Money(amount=Decimal('0'), currency='CLP'),
        visible_sales=100
    )
    
    rules = DecisionRules(
        minimum_margin_pct=Decimal('25.0'),
        excellent_margin_pct=Decimal('40.0'),
        minimum_sales=100
    )
    
    with pytest.raises(ValueError, match="same currency"):
        engine.calculate(data, rules)

def test_profit_engine_zero_price():
    """Manejo de precio cero."""
    engine = ProfitEngine()
    
    data = FinancialData(
        price=Money(amount=Decimal('0'), currency='CLP'),
        supplier_price=Money(amount=Decimal('10'), currency='CLP'),
        commission_pct=Decimal('10.0'),
        shipping=Money(amount=Decimal('0'), currency='CLP'),
        other_costs=Money(amount=Decimal('0'), currency='CLP'),
        visible_sales=100
    )
    
    rules = DecisionRules(
        minimum_margin_pct=Decimal('25.0'),
        excellent_margin_pct=Decimal('40.0'),
        minimum_sales=100
    )
    
    result = engine.calculate(data, rules)
    
    assert result.net_profit.amount == Decimal('-10')
    assert result.net_margin_pct == Decimal('0')
    assert result.decision == Decision.REJECT

