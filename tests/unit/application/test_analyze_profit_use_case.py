import pytest
from decimal import Decimal

from src.domain.profit.models import FinancialData, DecisionRules, Money, ProfitAnalysis, Decision
from src.domain.profit.ports import ProfitDataRepository
from src.application.use_cases.analyze_profit import AnalyzeProfitUseCase

class FakeProfitDataRepository(ProfitDataRepository):
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.financial_data_called_with = None
        self.decision_rules_called_with = None

    def get_financial_data(self, experiment_id: str) -> FinancialData:
        if self.should_fail:
            raise ValueError("Repository error fetching financial data")
        self.financial_data_called_with = experiment_id
        
        # We simulate some currency mismatch to test Domain errors if we want,
        # but let's provide correct data by default
        return FinancialData(
            price=Money(amount=Decimal('100'), currency="USD"),
            supplier_price=Money(amount=Decimal('40'), currency="USD"),
            commission_pct=Decimal('15'),
            shipping=Money(amount=Decimal('10'), currency="USD"),
            other_costs=Money(amount=Decimal('5'), currency="USD"),
            visible_sales=150
        )

    def get_decision_rules(self, experiment_id: str) -> DecisionRules:
        if self.should_fail:
            raise ValueError("Repository error fetching decision rules")
        self.decision_rules_called_with = experiment_id
        
        return DecisionRules(
            minimum_margin_pct=Decimal('15'),
            excellent_margin_pct=Decimal('30'),
            minimum_sales=100
        )


def test_analyze_profit_use_case_success():
    repository = FakeProfitDataRepository()
    use_case = AnalyzeProfitUseCase(repository)
    
    experiment_id = "EXP-TEST"
    result = use_case.execute(experiment_id)
    
    assert repository.financial_data_called_with == experiment_id
    assert repository.decision_rules_called_with == experiment_id
    
    assert isinstance(result, ProfitAnalysis)
    assert result.net_profit.amount == Decimal('30') # 100 - 15 - 40 - 10 - 5 = 30
    assert result.net_margin_pct == Decimal('30')
    assert result.decision == Decision.STRONG_BUY


def test_analyze_profit_use_case_repository_error_propagates():
    repository = FakeProfitDataRepository(should_fail=True)
    use_case = AnalyzeProfitUseCase(repository)
    
    with pytest.raises(ValueError, match="Repository error fetching financial data"):
        use_case.execute("EXP-TEST")


def test_analyze_profit_use_case_domain_error_propagates():
    class DomainErrorFakeRepository(FakeProfitDataRepository):
        def get_financial_data(self, experiment_id: str) -> FinancialData:
            # Create a currency mismatch to trigger a domain error in ProfitEngine
            return FinancialData(
                price=Money(amount=Decimal('100'), currency="USD"),
                supplier_price=Money(amount=Decimal('40'), currency="EUR"), # Mismatch
                commission_pct=Decimal('15'),
                shipping=Money(amount=Decimal('10'), currency="USD"),
                other_costs=Money(amount=Decimal('5'), currency="USD"),
                visible_sales=150
            )
            
    repository = DomainErrorFakeRepository()
    use_case = AnalyzeProfitUseCase(repository)
    
    with pytest.raises(ValueError, match="All money values must have the same currency"):
        use_case.execute("EXP-TEST")
