from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict, Any, Mapping
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import (
    EvidenceProvenanceType,
    PriceTier,
    CommercialQuote,
    SupplierRecommendation,
    ShippingOption,
)
from .models import (
    Money,
    FinancialData,
    DecisionRules,
    ProfitAnalysis,
    Decision,
    CostComponentType,
    CostComponentStatus,
    SalePriceType,
    LandedCostStatus,
    ProfitStatus,
    EconomicScenarioType,
    ExchangeRate,
    CostComponent,
    SalePrice,
    Revenue,
    LandedCost,
    ProfitResult,
    MarginResult,
    BreakEvenResult,
    UnitEconomics,
    EconomicInvestigationNeed,
    ProfitTrace,
    ScenarioAnalysisResult,
    EconomicEvaluationResult,
    MarketplaceFeeStructure,
)


class LandedCostCalculator:
    """
    Calculador determinista de Landed Cost (Costo Real puesto en destino).
    LANDED COST = purchase_cost + shipping_cost + duties_cost + taxes_cost + other_acquisition_cost.

    Reglas estrictas:
    - Solo suma componentes con status KNOWN.
    - UNKNOWN != 0, UNKNOWN != FREE, UNKNOWN != ASSUMED.
    - Si falta algún componente crítico (purchase_cost o shipping_cost), el estado es PARTIAL o INCOMPLETE.
    - Si todas las divisas no coinciden o no hay FX válido, estado NOT_COMPARABLE_CURRENCY.
    """

    @staticmethod
    def calculate(
        product_id: str,
        supplier_id: str,
        quantity: int,
        purchase_cost: CostComponent,
        shipping_cost: CostComponent,
        duties_cost: Optional[CostComponent] = None,
        taxes_cost: Optional[CostComponent] = None,
        other_acquisition_cost: Optional[CostComponent] = None,
        target_currency: Optional[str] = None,
        exchange_rates: Optional[Mapping[Tuple[str, str], ExchangeRate]] = None,
        exchange_rate: Optional[ExchangeRate] = None,
    ) -> LandedCost:
        if quantity < 1:
            raise ValueError("quantity must be at least 1")

        currency = target_currency or purchase_cost.currency
        unknowns: List[str] = []
        components_list: List[CostComponent] = []

        # Normalizar FX map
        fx_map: Dict[Tuple[str, str], ExchangeRate] = dict(exchange_rates) if exchange_rates else {}
        if exchange_rate is not None:
            fx_map[(exchange_rate.from_currency, exchange_rate.to_currency)] = exchange_rate

        # Helper para convertir un componente a target_currency
        def convert_component_if_needed(comp: CostComponent) -> Tuple[Optional[Decimal], CostComponent]:
            if comp.status != CostComponentStatus.KNOWN or comp.amount is None:
                return None, comp
            if comp.currency == currency:
                return comp.amount, comp
            # Buscar FX
            fx = fx_map.get((comp.currency, currency))
            if fx is None:
                return None, comp
            converted_amount = comp.amount * fx.rate
            converted_comp = CostComponent(
                component_type=comp.component_type,
                status=CostComponentStatus.KNOWN,
                amount=converted_amount,
                currency=currency,
                fee_rate=comp.fee_rate,
                fixed_fee_amount=comp.fixed_fee_amount * fx.rate if comp.fixed_fee_amount is not None else None,
                confidence=comp.confidence,
                provenance_type=comp.provenance_type,
                source=f"{comp.source}_FX_{fx.rate}",
                effective_at=comp.effective_at,
                details=f"Converted from {comp.amount} {comp.currency} @ rate {fx.rate}",
                is_per_unit=comp.is_per_unit,
            )
            return converted_amount, converted_comp

        # Valores por defecto para componentes opcionales
        if duties_cost is None:
            duties_cost = CostComponent(
                component_type=CostComponentType.IMPORT_DUTIES,
                status=CostComponentStatus.NOT_APPLICABLE,
                amount=Decimal("0"),
                currency=currency,
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source="DEFAULT_DOMESTIC",
                details="No import duties applicable for domestic sourcing",
            )
        if taxes_cost is None:
            taxes_cost = CostComponent(
                component_type=CostComponentType.TAXES,
                status=CostComponentStatus.NOT_APPLICABLE,
                amount=Decimal("0"),
                currency=currency,
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source="DEFAULT_DOMESTIC",
                details="No specific acquisition tax component specified",
            )
        if other_acquisition_cost is None:
            other_acquisition_cost = CostComponent(
                component_type=CostComponentType.OTHER_VARIABLE_COSTS,
                status=CostComponentStatus.NOT_APPLICABLE,
                amount=Decimal("0"),
                currency=currency,
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source="DEFAULT_NONE",
                details="No other acquisition costs",
            )

        all_components = [
            purchase_cost,
            shipping_cost,
            duties_cost,
            taxes_cost,
            other_acquisition_cost,
        ]

        # Validar consistencia de divisas
        currencies_present = {c.currency for c in all_components if c.status == CostComponentStatus.KNOWN}
        has_multi_currency = any(c != currency for c in currencies_present)
        if has_multi_currency:
            # Verificar si tenemos exchange rates válidos para todas las monedas extranjeras presentes
            missing_fx = False
            for c_curr in currencies_present:
                if c_curr != currency and (c_curr, currency) not in fx_map:
                    missing_fx = True
                    break
            if missing_fx:
                return LandedCost(
                    product_id=product_id,
                    supplier_id=supplier_id,
                    quantity=quantity,
                    currency=currency,
                    purchase_cost=purchase_cost,
                    shipping_cost=shipping_cost,
                    duties_cost=duties_cost,
                    taxes_cost=taxes_cost,
                    other_acquisition_cost=other_acquisition_cost,
                    total_landed_cost=None,
                    unit_landed_cost=None,
                    status=LandedCostStatus.NOT_COMPARABLE_CURRENCY,
                    confidence=Confidence.UNKNOWN,
                    provenance_type=EvidenceProvenanceType.DERIVED,
                    unknowns=("MULTI_CURRENCY_WITHOUT_FX",),
                    components=tuple(all_components),
                )

        # Evaluar componentes conocidos y desconocidos
        total_sum = Decimal("0")
        is_complete = True
        has_critical_unknown = False

        # Purchase cost es crítico
        if purchase_cost.status == CostComponentStatus.KNOWN and purchase_cost.amount is not None:
            conv_amt, conv_comp = convert_component_if_needed(purchase_cost)
            # Si purchase_cost es unitario, multiplicar por cantidad para el total
            p_amt = conv_amt * Decimal(str(quantity)) if conv_comp.is_per_unit else conv_amt
            total_sum += p_amt
            components_list.append(conv_comp)
        elif purchase_cost.status == CostComponentStatus.UNKNOWN:
            unknowns.append("PURCHASE_COST_UNKNOWN")
            is_complete = False
            has_critical_unknown = True

        # Shipping cost es crítico
        if shipping_cost.status == CostComponentStatus.KNOWN and shipping_cost.amount is not None:
            conv_amt, conv_comp = convert_component_if_needed(shipping_cost)
            # Shipping suele ser por pedido completo a menos que is_per_unit sea True
            s_amt = conv_amt * Decimal(str(quantity)) if conv_comp.is_per_unit else conv_amt
            total_sum += s_amt
            components_list.append(conv_comp)
        elif shipping_cost.status == CostComponentStatus.UNKNOWN:
            unknowns.append("SHIPPING_COST_UNKNOWN")
            is_complete = False
            has_critical_unknown = True

        # Duties
        if duties_cost.status == CostComponentStatus.KNOWN and duties_cost.amount is not None:
            conv_amt, conv_comp = convert_component_if_needed(duties_cost)
            d_amt = conv_amt * Decimal(str(quantity)) if conv_comp.is_per_unit else conv_amt
            total_sum += d_amt
            components_list.append(conv_comp)
        elif duties_cost.status == CostComponentStatus.UNKNOWN:
            unknowns.append("DUTIES_COST_UNKNOWN")
            is_complete = False

        # Taxes
        if taxes_cost.status == CostComponentStatus.KNOWN and taxes_cost.amount is not None:
            conv_amt, conv_comp = convert_component_if_needed(taxes_cost)
            t_amt = conv_amt * Decimal(str(quantity)) if conv_comp.is_per_unit else conv_amt
            total_sum += t_amt
            components_list.append(conv_comp)
        elif taxes_cost.status == CostComponentStatus.UNKNOWN:
            unknowns.append("TAXES_COST_UNKNOWN")
            is_complete = False

        # Other acquisition costs
        if other_acquisition_cost.status == CostComponentStatus.KNOWN and other_acquisition_cost.amount is not None:
            conv_amt, conv_comp = convert_component_if_needed(other_acquisition_cost)
            o_amt = conv_amt * Decimal(str(quantity)) if conv_comp.is_per_unit else conv_amt
            total_sum += o_amt
            components_list.append(conv_comp)
        elif other_acquisition_cost.status == CostComponentStatus.UNKNOWN:
            unknowns.append("OTHER_ACQUISITION_COST_UNKNOWN")
            is_complete = False

        # Determinar status y confidence
        if has_critical_unknown:
            status = LandedCostStatus.INCOMPLETE
            total_landed = None
            unit_landed = None
            confidence = Confidence.UNKNOWN
        elif not is_complete:
            status = LandedCostStatus.PARTIAL
            total_landed = total_sum
            unit_landed = total_sum / Decimal(str(quantity))
            confidence = Confidence.LOW
        else:
            status = LandedCostStatus.COMPLETE
            total_landed = total_sum
            unit_landed = total_sum / Decimal(str(quantity))
            # Degradación de confianza si hay FIXTURE
            has_fixture = any(c.provenance_type == EvidenceProvenanceType.FIXTURE for c in all_components if c.status == CostComponentStatus.KNOWN)
            if has_fixture:
                confidence = Confidence.MEDIUM
            else:
                confidences = [c.confidence for c in all_components if c.status == CostComponentStatus.KNOWN]
                if all(c == Confidence.HIGH for c in confidences):
                    confidence = Confidence.HIGH
                else:
                    confidence = Confidence.MEDIUM

        # Provenance consolidada
        if any(c.provenance_type == EvidenceProvenanceType.FIXTURE for c in all_components):
            overall_prov = EvidenceProvenanceType.FIXTURE
        elif any(c.provenance_type == EvidenceProvenanceType.LIVE for c in all_components):
            overall_prov = EvidenceProvenanceType.LIVE
        else:
            overall_prov = EvidenceProvenanceType.DERIVED

        return LandedCost(
            product_id=product_id,
            supplier_id=supplier_id,
            quantity=quantity,
            currency=currency,
            purchase_cost=purchase_cost,
            shipping_cost=shipping_cost,
            duties_cost=duties_cost,
            taxes_cost=taxes_cost,
            other_acquisition_cost=other_acquisition_cost,
            total_landed_cost=total_landed,
            unit_landed_cost=unit_landed,
            status=status,
            confidence=confidence,
            provenance_type=overall_prov,
            unknowns=tuple(unknowns),
            components=tuple(all_components),
        )


