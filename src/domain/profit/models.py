from decimal import Decimal
from dataclasses import dataclass
from enum import Enum

class Decision(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    REJECT = "REJECT"

@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

@dataclass(frozen=True)
class FinancialData:
    price: Money
    supplier_price: Money
    commission_pct: Decimal
    shipping: Money
    other_costs: Money
    visible_sales: int

@dataclass(frozen=True)
class DecisionRules:
    minimum_margin_pct: Decimal
    excellent_margin_pct: Decimal
    minimum_sales: int

@dataclass(frozen=True)
class ProfitAnalysis:
    net_profit: Money
    net_margin_pct: Decimal
    decision: Decision
    commission: Money
    market_demand_ok: bool
