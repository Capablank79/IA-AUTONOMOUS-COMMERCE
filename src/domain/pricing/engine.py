import uuid
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, Mapping, Any, Dict

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel
from src.domain.profit.models import (
    UnitEconomics,
    LandedCost,
    CostComponent,
    CostComponentType,
    SalePrice,
)
from src.domain.profit.engine import (
    UnitEconomicsCalculator,
    BreakEvenCalculator,
    LandedCostCalculator,
)
from src.domain.publication.models import SalesChannel
from .models import (
    PricingDecision,
    PricingAction,
    PricingRequest,
    PriceChangeReason,
)


class PricingDecisionEngine:
    """
    Motor determinista de decisiones de fijación y análisis de precios (G.4 / TASK 07.4).
    
    Principios inquebrantables:
    - No calcula precios en el vacío: utiliza landed cost, comisiones de canal, flete, impuestos y margen mínimo.
    - Calcula el Price Floor determinista: break_even_price o target_floor según el motor de Unit Economics.
    - Rechaza o marca violación si el precio propuesto no respeta el floor o margen mínimo.
    - Incorpora contexto de mercado (competencia, demanda, elasticidad estimada) sin saltarse las restricciones económicas.
    - Produce una PricingDecision inmutable y auditable.
    """

    @staticmethod
    def calculate_price_floor(
        unit_landed_cost: Decimal,
        marketplace_fee_rate: Optional[Decimal] = None,
        payment_fee_rate: Optional[Decimal] = None,
        shipping_cost: Optional[Decimal] = None,
        tax_rate: Optional[Decimal] = None,
        other_variable_costs: Optional[Decimal] = None,
        minimum_net_margin_pct: Optional[Decimal] = None,
        currency: str = "CLP",
    ) -> Decimal:
        """
        Calcula el precio de venta mínimo absoluto (Price Floor) para no caer por debajo
        del margen mínimo o punto de equilibrio.
        """
        # Usar BreakEvenCalculator del profit engine existente
        marketplace_fee_comp = (
            CostComponent.known(CostComponentType.MARKETPLACE_FEES, fee_rate=marketplace_fee_rate, currency=currency)
            if marketplace_fee_rate is not None
            else None
        )
        payment_fee_comp = (
            CostComponent.known(CostComponentType.PAYMENT_FEES, fee_rate=payment_fee_rate, currency=currency)
            if payment_fee_rate is not None
            else None
        )

        be_result = BreakEvenCalculator.calculate_break_even_price(
            unit_landed_cost=unit_landed_cost,
            marketplace_fees=marketplace_fee_comp,
            payment_fees=payment_fee_comp,
            fulfillment_per_unit=shipping_cost or Decimal("0"),
            other_variable_per_unit=other_variable_costs or Decimal("0"),
            target_net_margin_pct=minimum_net_margin_pct,
            currency=currency,
        )

        if not be_result.is_computable or be_result.break_even_sale_price is None:
            # Fallback seguro al costo unitario landed si no hay estructura de comisiones
            return unit_landed_cost

        if minimum_net_margin_pct and be_result.target_net_margin_price is not None:
            return be_result.target_net_margin_price

        return be_result.break_even_sale_price

    @classmethod
    def evaluate_pricing_decision(
        cls,
        listing_id: str,
        channel: SalesChannel,
        current_price: Decimal,
        proposed_price: Decimal,
        unit_landed_cost: Decimal,
        marketplace_fee_rate: Optional[Decimal] = Decimal("0.13"),
        payment_fee_rate: Optional[Decimal] = Decimal("0.0"),
        shipping_cost: Optional[Decimal] = Decimal("0"),
        tax_rate: Optional[Decimal] = Decimal("0.19"),
        other_variable_costs: Optional[Decimal] = Decimal("0"),
        minimum_net_margin_pct: Optional[Decimal] = Decimal("0.10"),
        target_net_margin_pct: Optional[Decimal] = Decimal("0.20"),
        product_id: Optional[str] = None,
        reason: PriceChangeReason = PriceChangeReason.MARGIN_OPTIMIZATION,
        market_competitor_price: Optional[Decimal] = None,
        confidence: Confidence = Confidence.HIGH,
        risk_level: RiskLevel = RiskLevel.LOW,
        rationale: str = "",
        evidence: Optional[Mapping[str, Any]] = None,
        max_price_change_pct: Decimal = Decimal("30.0"),
    ) -> PricingDecision:
        """
        Construye una PricingDecision evaluando economía unitaria, price floor y límites.
        """
        currency = channel.currency

        # 1. Calcular price floor determinista
        price_floor = cls.calculate_price_floor(
            unit_landed_cost=unit_landed_cost,
            marketplace_fee_rate=marketplace_fee_rate,
            payment_fee_rate=payment_fee_rate,
            shipping_cost=shipping_cost,
            tax_rate=tax_rate,
            other_variable_costs=other_variable_costs,
            minimum_net_margin_pct=minimum_net_margin_pct,
            currency=currency,
        )

        # 2. Calcular target price deseado
        target_price = cls.calculate_price_floor(
            unit_landed_cost=unit_landed_cost,
            marketplace_fee_rate=marketplace_fee_rate,
            payment_fee_rate=payment_fee_rate,
            shipping_cost=shipping_cost,
            tax_rate=tax_rate,
            other_variable_costs=other_variable_costs,
            minimum_net_margin_pct=target_net_margin_pct,
            currency=currency,
        )

        # 3. Calcular Unit Economics para proposed_price
        landed_cost_comp = CostComponent.known(CostComponentType.PRODUCT_COST, amount=unit_landed_cost, currency=currency)
        shipping_cost_comp = CostComponent.known(CostComponentType.SHIPPING_COST, amount=shipping_cost or Decimal("0"), currency=currency)
        landed_cost_obj = LandedCostCalculator.calculate(
            product_id=product_id or listing_id,
            supplier_id="SUPPLIER_DEFAULT",
            quantity=1,
            purchase_cost=landed_cost_comp,
            shipping_cost=shipping_cost_comp,
            target_currency=currency,
        )

        mkt_fee_comp = (
            CostComponent.known(CostComponentType.MARKETPLACE_FEES, fee_rate=marketplace_fee_rate, currency=currency)
            if marketplace_fee_rate is not None
            else None
        )
        pay_fee_comp = (
            CostComponent.known(CostComponentType.PAYMENT_FEES, fee_rate=payment_fee_rate, currency=currency)
            if payment_fee_rate is not None
            else None
        )
        tax_comp = (
            CostComponent.known(CostComponentType.TAXES, fee_rate=tax_rate, currency=currency)
            if tax_rate is not None
            else None
        )
        other_comp = (
            CostComponent.known(CostComponentType.OTHER_VARIABLE_COSTS, amount=other_variable_costs, currency=currency)
            if other_variable_costs is not None
            else None
        )

        ue = UnitEconomicsCalculator.calculate_unit_economics(
            product_id=product_id or listing_id,
            supplier_id="SUPPLIER_DEFAULT",
            quantity_scenario=1,
            sale_price=SalePrice.observed(proposed_price, currency=currency),
            purchase_cost=landed_cost_comp,
            shipping_cost=shipping_cost_comp,
            landed_cost=landed_cost_obj,
            taxes=tax_comp,
            marketplace_fees=mkt_fee_comp,
            payment_fees=pay_fee_comp,
            other_costs=other_comp,
        )

        expected_margin_pct = ue.net_margin_pct
        expected_profit_amount = ue.net_profit

        # 4. Chequeo de límites para flags de aprobación
        pct_change = abs(((proposed_price - current_price) / current_price) * Decimal("100")) if current_price > Decimal("0") else Decimal("0")
        requires_approval = (
            pct_change > max_price_change_pct
            or risk_level == RiskLevel.HIGH
            or (minimum_net_margin_pct is not None and expected_margin_pct < minimum_net_margin_pct)
        )

        evidence_dict: Dict[str, Any] = {
            "unit_landed_cost": str(unit_landed_cost),
            "marketplace_fee_rate": str(marketplace_fee_rate) if marketplace_fee_rate is not None else None,
            "minimum_net_margin_pct": str(minimum_net_margin_pct) if minimum_net_margin_pct is not None else None,
            "target_net_margin_pct": str(target_net_margin_pct) if target_net_margin_pct is not None else None,
            "market_competitor_price": str(market_competitor_price) if market_competitor_price is not None else None,
            "price_delta_pct": str(pct_change),
            "reason": reason.value,
        }
        if evidence:
            evidence_dict.update(dict(evidence))

        constraints: Dict[str, Any] = {
            "minimum_allowed_price": str(price_floor),
            "max_price_change_pct": str(max_price_change_pct),
        }

        decision_id = f"PDEC-{uuid.uuid4().hex[:12]}"

        full_rationale = rationale or f"Adjust price from {current_price} to {proposed_price} {currency} based on {reason.value}."

        return PricingDecision(
            decision_id=decision_id,
            listing_id=listing_id,
            channel=channel,
            current_price=current_price,
            proposed_price=proposed_price,
            minimum_allowed_price=price_floor,
            target_price=target_price,
            currency=currency,
            product_id=product_id,
            unit_economics=ue,
            expected_margin_pct=expected_margin_pct,
            expected_profit_amount=expected_profit_amount,
            rationale=full_rationale,
            evidence=evidence_dict,
            confidence=confidence,
            risk_level=risk_level,
            constraints=constraints,
            requires_approval=requires_approval,
        )

    @classmethod
    def propose_pricing_decision(cls, *args, **kwargs) -> PricingDecision:
        return cls.evaluate_pricing_decision(*args, **kwargs)

    @classmethod
    def evaluate_price_economics(
        cls,
        proposed_price: Decimal,
        unit_landed_cost: Decimal,
        marketplace_fee_rate: Optional[Decimal] = None,
        payment_fee_rate: Optional[Decimal] = None,
        shipping_cost: Optional[Decimal] = None,
        tax_rate: Optional[Decimal] = None,
        other_variable_costs: Optional[Decimal] = None,
        currency: str = "CLP",
    ) -> Any:
        landed_cost_comp = CostComponent.known(CostComponentType.PRODUCT_COST, amount=unit_landed_cost, currency=currency)
        shipping_cost_comp = CostComponent.known(CostComponentType.SHIPPING_COST, amount=Decimal("0"), currency=currency)
        landed_cost_obj = LandedCostCalculator.calculate(
            product_id="TEMP_EVAL",
            supplier_id="SUPPLIER_DEFAULT",
            quantity=1,
            purchase_cost=landed_cost_comp,
            shipping_cost=shipping_cost_comp,
            target_currency=currency,
        )
        mkt_fee_comp = (
            CostComponent.known(CostComponentType.MARKETPLACE_FEES, fee_rate=marketplace_fee_rate, currency=currency)
            if marketplace_fee_rate is not None
            else None
        )
        pay_fee_comp = (
            CostComponent.known(CostComponentType.PAYMENT_FEES, fee_rate=payment_fee_rate, currency=currency)
            if payment_fee_rate is not None
            else None
        )
        ship_comp = (
            CostComponent.known(CostComponentType.SHIPPING_COST, amount=shipping_cost, currency=currency)
            if shipping_cost is not None
            else None
        )
        tax_comp = (
            CostComponent.known(CostComponentType.TAXES, fee_rate=tax_rate, currency=currency)
            if tax_rate is not None
            else None
        )
        other_comp = (
            CostComponent.known(CostComponentType.OTHER_VARIABLE_COSTS, amount=other_variable_costs, currency=currency)
            if other_variable_costs is not None
            else None
        )
        ue = UnitEconomicsCalculator.calculate_unit_economics(
            product_id="TEMP_EVAL",
            supplier_id="SUPPLIER_DEFAULT",
            quantity_scenario=1,
            sale_price=SalePrice.observed(proposed_price, currency=currency),
            purchase_cost=landed_cost_comp,
            shipping_cost=shipping_cost_comp,
            landed_cost=landed_cost_obj,
            taxes=tax_comp,
            marketplace_fees=mkt_fee_comp,
            payment_fees=pay_fee_comp,
            other_costs=other_comp,
        )
        return ue

    @staticmethod
    def create_pricing_action(
        decision: PricingDecision,
        correlation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> PricingAction:
        """
        Convierte una PricingDecision válida en una PricingAction ejecutable.
        """
        action_id = f"PACT-{uuid.uuid4().hex[:12]}"
        req_id = request_id or f"REQ-{uuid.uuid4().hex[:12]}"
        corr_id = correlation_id or f"CORR-{uuid.uuid4().hex[:12]}"
        # Idempotency key determinista basada en listing_id y new_price si no se proporciona
        idemp_key = idempotency_key or f"IDEMP-PRICE-{decision.listing_id}-{decision.proposed_price}"

        return PricingAction(
            action_id=action_id,
            decision_id=decision.decision_id,
            listing_id=decision.listing_id,
            channel=decision.channel,
            old_price=decision.current_price,
            new_price=decision.proposed_price,
            proposed_price=decision.proposed_price,
            current_price=decision.current_price,
            currency=decision.currency,
            reason=decision.evidence.get("reason", decision.rationale) if decision.evidence else decision.rationale,
            request_id=req_id,
            idempotency_key=idemp_key,
            correlation_id=corr_id,
        )