class UnitEconomicsCalculator:
    """
    Calculador determinista de Economía Unitaria (Unit Economics), Margen y Ganancia.

    Fórmulas deterministas:
    - Gross Profit = Sale Price - Unit Landed Cost
    - Gross Margin % = ((Sale Price - Unit Landed Cost) / Sale Price) * 100
    - Net Profit = Sale Price - Unit Landed Cost - Marketplace Fees - Payment Fees - Packaging - Fulfillment - Other Costs
    - Net Margin % = (Net Profit / Sale Price) * 100
    - Markup % = ((Sale Price - Unit Landed Cost) / Unit Landed Cost) * 100
    """

    @staticmethod
    def calculate_unit_economics(
        product_id: str,
        supplier_id: str,
        quantity_scenario: int,
        sale_price: SalePrice,
        purchase_cost: CostComponent,
        shipping_cost: CostComponent,
        landed_cost: Optional[LandedCost] = None,
        import_duties: Optional[CostComponent] = None,
        taxes: Optional[CostComponent] = None,
        marketplace_fees: Optional[CostComponent] = None,
        payment_fees: Optional[CostComponent] = None,
        packaging_cost: Optional[CostComponent] = None,
        fulfillment_cost: Optional[CostComponent] = None,
        other_costs: Optional[CostComponent] = None,
        exchange_rate: Optional[ExchangeRate] = None,
    ) -> UnitEconomics:
        trace_steps: List[str] = []
        unknowns: List[str] = []
        currency = sale_price.currency

        # Moneda del costo de compra vs precio de venta
        if purchase_cost.currency != currency or shipping_cost.currency != currency:
            if exchange_rate is None or exchange_rate.from_currency != purchase_cost.currency or exchange_rate.to_currency != currency:
                trace_steps.append(f"Currency mismatch: purchase={purchase_cost.currency}, shipping={shipping_cost.currency}, sale={currency}. No valid FX provided.")
                # Retornar estado NOT_COMPARABLE_CURRENCY
                dummy_landed = landed_cost or LandedCostCalculator.calculate(
                    product_id=product_id,
                    supplier_id=supplier_id,
                    quantity=quantity_scenario,
                    purchase_cost=purchase_cost,
                    shipping_cost=shipping_cost,
                    duties_cost=import_duties,
                    taxes_cost=taxes,
                    other_acquisition_cost=other_costs,
                    target_currency=currency,
                    exchange_rate=exchange_rate,
                )
                return UnitEconomics(
                    product_id=product_id,
                    supplier_id=supplier_id,
                    quantity_scenario=quantity_scenario,
                    sale_price=sale_price,
                    purchase_cost=purchase_cost,
                    shipping_cost=shipping_cost,
                    import_duties=import_duties or CostComponent(CostComponentType.IMPORT_DUTIES, CostComponentStatus.UNKNOWN),
                    taxes=taxes or CostComponent(CostComponentType.TAXES, CostComponentStatus.UNKNOWN),
                    marketplace_fees=marketplace_fees or CostComponent(CostComponentType.MARKETPLACE_FEES, CostComponentStatus.UNKNOWN),
                    payment_fees=payment_fees or CostComponent(CostComponentType.PAYMENT_FEES, CostComponentStatus.UNKNOWN),
                    packaging_cost=packaging_cost or CostComponent(CostComponentType.PACKAGING, CostComponentStatus.UNKNOWN),
                    fulfillment_cost=fulfillment_cost or CostComponent(CostComponentType.FULFILLMENT, CostComponentStatus.UNKNOWN),
                    other_costs=other_costs or CostComponent(CostComponentType.OTHER_VARIABLE_COSTS, CostComponentStatus.UNKNOWN),
                    landed_cost=dummy_landed,
                    gross_profit=None,
                    net_profit=None,
                    gross_margin_pct=None,
                    net_margin_pct=None,
                    unit_markup_pct=None,
                    status=ProfitStatus.NOT_COMPARABLE_CURRENCY,
                    currency=currency,
                    confidence=Confidence.UNKNOWN,
                    provenance_type=EvidenceProvenanceType.DERIVED,
                    unknowns=("CURRENCY_NOT_COMPARABLE_WITHOUT_FX",),
                    trace=tuple(trace_steps),
                )

        # Usar landed_cost provisto o calcularlo
        if landed_cost is not None:
            effective_landed_cost = landed_cost
        else:
            effective_landed_cost = LandedCostCalculator.calculate(
                product_id=product_id,
                supplier_id=supplier_id,
                quantity=quantity_scenario,
                purchase_cost=purchase_cost,
                shipping_cost=shipping_cost,
                duties_cost=import_duties,
                taxes_cost=taxes,
                other_acquisition_cost=other_costs,
                target_currency=currency,
                exchange_rate=exchange_rate,
            )

        trace_steps.append(f"Landed Cost Status: {effective_landed_cost.status.value}, Unit Landed: {effective_landed_cost.unit_landed_cost}")
        for u in effective_landed_cost.unknowns:
            unknowns.append(u)

        # Preparar componentes de canal y operación
        if marketplace_fees is None:
            marketplace_fees = CostComponent(
                component_type=CostComponentType.MARKETPLACE_FEES,
                status=CostComponentStatus.UNKNOWN,
                currency=currency,
            )
        if payment_fees is None:
            payment_fees = CostComponent(
                component_type=CostComponentType.PAYMENT_FEES,
                status=CostComponentStatus.NOT_APPLICABLE,
                amount=Decimal("0"),
                currency=currency,
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source="INCLUDED_IN_MARKETPLACE",
            )
        if packaging_cost is None:
            packaging_cost = CostComponent(
                component_type=CostComponentType.PACKAGING,
                status=CostComponentStatus.NOT_APPLICABLE,
                amount=Decimal("0"),
                currency=currency,
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source="SUPPLIER_PACKAGED",
            )
        if fulfillment_cost is None:
            fulfillment_cost = CostComponent(
                component_type=CostComponentType.FULFILLMENT,
                status=CostComponentStatus.NOT_APPLICABLE,
                amount=Decimal("0"),
                currency=currency,
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source="MERCHANT_FULFILLED",
            )
        if other_costs is None:
            other_costs = CostComponent(
                component_type=CostComponentType.OTHER_VARIABLE_COSTS,
                status=CostComponentStatus.NOT_APPLICABLE,
                amount=Decimal("0"),
                currency=currency,
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source="NONE",
            )

        # Evaluar Gross Profit & Gross Margin
        gross_profit: Optional[Decimal] = None
        gross_margin_pct: Optional[Decimal] = None
        unit_markup_pct: Optional[Decimal] = None

        if effective_landed_cost.unit_landed_cost is not None:
            gross_profit = sale_price.amount - effective_landed_cost.unit_landed_cost
            gross_margin_pct = (gross_profit / sale_price.amount) * Decimal("100")
            if effective_landed_cost.unit_landed_cost > Decimal("0"):
                unit_markup_pct = (gross_profit / effective_landed_cost.unit_landed_cost) * Decimal("100")
            trace_steps.append(f"Gross Profit: {gross_profit} ({currency}), Gross Margin: {gross_margin_pct:.2f}%")
        else:
            trace_steps.append("Gross Profit cannot be calculated: Unit Landed Cost is incomplete/unknown.")

        # Evaluar Marketplace Fees
        mp_fee_amount: Optional[Decimal] = None
        if marketplace_fees.status == CostComponentStatus.KNOWN:
            if marketplace_fees.amount is not None:
                mp_fee_amount = marketplace_fees.amount
            elif marketplace_fees.fee_rate is not None:
                fixed_part = marketplace_fees.fixed_fee_amount or Decimal("0")
                mp_fee_amount = (sale_price.amount * marketplace_fees.fee_rate) + fixed_part
                # Crear componente con amount calculado
                marketplace_fees = CostComponent(
                    component_type=CostComponentType.MARKETPLACE_FEES,
                    status=CostComponentStatus.KNOWN,
                    amount=mp_fee_amount,
                    currency=currency,
                    fee_rate=marketplace_fees.fee_rate,
                    fixed_fee_amount=marketplace_fees.fixed_fee_amount,
                    confidence=marketplace_fees.confidence,
                    provenance_type=marketplace_fees.provenance_type,
                    source=marketplace_fees.source,
                    details=f"Rate {marketplace_fees.fee_rate * Decimal('100')}% + fixed {fixed_part}",
                )
            trace_steps.append(f"Marketplace Fee: {mp_fee_amount} ({currency})")
        elif marketplace_fees.status == CostComponentStatus.UNKNOWN:
            unknowns.append("MARKETPLACE_FEES_UNKNOWN")
            trace_steps.append("Marketplace Fee is UNKNOWN.")

        # Evaluar Payment Fees
        pay_fee_amount: Optional[Decimal] = None
        if payment_fees.status == CostComponentStatus.KNOWN:
            if payment_fees.amount is not None:
                pay_fee_amount = payment_fees.amount
            elif payment_fees.fee_rate is not None:
                fixed_part = payment_fees.fixed_fee_amount or Decimal("0")
                pay_fee_amount = (sale_price.amount * payment_fees.fee_rate) + fixed_part
                payment_fees = CostComponent(
                    component_type=CostComponentType.PAYMENT_FEES,
                    status=CostComponentStatus.KNOWN,
                    amount=pay_fee_amount,
                    currency=currency,
                    fee_rate=payment_fees.fee_rate,
                    fixed_fee_amount=payment_fees.fixed_fee_amount,
                    confidence=payment_fees.confidence,
                    provenance_type=payment_fees.provenance_type,
                    source=payment_fees.source,
                    details=f"Rate {payment_fees.fee_rate * Decimal('100')}% + fixed {fixed_part}",
                )
            trace_steps.append(f"Payment Fee: {pay_fee_amount} ({currency})")
        elif payment_fees.status == CostComponentStatus.UNKNOWN:
            unknowns.append("PAYMENT_FEES_UNKNOWN")
            trace_steps.append("Payment Fee is UNKNOWN.")
        elif payment_fees.status == CostComponentStatus.NOT_APPLICABLE:
            pay_fee_amount = Decimal("0")

        # Evaluar Packaging, Fulfillment, Other Costs
        pkg_amount = packaging_cost.amount if packaging_cost.status == CostComponentStatus.KNOWN else Decimal("0")
        if packaging_cost.status == CostComponentStatus.UNKNOWN:
            unknowns.append("PACKAGING_COST_UNKNOWN")

        ful_amount = fulfillment_cost.amount if fulfillment_cost.status == CostComponentStatus.KNOWN else Decimal("0")
        if fulfillment_cost.status == CostComponentStatus.UNKNOWN:
            unknowns.append("FULFILLMENT_COST_UNKNOWN")

        oth_amount = other_costs.amount if other_costs.status == CostComponentStatus.KNOWN else Decimal("0")
        if other_costs.status == CostComponentStatus.UNKNOWN:
            unknowns.append("OTHER_VARIABLE_COST_UNKNOWN")

        # Evaluar Net Profit & Net Margin
        net_profit: Optional[Decimal] = None
        net_margin_pct: Optional[Decimal] = None
        critical_operating_unknown = (
            marketplace_fees.status == CostComponentStatus.UNKNOWN
            or payment_fees.status == CostComponentStatus.UNKNOWN
            or effective_landed_cost.status in (LandedCostStatus.INCOMPLETE, LandedCostStatus.NOT_COMPARABLE_CURRENCY)
        )

        if not critical_operating_unknown and gross_profit is not None and mp_fee_amount is not None and pay_fee_amount is not None:
            channel_and_operating_costs = mp_fee_amount + pay_fee_amount + pkg_amount + ful_amount + oth_amount
            net_profit = gross_profit - channel_and_operating_costs
            net_margin_pct = (net_profit / sale_price.amount) * Decimal("100")
            trace_steps.append(f"Net Profit: {net_profit} ({currency}), Net Margin: {net_margin_pct:.2f}%")
        else:
            trace_steps.append("Net Profit cannot be calculated due to missing critical costs (Landed Cost / Channel Fees).")

        # Determinar status global de Profit
        if effective_landed_cost.status == LandedCostStatus.NOT_COMPARABLE_CURRENCY:
            status = ProfitStatus.NOT_COMPARABLE_CURRENCY
        elif effective_landed_cost.status == LandedCostStatus.INCOMPLETE:
            status = ProfitStatus.PROFIT_INCOMPLETE
        elif net_profit is not None and not unknowns:
            status = ProfitStatus.PROFIT_COMPLETE
        elif gross_profit is not None or net_profit is not None:
            status = ProfitStatus.PROFIT_PARTIAL
        else:
            status = ProfitStatus.PROFIT_UNKNOWN

        # Determinar confianza económica
        all_eval_components = [
            purchase_cost,
            shipping_cost,
            import_duties,
            taxes,
            marketplace_fees,
            payment_fees,
            packaging_cost,
            fulfillment_cost,
            other_costs,
        ]
        has_fixture = any(
            c.provenance_type == EvidenceProvenanceType.FIXTURE
            for c in all_eval_components
            if c is not None and c.status == CostComponentStatus.KNOWN
        ) or sale_price.provenance_type == EvidenceProvenanceType.FIXTURE

        if status == ProfitStatus.PROFIT_UNKNOWN or status == ProfitStatus.NOT_COMPARABLE_CURRENCY:
            confidence = Confidence.UNKNOWN
        elif status == ProfitStatus.PROFIT_PARTIAL:
            confidence = Confidence.LOW
        elif has_fixture:
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.HIGH

        # Provenance
        if any(c.provenance_type == EvidenceProvenanceType.FIXTURE for c in all_eval_components if c is not None):
            prov_type = EvidenceProvenanceType.FIXTURE
        elif any(c.provenance_type == EvidenceProvenanceType.LIVE for c in all_eval_components if c is not None):
            prov_type = EvidenceProvenanceType.LIVE
        else:
            prov_type = EvidenceProvenanceType.DERIVED

        return UnitEconomics(
            product_id=product_id,
            supplier_id=supplier_id,
            quantity_scenario=quantity_scenario,
            sale_price=sale_price,
            purchase_cost=purchase_cost,
            shipping_cost=shipping_cost,
            import_duties=import_duties or CostComponent(CostComponentType.IMPORT_DUTIES, CostComponentStatus.NOT_APPLICABLE),
            taxes=taxes or CostComponent(CostComponentType.TAXES, CostComponentStatus.NOT_APPLICABLE),
            marketplace_fees=marketplace_fees,
            payment_fees=payment_fees,
            packaging_cost=packaging_cost,
            fulfillment_cost=fulfillment_cost,
            other_costs=other_costs,
            landed_cost=effective_landed_cost,
            gross_profit=gross_profit,
            net_profit=net_profit,
            gross_margin_pct=gross_margin_pct,
            net_margin_pct=net_margin_pct,
            unit_markup_pct=unit_markup_pct,
            status=status,
            currency=currency,
            confidence=confidence,
            provenance_type=prov_type,
            unknowns=tuple(unknowns),
            trace=tuple(trace_steps),
        )


