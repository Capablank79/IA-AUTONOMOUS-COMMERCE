import json
import decimal
from decimal import Decimal
from pathlib import Path
from typing import Union

from src.domain.profit.models import FinancialData, DecisionRules, Money
from src.domain.profit.ports import ProfitDataRepository


class JsonRepositoryError(Exception):
    """Base exception for JSON Repository errors."""
    pass


class DataDirectoryNotFoundError(JsonRepositoryError):
    """Raised when the data directory does not exist."""
    pass


class InvalidJsonError(JsonRepositoryError):
    """Raised when a JSON file is invalid."""
    pass


class ExperimentNotFoundError(JsonRepositoryError):
    """Raised when an experiment with the given ID is not found."""
    pass


class InvalidDataStructureError(JsonRepositoryError):
    """Raised when the JSON data structure is invalid or missing required fields."""
    pass


class JsonProfitDataRepository(ProfitDataRepository):
    """
    Infrastructure Adapter for ProfitDataRepository.
    Retrieves data from JSON files.
    """

    def __init__(self, data_dir: Union[Path, str]):
        self.data_dir = Path(data_dir)

    def _load_experiment_data(self, experiment_id: str) -> dict:
        if not self.data_dir.exists() or not self.data_dir.is_dir():
            raise DataDirectoryNotFoundError(f"Data directory not found: {self.data_dir}")

        for file_path in self.data_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    # Using parse_float=Decimal ensures that float values in JSON
                    # are parsed directly as Decimal objects to avoid precision loss.
                    data = json.load(f, parse_float=Decimal)
            except json.JSONDecodeError as e:
                raise InvalidJsonError(f"Invalid JSON in file {file_path.name}: {e}")

            if not isinstance(data, dict):
                raise InvalidDataStructureError(f"Root JSON is not an object in {file_path.name}")

            if data.get("experiment_id") == experiment_id:
                return data

        raise ExperimentNotFoundError(f"Experiment {experiment_id} not found in {self.data_dir}")

    def get_financial_data(self, experiment_id: str) -> FinancialData:
        data = self._load_experiment_data(experiment_id)

        try:
            market = data["market"]
            suppliers = data["suppliers"]

            # Currency mapping based on existing keys like "market_price_clp"
            currency = "CLP"

            price = Decimal(str(market["market_price_clp"]))
            supplier_price = Decimal(str(suppliers["test_supplier_price_clp"]))
            commission_pct = Decimal(str(market["marketplace_commission_pct"]))
            shipping = Decimal(str(market["shipping_cost_clp"]))
            other_costs = Decimal(str(market["other_costs_clp"]))
            visible_sales = int(market["visible_sales"])

            return FinancialData(
                price=Money(amount=price, currency=currency),
                supplier_price=Money(amount=supplier_price, currency=currency),
                commission_pct=commission_pct,
                shipping=Money(amount=shipping, currency=currency),
                other_costs=Money(amount=other_costs, currency=currency),
                visible_sales=visible_sales
            )
        except KeyError as e:
            raise InvalidDataStructureError(f"Missing expected key in experiment {experiment_id}: {e}")
        except (ValueError, TypeError, decimal.InvalidOperation) as e:
            raise InvalidDataStructureError(f"Invalid data type in experiment {experiment_id}: {e}")

    def get_decision_rules(self, experiment_id: str) -> DecisionRules:
        data = self._load_experiment_data(experiment_id)

        try:
            rules = data["decision_rules"]

            return DecisionRules(
                minimum_margin_pct=Decimal(str(rules["minimum_net_margin_pct"])),
                excellent_margin_pct=Decimal(str(rules["excellent_net_margin_pct"])),
                minimum_sales=int(rules["minimum_visible_sales"])
            )
        except KeyError as e:
            raise InvalidDataStructureError(f"Missing expected key in decision rules for {experiment_id}: {e}")
        except (ValueError, TypeError, decimal.InvalidOperation) as e:
            raise InvalidDataStructureError(f"Invalid data type in decision rules for {experiment_id}: {e}")
