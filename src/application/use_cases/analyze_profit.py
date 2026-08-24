from src.domain.profit.ports import ProfitDataRepository
from src.domain.profit.engine import ProfitEngine
from src.domain.profit.models import ProfitAnalysis

class AnalyzeProfitUseCase:
    """
    Use case to orchestrate the profit analysis of an experiment.
    It fetches data from a repository and uses the ProfitEngine to perform the calculation.
    """
    def __init__(self, repository: ProfitDataRepository):
        self._repository = repository
        self._engine = ProfitEngine()

    def execute(self, experiment_id: str) -> ProfitAnalysis:
        financial_data = self._repository.get_financial_data(experiment_id)
        decision_rules = self._repository.get_decision_rules(experiment_id)
        
        return self._engine.calculate(data=financial_data, rules=decision_rules)
