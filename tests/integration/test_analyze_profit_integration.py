import pytest
from pathlib import Path
from decimal import Decimal

from src.application.use_cases.analyze_profit import AnalyzeProfitUseCase
from src.infrastructure.persistence.data.json.profit_repository import JsonProfitDataRepository
from src.domain.profit.models import ProfitAnalysis, Decision

# Relative path to the real data directory from project root
REAL_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "experiments"

def test_analyze_profit_use_case_integration():
    # Arrange
    repository = JsonProfitDataRepository(REAL_DATA_DIR)
    use_case = AnalyzeProfitUseCase(repository)
    
    experiment_id = "EXP-001"
    
    # Act
    result = use_case.execute(experiment_id)
    
    # Assert
    assert isinstance(result, ProfitAnalysis)
    
    # Assert specific values based on the EXP-001 JSON to ensure full integration correctness
    # The JSON values for EXP-001:
    # price = 99990
    # supplier = 4384
    # commission = 13% = 12998.7
    # shipping = 0
    # other = 0
    # net_profit = 99990 - 12998.7 - 4384 = 82607.3
    assert result.net_profit.amount == Decimal("82607.3")
    assert result.net_profit.currency == "CLP"
    
    # net_margin = 82607.3 / 99990 = 82.615...
    # Just checking it's a Decimal and it's positive and > excellent_margin (40%)
    assert result.net_margin_pct > Decimal("40.0")
    
    # Since margin is excellent and demand is ok, it should be STRONG_BUY
    assert result.decision == Decision.STRONG_BUY