class BreakEvenCalculator:
    """
    Calculador determinista de Punto de Equilibrio (Break-Even).
    Responde:
    1. ¿Cuál es el precio mínimo de venta para no perder dinero (Net Profit = 0)?
       Fórmula:
       Break-Even Sale Price = (Unit Landed Cost + Fixed Fees per Unit) / (1 - Variable Fee Rates)
    2. ¿Cuántas unidades deben venderse para cubrir costos fijos totales?
       Fórmula:
       Break-Even Units = Fixed Costs / (Sale Price - Unit Variable Cost)
    """

    @staticmethod
    def calculate_break_even_price(
        landed_cost: Optional[LandedCost] = None,
        unit_landed_cost: Optional[Decimal] = None,
        marketplace_fees: Optional[CostComponent] = None,
        payment_fees: Optional[CostComponent] = None,
        marketplace_fee_rate: Optional[Decimal] = Decimal("0"),
        marketplace_fixed_fee: Optional[Decimal] = Decimal("0"),
        payment_fee_rate: Optional[Decimal] = Decimal("0"),
        payment_fixed_fee: Optional[Decimal] = Decimal("0"),
        packaging_per_unit: Optional[Decimal] = Decimal("0"),
        fulfillment_per_unit: Optional[Decimal] = Decimal("0"),
        other_variable_per_unit: Optional[Decimal] = Decimal("0"),
        target_net_margin_pct: Optional[Decimal] = None,
        currency: str = "CLP",
    ) -> BreakEvenResult:
        unknowns: List[str] = []

        # Extraer landed cost de LandedCost si se pasa
        effective_unit_landed = unit_landed_cost
        effective_currency = currency
        if landed_cost is not None:
            effective_unit_landed = landed_cost.unit_landed_cost
            effective_currency = landed_cost.currency
            if landed_cost.status != LandedCostStatus.COMPLETE or effective_unit_landed is None:
                unknowns.extend(landed_cost.unknowns or ["LANDED_COST_INCOMPLETE"])

        # Extraer comisiones de marketplace_fees si se pasa
        effective_mkt_rate = marketplace_fee_rate or Decimal("0")
        effective_mkt_fixed = marketplace_fixed_fee or Decimal("0")
        if marketplace_fees is not None:
            if marketplace_fees.status == CostComponentStatus.KNOWN:
                if marketplace_fees.fee_rate is not None:
                    effective_mkt_rate = marketplace_fees.fee_rate
                if marketplace_fees.fixed_fee_amount is not None:
                    effective_mkt_fixed = marketplace_fees.fixed_fee_amount
            elif marketplace_fees.status == CostComponentStatus.UNKNOWN:
                unknowns.append("MARKETPLACE_FEES_UNKNOWN")

        # Extraer comisiones de payment_fees si se pasa
        effective_pay_rate = payment_fee_rate or Decimal("0")
        effective_pay_fixed = payment_fixed_fee or Decimal("0")
        if payment_fees is not None:
            if payment_fees.status == CostComponentStatus.KNOWN:
                if payment_fees.fee_rate is not None:
                    effective_pay_rate = payment_fees.fee_rate
                if payment_fees.fixed_fee_amount is not None:
                    effective_pay_fixed = payment_fees.fixed_fee_amount
            elif payment_fees.status == CostComponentStatus.UNKNOWN:
                unknowns.append("PAYMENT_FEES_UNKNOWN")

        if effective_unit_landed is None or len(unknowns) > 0:
            if effective_unit_landed is None and "UNIT_LANDED_COST_UNKNOWN" not in unknowns:
                unknowns.append("UNIT_LANDED_COST_UNKNOWN")
            return BreakEvenResult(
                break_even_sale_price=None,
                break_even_units=None,
                target_net_margin_price=None,
                is_computable=False,
                currency=effective_currency,
                formula_used="Break-Even Price = (Unit Landed + Fixed) / (1 - Variable Rates)",
                unknowns=tuple(unknowns),
            )

        fee_rate_total = effective_mkt_rate + effective_pay_rate
        if fee_rate_total >= Decimal("1.0"):
            unknowns.append("FEE_RATES_EXCEED_OR_EQUAL_100_PERCENT")
            return BreakEvenResult(
                break_even_sale_price=None,
                break_even_units=None,
                target_net_margin_price=None,
                is_computable=False,
                currency=effective_currency,
                formula_used="Break-Even Price = (Unit Landed + Fixed) / (1 - Variable Rates)",
                unknowns=tuple(unknowns),
            )

        fixed_per_unit = (
            effective_mkt_fixed
            + effective_pay_fixed
            + (packaging_per_unit or Decimal("0"))
            + (fulfillment_per_unit or Decimal("0"))
            + (other_variable_per_unit or Decimal("0"))
        )

        base_unit_cost = effective_unit_landed + fixed_per_unit
        one_minus_rates = Decimal("1.0") - fee_rate_total

        be_price = round(base_unit_cost / one_minus_rates, 2)

        target_price = None
        if target_net_margin_pct is not None:
            target_margin_rate = target_net_margin_pct / Decimal("100")
            divisor = one_minus_rates - target_margin_rate
            if divisor > Decimal("0"):
                target_price = round(base_unit_cost / divisor, 2)

        return BreakEvenResult(
            break_even_sale_price=be_price,
            break_even_units=1,  # 1 unit break-even price baseline
            target_net_margin_price=target_price,
            is_computable=True,
            currency=effective_currency,
            formula_used="Break-Even Price = (Unit Landed + Fixed) / (1 - Fee Rates)",
            unknowns=(),
        )


