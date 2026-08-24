from typing import Protocol

from .models import DecisionRules, FinancialData


class ProfitDataRepository(Protocol):
    """
    Port that defines the contract to obtain the necessary data
    for the ProfitEngine to perform its analysis.
    
    This repository is read-only according to current business needs.
    """

    def get_financial_data(self, experiment_id: str) -> FinancialData:
        """
        Retrieves the financial data associated with a specific experiment.
        """
        ...

    def get_decision_rules(self, experiment_id: str) -> DecisionRules:
        """
        Retrieves the decision rules applied to a specific experiment.
        """
        ...
