from decimal import Decimal

from src.domain.profit.models import DecisionRules, FinancialData, Money
from src.domain.profit.ports import ProfitDataRepository


class DummyProfitDataRepository:
    """
    A dummy implementation to verify the ProfitDataRepository Protocol.
    """
    def get_financial_data(self, experiment_id: str) -> FinancialData:
        return FinancialData(
            price=Money(Decimal('100'), 'CLP'),
            supplier_price=Money(Decimal('50'), 'CLP'),
            commission_pct=Decimal('10'),
            shipping=Money(Decimal('5'), 'CLP'),
            other_costs=Money(Decimal('0'), 'CLP'),
            visible_sales=10
        )

    def get_decision_rules(self, experiment_id: str) -> DecisionRules:
        return DecisionRules(
            minimum_margin_pct=Decimal('10'),
            excellent_margin_pct=Decimal('20'),
            minimum_sales=5
        )


def test_profit_data_repository_protocol():
    """
    Test that ensures the Protocol exists and can be implemented
    correctly by a class providing the required methods.
    """
    repo: ProfitDataRepository = DummyProfitDataRepository()
    
    financial_data = repo.get_financial_data("exp-123")
    rules = repo.get_decision_rules("exp-123")
    
    assert isinstance(financial_data, FinancialData)
    assert isinstance(rules, DecisionRules)
    assert financial_data.price.amount == Decimal('100')
    assert rules.minimum_sales == 5