class EconomicScenarioAnalyzer:
    """
    Generador y comparador determinista de escenarios económicos (Base, Conservador, Optimista).
    Solo varía parámetros explícitamente definidos (ej. variación de precio de venta +/- 15%).
    No inventa probabilidades ni distribuciones estadísticas no fundamentadas.
    """

    @staticmethod
    def analyze_scenarios(
        base_unit_economics: Optional[UnitEconomics] = None,
        product_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        quantity: int = 1,
        base_sale_price: Optional[SalePrice] = None,
        purchase_cost: Optional[CostComponent] = None,
        shipping_cost: Optional[CostComponent] = None,
        landed_cost: Optional[LandedCost] = None,
        price_drop_pct: Optional[Decimal] = None,
        price_increase_pct: Optional[Decimal] = None,
        conservative_price_factor: Optional[Decimal] = None,
        optimistic_price_factor: Optional[Decimal] = None,
        marketplace_fees: Optional[CostComponent] = None,
        payment_fees: Optional[CostComponent] = None,
        packaging_cost: Optional[CostComponent] = None,
        fulfillment_cost: Optional[CostComponent] = None,
        other_costs: Optional[CostComponent] = None,
    ) -> ScenarioAnalysisResult:
        if base_unit_economics is not None:
            effective_base_econ = base_unit_economics
        else:
            if base_sale_price is None or purchase_cost is None or shipping_cost is None:
                raise ValueError("base_sale_price, purchase_cost, and shipping_cost required if base_unit_economics is None")
            effective_base_econ = UnitEconomicsCalculator.calculate_unit_economics(
                product_id=product_id or "UNKNOWN",
                supplier_id=supplier_id or "UNKNOWN",
                quantity_scenario=quantity,
                sale_price=base_sale_price,
                purchase_cost=purchase_cost,
                shipping_cost=shipping_cost,
                landed_cost=landed_cost,
                marketplace_fees=marketplace_fees,
                payment_fees=payment_fees,
                packaging_cost=packaging_cost,
                fulfillment_cost=fulfillment_cost,
                other_costs=other_costs,
            )

        base_sp = effective_base_econ.sale_price

        # Calcular factores
        if conservative_price_factor is not None:
            cons_factor = conservative_price_factor
        elif price_drop_pct is not None:
            cons_factor = Decimal("1.0") - (price_drop_pct / Decimal("100"))
        else:
            cons_factor = Decimal("0.85")

        if optimistic_price_factor is not None:
            opt_factor = optimistic_price_factor
        elif price_increase_pct is not None:
            opt_factor = Decimal("1.0") + (price_increase_pct / Decimal("100"))
        else:
            opt_factor = Decimal("1.15")

        # Conservador
        cons_price_amount = round(base_sp.amount * cons_factor, 2)
        cons_sale_price = SalePrice(
            amount=cons_price_amount,
            currency=base_sp.currency,
            price_type=SalePriceType.SCENARIO_SALE_PRICE,
            confidence=base_sp.confidence,
            provenance_type=EvidenceProvenanceType.DERIVED,
            source="SCENARIO_CONSERVATIVE",
            details=f"Conservative scenario: {cons_factor * Decimal('100')}% of observed price",
        )
        cons_econ = UnitEconomicsCalculator.calculate_unit_economics(
            product_id=effective_base_econ.product_id,
            supplier_id=effective_base_econ.supplier_id,
            quantity_scenario=effective_base_econ.quantity_scenario,
            sale_price=cons_sale_price,
            purchase_cost=effective_base_econ.purchase_cost,
            shipping_cost=effective_base_econ.shipping_cost,
            import_duties=effective_base_econ.import_duties,
            taxes=effective_base_econ.taxes,
            marketplace_fees=effective_base_econ.marketplace_fees,
            payment_fees=effective_base_econ.payment_fees,
            packaging_cost=effective_base_econ.packaging_cost,
            fulfillment_cost=effective_base_econ.fulfillment_cost,
            other_costs=effective_base_econ.other_costs,
        )

        # Optimista
        opt_price_amount = round(base_sp.amount * opt_factor, 2)
        opt_sale_price = SalePrice(
            amount=opt_price_amount,
            currency=base_sp.currency,
            price_type=SalePriceType.SCENARIO_SALE_PRICE,
            confidence=base_sp.confidence,
            provenance_type=EvidenceProvenanceType.DERIVED,
            source="SCENARIO_OPTIMISTIC",
            details=f"Optimistic scenario: {opt_factor * Decimal('100')}% of observed price",
        )
        opt_econ = UnitEconomicsCalculator.calculate_unit_economics(
            product_id=effective_base_econ.product_id,
            supplier_id=effective_base_econ.supplier_id,
            quantity_scenario=effective_base_econ.quantity_scenario,
            sale_price=opt_sale_price,
            purchase_cost=effective_base_econ.purchase_cost,
            shipping_cost=effective_base_econ.shipping_cost,
            import_duties=effective_base_econ.import_duties,
            taxes=effective_base_econ.taxes,
            marketplace_fees=effective_base_econ.marketplace_fees,
            payment_fees=effective_base_econ.payment_fees,
            packaging_cost=effective_base_econ.packaging_cost,
            fulfillment_cost=effective_base_econ.fulfillment_cost,
            other_costs=effective_base_econ.other_costs,
        )

        summary = (
            f"Base Net Margin: {effective_base_econ.net_margin_pct}%, "
            f"Conservative: {cons_econ.net_margin_pct}%, "
            f"Optimistic: {opt_econ.net_margin_pct}%"
        )

        scenarios_map = {
            EconomicScenarioType.BASE: effective_base_econ,
            EconomicScenarioType.CONSERVATIVE: cons_econ,
            EconomicScenarioType.OPTIMISTIC: opt_econ,
        }

        return ScenarioAnalysisResult(
            base_scenario=effective_base_econ,
            conservative_scenario=cons_econ,
            optimistic_scenario=opt_econ,
            comparison_summary=summary,
            scenarios=scenarios_map,
        )


