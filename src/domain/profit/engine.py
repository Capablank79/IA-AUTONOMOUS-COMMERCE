from decimal import Decimal
from .models import FinancialData, DecisionRules, ProfitAnalysis, Decision, Money

class ProfitEngine:
    def calculate(self, data: FinancialData, rules: DecisionRules) -> ProfitAnalysis:
        if len({data.price.currency, data.supplier_price.currency, 
                data.shipping.currency, data.other_costs.currency}) > 1:
            raise ValueError("All money values must have the same currency")
        
        currency = data.price.currency

        market_demand_ok = data.visible_sales >= rules.minimum_sales

        commission_amount = data.price.amount * (data.commission_pct / Decimal('100'))
        
        net_profit_amount = (
            data.price.amount 
            - commission_amount 
            - data.supplier_price.amount 
            - data.shipping.amount 
            - data.other_costs.amount
        )

        if data.price.amount == Decimal('0'):
            net_margin = Decimal('0')
        else:
            net_margin = (net_profit_amount / data.price.amount) * Decimal('100')

        if net_margin >= rules.excellent_margin_pct and market_demand_ok:
            decision = Decision.STRONG_BUY
        elif net_margin >= rules.minimum_margin_pct and market_demand_ok:
            decision = Decision.BUY
        else:
            decision = Decision.REJECT

        return ProfitAnalysis(
            net_profit=Money(amount=net_profit_amount, currency=currency),
            net_margin_pct=net_margin,
            decision=decision,
            commission=Money(amount=commission_amount, currency=currency),
            market_demand_ok=market_demand_ok
        )
