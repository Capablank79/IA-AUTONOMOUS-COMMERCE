import json
from decimal import Decimal
from pathlib import Path
import pytest
import tempfile

from src.domain.profit.models import FinancialData, DecisionRules, Money
from src.infrastructure.persistence.data.json.profit_repository import (
    JsonProfitDataRepository,
    DataDirectoryNotFoundError,
    InvalidJsonError,
    ExperimentNotFoundError,
    InvalidDataStructureError
)

# Relative path to the real data directory from project root
REAL_DATA_DIR = Path(__file__).resolve().parents[6] / "data" / "experiments"

@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)

@pytest.fixture
def repo(temp_data_dir):
    return JsonProfitDataRepository(temp_data_dir)

def test_read_existing_experiment_success():
    """1. lectura correcta del experimento existente
       5. experiment_id válido"""
    # Use the real data directory for this test
    repo = JsonProfitDataRepository(REAL_DATA_DIR)
    
    fin_data = repo.get_financial_data("EXP-001")
    rules = repo.get_decision_rules("EXP-001")
    
    assert fin_data is not None
    assert rules is not None

def test_financial_data_conversion():
    """2. conversión correcta a FinancialData
       4. conversión monetaria a Decimal"""
    repo = JsonProfitDataRepository(REAL_DATA_DIR)
    fin_data = repo.get_financial_data("EXP-001")
    
    assert isinstance(fin_data, FinancialData)
    
    assert isinstance(fin_data.price, Money)
    assert isinstance(fin_data.price.amount, Decimal)
    assert fin_data.price.currency == "CLP"
    
    assert isinstance(fin_data.supplier_price, Money)
    assert isinstance(fin_data.supplier_price.amount, Decimal)
    assert fin_data.supplier_price.currency == "CLP"
    
    assert isinstance(fin_data.commission_pct, Decimal)
    assert isinstance(fin_data.shipping, Money)
    assert isinstance(fin_data.shipping.amount, Decimal)
    assert isinstance(fin_data.other_costs, Money)
    assert isinstance(fin_data.other_costs.amount, Decimal)
    
    assert isinstance(fin_data.visible_sales, int)
    
    # Specific values from existing JSON
    assert fin_data.price.amount == Decimal("99990")
    assert fin_data.commission_pct == Decimal("13.0")
    assert fin_data.supplier_price.amount == Decimal("4384")

def test_decision_rules_conversion():
    """3. conversión correcta a DecisionRules"""
    repo = JsonProfitDataRepository(REAL_DATA_DIR)
    rules = repo.get_decision_rules("EXP-001")
    
    assert isinstance(rules, DecisionRules)
    assert isinstance(rules.minimum_margin_pct, Decimal)
    assert isinstance(rules.excellent_margin_pct, Decimal)
    assert isinstance(rules.minimum_sales, int)
    
    assert rules.minimum_margin_pct == Decimal("25.0")
    assert rules.excellent_margin_pct == Decimal("40.0")
    assert rules.minimum_sales == 100

def test_experiment_id_not_found(repo, temp_data_dir):
    """6. experiment_id inexistente"""
    # Create a valid JSON but different ID
    file_path = temp_data_dir / "other.json"
    with open(file_path, "w") as f:
        json.dump({"experiment_id": "EXP-999"}, f)
        
    with pytest.raises(ExperimentNotFoundError):
        repo.get_financial_data("EXP-001")

def test_data_directory_not_found():
    """7. archivo inexistente si es aplicable (Data directory not found)"""
    repo = JsonProfitDataRepository(Path("/path/that/does/not/exist/12345"))
    with pytest.raises(DataDirectoryNotFoundError):
        repo.get_financial_data("EXP-001")

def test_invalid_json(repo, temp_data_dir):
    """8. JSON inválido si es razonablemente testeable"""
    file_path = temp_data_dir / "bad.json"
    with open(file_path, "w") as f:
        f.write("{ invalid json")
        
    with pytest.raises(InvalidJsonError):
        repo.get_financial_data("EXP-001")

def test_invalid_data_structure_missing_key(repo, temp_data_dir):
    """9. estructura inválida si es razonablemente testeable (Missing key)"""
    file_path = temp_data_dir / "missing_key.json"
    with open(file_path, "w") as f:
        json.dump({
            "experiment_id": "EXP-001",
            "market": {} # Missing required keys
        }, f)
        
    with pytest.raises(InvalidDataStructureError):
        repo.get_financial_data("EXP-001")

def test_invalid_data_structure_not_dict(repo, temp_data_dir):
    """9. estructura inválida si es razonablemente testeable (Root not object)"""
    file_path = temp_data_dir / "not_dict.json"
    with open(file_path, "w") as f:
        json.dump(["not", "a", "dict"], f)
        
    with pytest.raises(InvalidDataStructureError):
        repo.get_financial_data("EXP-001")

def test_invalid_data_type(repo, temp_data_dir):
    """9. estructura inválida si es razonablemente testeable (Invalid data type for Decimal)"""
    file_path = temp_data_dir / "invalid_type.json"
    with open(file_path, "w") as f:
        json.dump({
            "experiment_id": "EXP-001",
            "market": {
                "market_price_clp": "not-a-number",
                "marketplace_commission_pct": 13.0,
                "shipping_cost_clp": 0,
                "other_costs_clp": 0,
                "visible_sales": 100
            },
            "suppliers": {
                "test_supplier_price_clp": 4384
            }
        }, f)
        
    with pytest.raises(InvalidDataStructureError):
        repo.get_financial_data("EXP-001")