class EconomicInvestigationDetector:
    """
    Detector determinista de necesidades de investigación económica.
    Identifica qué datos faltan, el impacto de su ausencia y la prioridad para resolverlos.
    """

    @staticmethod
    def detect_investigation_needs(
        unit_economics: Optional[UnitEconomics] = None,
        purchase_cost: Optional[CostComponent] = None,
        shipping_cost: Optional[CostComponent] = None,
        import_duties: Optional[CostComponent] = None,
        taxes: Optional[CostComponent] = None,
        marketplace_fees: Optional[CostComponent] = None,
        payment_fees: Optional[CostComponent] = None,
    ) -> Tuple[EconomicInvestigationNeed, ...]:
        needs: List[EconomicInvestigationNeed] = []

        # Extraer componentes según si se pasa unit_economics o componentes sueltos
        eff_purchase = unit_economics.purchase_cost if unit_economics else purchase_cost
        eff_shipping = unit_economics.shipping_cost if unit_economics else shipping_cost
        eff_duties = unit_economics.import_duties if unit_economics else import_duties
        eff_taxes = unit_economics.taxes if unit_economics else taxes
        eff_marketplace = unit_economics.marketplace_fees if unit_economics else marketplace_fees
        eff_payment = unit_economics.payment_fees if unit_economics else payment_fees

        if eff_shipping and eff_shipping.status == CostComponentStatus.UNKNOWN:
            needs.append(
                EconomicInvestigationNeed(
                    missing_component=CostComponentType.SHIPPING_COST,
                    impact="Landed Cost cannot be completely calculated, preventing deterministic margin computation",
                    priority="HIGH",
                    suggested_action="Request shipping tariff table or confirmed courier quote from supplier/courier API",
                )
            )

        if eff_purchase and eff_purchase.status == CostComponentStatus.UNKNOWN:
            needs.append(
                EconomicInvestigationNeed(
                    missing_component=CostComponentType.PRODUCT_COST,
                    impact="Fundamental wholesale purchase cost is missing",
                    priority="HIGH",
                    suggested_action="Obtain verified CommercialQuote or formal wholesale price list",
                )
            )

        if eff_marketplace and eff_marketplace.status == CostComponentStatus.UNKNOWN:
            needs.append(
                EconomicInvestigationNeed(
                    missing_component=CostComponentType.MARKETPLACE_FEES,
                    impact="Channel commission is unknown, net profit and net margin cannot be calculated",
                    priority="HIGH",
                    suggested_action="Lookup marketplace category fee schedule for target category",
                )
            )

        if eff_payment and eff_payment.status == CostComponentStatus.UNKNOWN:
            needs.append(
                EconomicInvestigationNeed(
                    missing_component=CostComponentType.PAYMENT_FEES,
                    impact="Payment gateway processing fee rate is unknown",
                    priority="MEDIUM",
                    suggested_action="Lookup payment processing rate structure",
                )
            )

        if eff_duties and eff_duties.status == CostComponentStatus.UNKNOWN:
            needs.append(
                EconomicInvestigationNeed(
                    missing_component=CostComponentType.IMPORT_DUTIES,
                    impact="Cross-border tariff rate missing for international supplier",
                    priority="MEDIUM",
                    suggested_action="Check customs tariff code (HS Code) for product category",
                )
            )

        if eff_taxes and eff_taxes.status == CostComponentStatus.UNKNOWN:
            needs.append(
                EconomicInvestigationNeed(
                    missing_component=CostComponentType.TAXES,
                    impact="Tax rate (IVA / Sales Tax) allocation unconfirmed",
                    priority="LOW",
                    suggested_action="Confirm tax regime of buyer and seller",
                )
            )

        return tuple(needs)


class ProfitEngine:
    """
    Motor Económico Unificado de IA Autonomous Commerce (D-01).
    Transforma una oportunidad y su proveedor seleccionado en una evaluación económica determinista.

    Preserva compatibilidad con la interfaz legacy calculate(FinancialData, DecisionRules).
    """

    def calculate(self, data: FinancialData, rules: DecisionRules) -> ProfitAnalysis:
        """
        Interfaz legacy para compatibilidad con código existente y tests de regresión.
        """
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

    def evaluate_opportunity_economics(
        self,
        product_id: str,
        supplier_id: str,
        sale_price: SalePrice,
        purchase_cost: CostComponent,
        shipping_cost: CostComponent,
        quantity_scenarios: Optional[List[int]] = None,
        primary_quantity_scenario: Optional[int] = None,
        price_tiers: Optional[List[PriceTier]] = None,
        import_duties: Optional[CostComponent] = None,
        taxes: Optional[CostComponent] = None,
        marketplace_fees: Optional[CostComponent] = None,
        payment_fees: Optional[CostComponent] = None,
        packaging_cost: Optional[CostComponent] = None,
        fulfillment_cost: Optional[CostComponent] = None,
        other_costs: Optional[CostComponent] = None,
        exchange_rate: Optional[ExchangeRate] = None,
    ) -> EconomicEvaluationResult:
        """
        Evaluación integral determinista D-01:
        - Unit Economics por cada escenario de cantidad (1, MOQ, Volumen)
        - Price tiers integration
        - Break-Even analysis
        - Scenario analysis (Base, Conservador, Optimista)
        - Detección de investigaciones requeridas
        - Profit Trace completo
        """
        scenarios_to_eval = quantity_scenarios or [1]
        if 1 not in scenarios_to_eval:
            scenarios_to_eval.insert(0, 1)

        eval_scenarios_map: Dict[int, UnitEconomics] = {}
        all_steps: List[str] = []
        components_trace_list: List[Dict[str, Any]] = []

        all_steps.append(f"Starting deterministic economic evaluation for product '{product_id}', supplier '{supplier_id}'")
        all_steps.append(f"Observed sale price: {sale_price.amount} {sale_price.currency} (Source: {sale_price.source}, Type: {sale_price.price_type.value})")

        for qty in scenarios_to_eval:
            # Determinar precio de compra según PriceTier si existe
            effective_purchase_cost = purchase_cost
            if price_tiers and purchase_cost.status == CostComponentStatus.KNOWN:
                # Buscar tier correspondiente
                applicable_tier = None
                for pt in sorted(price_tiers, key=lambda t: t.min_quantity, reverse=True):
                    if qty >= pt.min_quantity:
                        if pt.max_quantity is None or qty <= pt.max_quantity:
                            applicable_tier = pt
                            break
                if applicable_tier:
                    effective_purchase_cost = CostComponent(
                        component_type=CostComponentType.PRODUCT_COST,
                        status=CostComponentStatus.KNOWN,
                        amount=applicable_tier.unit_price,
                        currency=applicable_tier.currency,
                        confidence=purchase_cost.confidence,
                        provenance_type=purchase_cost.provenance_type,
                        source=f"PRICE_TIER_MIN_{applicable_tier.min_quantity}",
                        details=f"Price tier for quantity {qty}",
                    )

            unit_econ = UnitEconomicsCalculator.calculate_unit_economics(
                product_id=product_id,
                supplier_id=supplier_id,
                quantity_scenario=qty,
                sale_price=sale_price,
                purchase_cost=effective_purchase_cost,
                shipping_cost=shipping_cost,
                import_duties=import_duties,
                taxes=taxes,
                marketplace_fees=marketplace_fees,
                payment_fees=payment_fees,
                packaging_cost=packaging_cost,
                fulfillment_cost=fulfillment_cost,
                other_costs=other_costs,
                exchange_rate=exchange_rate,
            )
            eval_scenarios_map[qty] = unit_econ
            all_steps.append(f"Quantity Scenario QTY={qty} -> Status: {unit_econ.status.value}, Gross Margin: {unit_econ.gross_margin_pct}%, Net Margin: {unit_econ.net_margin_pct}%")

        if primary_quantity_scenario and primary_quantity_scenario in eval_scenarios_map:
            primary_econ = eval_scenarios_map[primary_quantity_scenario]
        else:
            primary_econ = eval_scenarios_map[scenarios_to_eval[0]]

        # Break-Even
        be_res = BreakEvenCalculator.calculate_break_even_price(
            unit_landed_cost=primary_econ.landed_cost.unit_landed_cost,
            marketplace_fee_rate=primary_econ.marketplace_fees.fee_rate,
            marketplace_fixed_fee=primary_econ.marketplace_fees.fixed_fee_amount,
            payment_fee_rate=primary_econ.payment_fees.fee_rate,
            payment_fixed_fee=primary_econ.payment_fees.fixed_fee_amount,
            packaging_per_unit=primary_econ.packaging_cost.amount,
            fulfillment_per_unit=primary_econ.fulfillment_cost.amount,
            other_variable_per_unit=primary_econ.other_costs.amount,
            currency=primary_econ.currency,
        )
        all_steps.append(f"Break-Even Price: {be_res.break_even_sale_price} {be_res.currency} (Computable: {be_res.is_computable})")

        # Scenario Analysis (Base / Conservative / Optimistic)
        scenarios_res: Optional[ScenarioAnalysisResult] = None
        if primary_econ.status in (ProfitStatus.PROFIT_COMPLETE, ProfitStatus.PROFIT_PARTIAL):
            scenarios_res = EconomicScenarioAnalyzer.analyze_scenarios(primary_econ)
            all_steps.append(f"Scenario Analysis generated: {scenarios_res.comparison_summary}")

        # Investigation Needs
        investigation_needs = EconomicInvestigationDetector.detect_investigation_needs(primary_econ)
        for need in investigation_needs:
            all_steps.append(f"Investigation Needed: {need.missing_component.value} (Priority: {need.priority}) -> {need.suggested_action}")

        # Trace
        components_trace_list.append({
            "sale_price": str(sale_price.amount),
            "purchase_cost": str(purchase_cost.amount) if purchase_cost.amount is not None else "UNKNOWN",
            "shipping_cost": str(shipping_cost.amount) if shipping_cost.amount is not None else "UNKNOWN",
            "marketplace_fees": str(primary_econ.marketplace_fees.amount) if primary_econ.marketplace_fees.amount is not None else "UNKNOWN",
            "status": primary_econ.status.value,
        })

        profit_trace = ProfitTrace(
            product_id=product_id,
            supplier_id=supplier_id,
            steps=tuple(all_steps),
            components_trace=tuple(components_trace_list),
        )

        return EconomicEvaluationResult(
            product_id=product_id,
            supplier_id=supplier_id,
            primary_unit_economics=primary_econ,
            quantity_scenarios=eval_scenarios_map,
            break_even=be_res,
            scenarios=scenarios_res,
            investigation_needs=investigation_needs,
            overall_confidence=primary_econ.confidence,
            overall_status=primary_econ.status,
            profit_trace=profit_trace,
        )
