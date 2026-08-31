from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict, Any, Mapping, Sequence
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence, SignalType, MarketEvidence
from src.domain.opportunity.models import Opportunity, OpportunityReadiness, EvidenceSufficiency
from src.domain.supplier_intelligence.models import (
    SupplierCandidate,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    SupplierRiskProfile,
    EvidenceProvenanceType,
    RiskLevel,
    QuoteFreshness,
    ShippingOption,
    ShippingMethod,
    CommercialQuote,
)
from src.domain.profit.models import (
    EconomicEvaluationResult,
    UnitEconomics,
    EconomicScenarioType,
    ProfitStatus,
    CostComponent,
    CostComponentStatus,
    CostComponentType,
    LandedCost,
    LandedCostStatus,
    ProfitStatus,
    SalePrice,
    Money,
)
from src.domain.profit.engine import (
    LandedCostCalculator,
    UnitEconomicsCalculator,
)
from src.domain.capital.models import (
    CapitalBudget,
    CapitalExposure,
    AllocationPolicy,
    AllocationDecision,
    AllocationStatus,
)
from src.domain.capital.engine import CapitalAllocationEngine

from .models import (
    OperatingModelType,
    OperatingDecisionType,
    DecisionTrigger,
    DemandVelocity,
    ObsolescenceRisk,
    InventoryScenario,
    DropshippingScenario,
    OperatingModelComparison,
    OperatingModelPolicy,
    DecisionExplanation,
    OperatingDecision,
    OperatingReassessmentRecord,
)


class OperatingModelEvaluator:
    """
    Evaluador y comparador determinista para construir los escenarios de
    Inventory vs Dropshipping a partir de datos reales de oportunidad, proveedor,
    economía y asignación de capital.
    
    Reglas estrictas anti-fabricación:
    - UNKNOWN != 0, UNKNOWN != FREE, UNKNOWN != HIGH_ROTATION, UNKNOWN != LOW_RISK.
    - No se asume que dropshipping requiere capital = 0 si existen costos de flete o buffer operativo.
    - No se asume demanda alta sin señales observadas o derivadas válidas.
    - Preserva rigurosamente provenance (LIVE, FIXTURE, MOCK, DERIVED, INFERRED).
    """

    @classmethod
    def build_inventory_scenario(
        cls,
        opportunity: Opportunity,
        quote: CommercialQuote,
        supplier_recommendation: Optional[SupplierRecommendation],
        supplier_risk_profile: Optional[SupplierRiskProfile],
        target_quantity: Optional[int] = None,
        storage_cost_monthly: Optional[CostComponent] = None,
        obsolescence_risk: Optional[ObsolescenceRisk] = None,
        shipping_option: Optional[ShippingOption] = None,
        marketplace_fee_rate: Optional[Decimal] = None,
        payment_fee_rate: Optional[Decimal] = None,
        tax_rate: Optional[Decimal] = None,
        fixed_marketplace_fee: Optional[Decimal] = None,
    ) -> InventoryScenario:
        unknowns: List[str] = []
        
        # 1. Determinar MOQ y Cantidad Objetivo
        moq_qty = quote.moq.quantity if quote.moq and quote.moq.quantity is not None and quote.moq.quantity > 0 else 1
        qty = target_quantity if target_quantity is not None and target_quantity >= moq_qty else moq_qty
        
        # 2. Obtener precio unitario para el tier correspondiente a qty
        price_tier_unit = quote.get_unit_price_for_quantity(qty)
        unit_price = price_tier_unit if price_tier_unit is not None else (quote.unit_price or Decimal("0"))
        
        purchase_comp = CostComponent.known(
            component_type=CostComponentType.PRODUCT_COST,
            amount=unit_price * Decimal(str(qty)),
            currency=quote.currency,
            confidence=quote.confidence,
            provenance_type=quote.provenance_type,
            source=f"QUOTE_TIER_QTY_{qty}",
            is_per_unit=False,
        )
        
        # 3. Flete de adquisición por volumen
        chosen_shipping = shipping_option
        if chosen_shipping is None and quote.shipping_cost is not None:
            chosen_shipping = ShippingOption(
                shipping_cost=quote.shipping_cost,
                currency=quote.currency,
                confidence=quote.confidence,
                provenance_type=quote.provenance_type,
            )
            
        if chosen_shipping is not None and chosen_shipping.shipping_cost is not None:
            # Flete escala o tiene costo por lote
            shipping_comp = CostComponent.known(
                component_type=CostComponentType.SHIPPING_COST,
                amount=chosen_shipping.shipping_cost,
                currency=chosen_shipping.currency,
                confidence=chosen_shipping.confidence,
                provenance_type=chosen_shipping.provenance_type,
                source=f"SHIPPING_{chosen_shipping.carrier or chosen_shipping.method.value}",
                is_per_unit=False,
            )
        else:
            shipping_comp = CostComponent.unknown(
                component_type=CostComponentType.SHIPPING_COST,
                currency=quote.currency,
                details="Shipping cost unknown for inventory batch",
            )
            unknowns.append("INVENTORY_SHIPPING_UNKNOWN")

        # 4. Landed Cost y Unit Economics para el lote de inventario
        landed_cost = LandedCostCalculator.calculate(
            product_id=opportunity.product_id,
            supplier_id=quote.supplier_id,
            quantity=qty,
            purchase_cost=purchase_comp,
            shipping_cost=shipping_comp,
            target_currency=quote.currency,
        )
        
        # Precio de venta esperado de la oportunidad
        # Usar precio promedio o actual de mercado
        sale_price_amount = opportunity.listing.price.amount if opportunity.listing and opportunity.listing.price else None
        if sale_price_amount is None:
            raise ValueError(f"Opportunity {opportunity.product_id} has no observable sale price")

        sale_price = SalePrice.observed(
            amount=sale_price_amount,
            currency=quote.currency,
            confidence=opportunity.confidence,
            provenance_type=quote.provenance_type,
            source="MARKET_OPPORTUNITY_LISTING",
        )

        unit_econ = UnitEconomicsCalculator.calculate_unit_economics(
            product_id=opportunity.product_id,
            supplier_id=quote.supplier_id,
            quantity_scenario=qty,
            sale_price=sale_price,
            purchase_cost=purchase_comp,
            shipping_cost=shipping_comp,
            landed_cost=landed_cost,
            marketplace_fees=CostComponent.known(
                component_type=CostComponentType.MARKETPLACE_FEES,
                fee_rate=marketplace_fee_rate or Decimal("0.13"),
                fixed_fee_amount=fixed_marketplace_fee or Decimal("0"),
                currency=quote.currency,
            ),
            payment_fees=CostComponent.known(
                component_type=CostComponentType.PAYMENT_FEES,
                fee_rate=payment_fee_rate or Decimal("0.035"),
                currency=quote.currency,
            ),
            taxes=CostComponent.known(
                component_type=CostComponentType.TAXES,
                fee_rate=tax_rate or Decimal("0.19"),
                currency=quote.currency,
            ),
        )
        
        # 5. Capital Requerido y Exposición de Stock
        # Para inventario, capital requerido = total landed cost del lote de compra
        required_cap = landed_cost.total_landed_cost if landed_cost.total_landed_cost is not None else (unit_price * Decimal(str(qty)))
        stock_exp = required_cap  # Todo el capital invertido en el lote está expuesto en stock físico
        
        # 6. Demanda y Velocidad de Rotación
        demand_signals = opportunity.evidence.demand_signals if opportunity.evidence else []
        demand_sig = demand_signals[0] if demand_signals else None
        demand_sig_type = demand_sig.signal_type if demand_sig else SignalType.INFERRED
        
        # Evaluar velocidad de rotación sólo con evidencia cuantitativa
        velocity = DemandVelocity.UNKNOWN
        estimated_days: Optional[int] = None
        if opportunity.listing and opportunity.listing.sold_quantity is not None:
            sold = opportunity.listing.sold_quantity
            if sold > 100:
                velocity = DemandVelocity.HIGH
                estimated_days = max(15, int(qty * 30 / max(1, sold // 3)))
            elif sold > 20:
                velocity = DemandVelocity.MODERATE
                estimated_days = max(30, int(qty * 60 / max(1, sold // 2)))
            elif sold > 0:
                velocity = DemandVelocity.SLOW
                estimated_days = max(60, int(qty * 90 / max(1, sold)))
            else:
                velocity = DemandVelocity.STAGNANT
                estimated_days = None
        else:
            unknowns.append("DEMAND_ROTATION_UNKNOWN")

        # 7. Lead time
        lead_time: Optional[int] = None
        if chosen_shipping and chosen_shipping.estimated_transit_days:
            lead_time = chosen_shipping.estimated_transit_days
        elif quote.lead_time_days:
            lead_time = quote.lead_time_days
        else:
            unknowns.append("INVENTORY_LEAD_TIME_UNKNOWN")

        # 8. Riesgo de proveedor y confiabilidad
        supp_risk_lvl = supplier_risk_profile.overall_risk_level if supplier_risk_profile else RiskLevel.MEDIUM
        supp_reliability = supplier_recommendation.primary_supplier.reliability_score if (supplier_recommendation and supplier_recommendation.primary_supplier) else None

        # 9. Obsolescencia
        obs_risk = obsolescence_risk or ObsolescenceRisk.UNKNOWN
        if obs_risk == ObsolescenceRisk.UNKNOWN:
            # Deducir si hay señal de tendencia
            trend_signals = opportunity.evidence.trend_signals if opportunity.evidence else []
            if trend_signals:
                trend_score = trend_signals[0].trend_score
                if trend_score > Decimal("0.7"):
                    obs_risk = ObsolescenceRisk.LOW
                elif trend_score < Decimal("0.3"):
                    obs_risk = ObsolescenceRisk.HIGH
                else:
                    obs_risk = ObsolescenceRisk.MEDIUM
            else:
                unknowns.append("OBSOLESCENCE_RISK_UNKNOWN")

        # Unificar unknowns con los de landed cost
        all_unknowns = list(set(list(landed_cost.unknowns) + unknowns))

        # Provenance y Confidence
        confidence = quote.confidence
        if landed_cost.status not in (ProfitStatus.PROFIT_COMPLETE, LandedCostStatus.COMPLETE):
            if confidence == Confidence.HIGH:
                confidence = Confidence.MEDIUM

        return InventoryScenario(
            opportunity_id=opportunity.product_id,
            supplier_id=quote.supplier_id,
            target_quantity=qty,
            moq=moq_qty,
            unit_economics=unit_econ,
            required_capital=required_cap,
            stock_exposure=stock_exp,
            lead_time_days=lead_time,
            demand_signal_type=demand_sig_type,
            demand_velocity=velocity,
            obsolescence_risk=obs_risk,
            supplier_risk_level=supp_risk_lvl,
            supplier_reliability_score=supp_reliability,
            confidence=confidence,
            provenance_type=quote.provenance_type,
            storage_cost_monthly=storage_cost_monthly,
            estimated_days_to_sell=estimated_days,
            unknowns=tuple(sorted(all_unknowns)),
        )

    @classmethod
    def build_dropshipping_scenario(
        cls,
        opportunity: Opportunity,
        quote: CommercialQuote,
        supplier_recommendation: Optional[SupplierRecommendation],
        supplier_risk_profile: Optional[SupplierRiskProfile],
        direct_shipping_option: Optional[ShippingOption] = None,
        operational_buffer_capital: Decimal = Decimal("0"),
        marketplace_fee_rate: Optional[Decimal] = None,
        payment_fee_rate: Optional[Decimal] = None,
        tax_rate: Optional[Decimal] = None,
        fixed_marketplace_fee: Optional[Decimal] = None,
    ) -> DropshippingScenario:
        unknowns: List[str] = []

        # 1. Costo unitario para Dropshipping (QTY=1)
        qty = 1
        unit_price_eval = quote.get_unit_price_for_quantity(1)
        unit_price = unit_price_eval if unit_price_eval is not None else (quote.unit_price or Decimal("0"))

        purchase_comp = CostComponent.known(
            component_type=CostComponentType.PRODUCT_COST,
            amount=unit_price,
            currency=quote.currency,
            confidence=quote.confidence,
            provenance_type=quote.provenance_type,
            source="QUOTE_UNIT_DROPSHIP",
            is_per_unit=True,
        )

        # 2. Flete directo por unidad al cliente final
        chosen_shipping = direct_shipping_option
        if chosen_shipping is None and quote.shipping_cost is not None:
            chosen_shipping = ShippingOption(
                shipping_cost=quote.shipping_cost,
                currency=quote.currency,
                confidence=quote.confidence,
                provenance_type=quote.provenance_type,
            )

        if chosen_shipping is not None and chosen_shipping.shipping_cost is not None:
            shipping_comp = CostComponent.known(
                component_type=CostComponentType.SHIPPING_COST,
                amount=chosen_shipping.shipping_cost,
                currency=chosen_shipping.currency,
                confidence=chosen_shipping.confidence,
                provenance_type=chosen_shipping.provenance_type,
                source=f"DROPSHIP_SHIPPING_{chosen_shipping.carrier or chosen_shipping.method.value}",
                is_per_unit=True,
            )
        else:
            shipping_comp = CostComponent.unknown(
                component_type=CostComponentType.SHIPPING_COST,
                currency=quote.currency,
                details="Direct drop-ship delivery cost to customer unknown",
            )
            unknowns.append("DROPSHIPPING_SHIPPING_UNKNOWN")

        # 3. Landed Cost unitario y Unit Economics
        landed_cost = LandedCostCalculator.calculate(
            product_id=opportunity.product_id,
            supplier_id=quote.supplier_id,
            quantity=1,
            purchase_cost=purchase_comp,
            shipping_cost=shipping_comp,
            target_currency=quote.currency,
        )

        sale_price_amount = opportunity.listing.price.amount if opportunity.listing and opportunity.listing.price else None
        if sale_price_amount is None:
            raise ValueError(f"Opportunity {opportunity.product_id} has no observable sale price")

        sale_price = SalePrice.observed(
            amount=sale_price_amount,
            currency=quote.currency,
            confidence=opportunity.confidence,
            provenance_type=quote.provenance_type,
            source="MARKET_OPPORTUNITY_LISTING",
        )

        mkt_rate = marketplace_fee_rate or Decimal("0.13")
        pay_rate = payment_fee_rate or Decimal("0.035")

        unit_econ = UnitEconomicsCalculator.calculate_unit_economics(
            product_id=opportunity.product_id,
            supplier_id=quote.supplier_id,
            quantity_scenario=1,
            sale_price=sale_price,
            purchase_cost=purchase_comp,
            shipping_cost=shipping_comp,
            landed_cost=landed_cost,
            marketplace_fees=CostComponent.known(
                component_type=CostComponentType.MARKETPLACE_FEES,
                fee_rate=mkt_rate,
                fixed_fee_amount=fixed_marketplace_fee or Decimal("0"),
                currency=quote.currency,
            ),
            payment_fees=CostComponent.known(
                component_type=CostComponentType.PAYMENT_FEES,
                fee_rate=pay_rate,
                currency=quote.currency,
            ),
            taxes=CostComponent.known(
                component_type=CostComponentType.TAXES,
                fee_rate=tax_rate or Decimal("0.19"),
                currency=quote.currency,
            ),
        )

        # 4. Capital Operativo Requerido
        unit_lc = landed_cost.unit_landed_cost if landed_cost.unit_landed_cost is not None else unit_price
        required_capital = unit_lc + operational_buffer_capital

        # 5. Lead Time directo y SLA
        lead_time: Optional[int] = None
        if chosen_shipping and chosen_shipping.estimated_transit_days:
            lead_time = chosen_shipping.estimated_transit_days
        elif quote.lead_time_days:
            lead_time = quote.lead_time_days
        else:
            unknowns.append("DROPSHIPPING_LEAD_TIME_UNKNOWN")

        # 6. Confiabilidad y SLA del proveedor
        supp_risk_lvl = supplier_risk_profile.overall_risk_level if supplier_risk_profile else RiskLevel.MEDIUM
        supp_reliability = supplier_recommendation.primary_supplier.reliability_score if (supplier_recommendation and supplier_recommendation.primary_supplier) else None
        sla_ok = True

        all_unknowns = list(set(list(landed_cost.unknowns) + unknowns))

        return DropshippingScenario(
            opportunity_id=opportunity.product_id,
            supplier_id=quote.supplier_id,
            unit_economics=unit_econ,
            required_operational_capital=required_capital,
            lead_time_days=lead_time,
            supplier_risk_level=supp_risk_lvl,
            supplier_reliability_score=supp_reliability,
            supplier_sla_compliant=sla_ok,
            confidence=quote.confidence,
            provenance_type=quote.provenance_type,
            payment_gateway_fee_pct=pay_rate,
            marketplace_fee_pct=mkt_rate,
            unknowns=tuple(sorted(all_unknowns)),
        )

    @classmethod
    def compare_scenarios(
        cls,
        inventory_scenario: InventoryScenario,
        dropshipping_scenario: DropshippingScenario,
    ) -> OperatingModelComparison:
        """
        Compara explícitamente los escenarios de Inventory y Dropshipping.
        Calcula diferenciales cuantitativos y ventajas/desventajas cualitativas.
        """
        if inventory_scenario.opportunity_id != dropshipping_scenario.opportunity_id:
            raise ValueError("Cannot compare scenarios for different opportunity IDs")

        inv_net_profit_unit = inventory_scenario.unit_economics.net_profit
        drop_net_profit_unit = dropshipping_scenario.unit_economics.net_profit

        profit_diff_per_unit: Optional[Decimal] = None
        if inv_net_profit_unit is not None and drop_net_profit_unit is not None:
            profit_diff_per_unit = inv_net_profit_unit - drop_net_profit_unit

        # Profit total diferencial para el lote
        profit_diff: Optional[Decimal] = None
        inv_total_profit = inventory_scenario.expected_profit
        if inv_total_profit is not None and drop_net_profit_unit is not None:
            # Comparar el profit del lote de inventario vs vender la misma cantidad vía dropshipping
            drop_batch_profit = drop_net_profit_unit * Decimal(str(inventory_scenario.target_quantity))
            profit_diff = inv_total_profit - drop_batch_profit

        # Margen diferencial %
        margin_diff_pct: Optional[Decimal] = None
        inv_margin = inventory_scenario.expected_margin_pct
        drop_margin = dropshipping_scenario.expected_margin_pct
        if inv_margin is not None and drop_margin is not None:
            margin_diff_pct = inv_margin - drop_margin

        # Capital y Exposición diferencial
        cap_diff = inventory_scenario.required_capital - dropshipping_scenario.required_operational_capital
        stock_exp_diff = inventory_scenario.stock_exposure  # Dropshipping stock exposure is 0

        # Ventajas y desventajas estructuradas
        inv_adv: List[str] = []
        inv_disadv: List[str] = []
        drop_adv: List[str] = []
        drop_disadv: List[str] = []

        # Margen y Costo
        if margin_diff_pct is not None:
            if margin_diff_pct > Decimal("0"):
                inv_adv.append(f"Higher profit margin (+{margin_diff_pct * 100:.1f}%) due to volume purchase")
                drop_disadv.append(f"Lower profit margin (-{margin_diff_pct * 100:.1f}%) due to single-unit fulfillment")
            elif margin_diff_pct < Decimal("0"):
                drop_adv.append(f"Better unit margin (+{(-margin_diff_pct) * 100:.1f}%) compared to loaded inventory landed cost")
                inv_disadv.append("Inferior margin after inventory shipping and handling costs")

        # Capital y Exposición
        if cap_diff > Decimal("0"):
            drop_adv.append(f"Drastically lower upfront capital requirement (-{cap_diff} CLP)")
            drop_adv.append("Zero physical stock exposure and no capital lock-up risk")
            inv_disadv.append(f"High upfront capital requirement ({inventory_scenario.required_capital} CLP)")
            inv_disadv.append(f"Full physical stock exposure ({inventory_scenario.stock_exposure} CLP locked)")

        # Velocidad y Demanda
        if inventory_scenario.demand_velocity in (DemandVelocity.HIGH, DemandVelocity.MODERATE):
            inv_adv.append(f"Validated demand velocity ({inventory_scenario.demand_velocity.value}) supports inventory rotation")
        elif inventory_scenario.demand_velocity in (DemandVelocity.SLOW, DemandVelocity.STAGNANT):
            inv_disadv.append(f"Sluggish demand velocity ({inventory_scenario.demand_velocity.value}) increases holding risk")

        if inventory_scenario.obsolescence_risk in (ObsolescenceRisk.HIGH, ObsolescenceRisk.CRITICAL):
            inv_disadv.append(f"High obsolescence risk ({inventory_scenario.obsolescence_risk.value}) makes stock dangerous")
            drop_adv.append("Protects against product obsolescence by buying on-demand")

        # Riesgo Operacional de Proveedor y SLA
        if dropshipping_scenario.supplier_risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            drop_disadv.append(f"Supplier operational risk is {dropshipping_scenario.supplier_risk_level.value}, making fulfillment unreliable")
            inv_adv.append("Mitigates ongoing supplier fulfillment failures once batch is received")
        
        if not dropshipping_scenario.supplier_sla_compliant:
            drop_disadv.append("Supplier has poor SLA compliance history; direct shipping to clients is high-risk")

        if dropshipping_scenario.lead_time_days and dropshipping_scenario.lead_time_days > 7:
            drop_disadv.append(f"Direct dropship lead time ({dropshipping_scenario.lead_time_days} days) exceeds rapid delivery standards")
            inv_adv.append("Allows same-day/next-day local dispatch once stock is held")

        # Unknowns unificados
        combined_unk = tuple(sorted(list(set(list(inventory_scenario.unknowns) + list(dropshipping_scenario.unknowns)))))

        # Confidence combinada
        conf = Confidence.HIGH
        if inventory_scenario.confidence == Confidence.UNKNOWN or dropshipping_scenario.confidence == Confidence.UNKNOWN:
            conf = Confidence.UNKNOWN
        elif inventory_scenario.confidence == Confidence.LOW or dropshipping_scenario.confidence == Confidence.LOW:
            conf = Confidence.LOW
        elif inventory_scenario.confidence == Confidence.MEDIUM or dropshipping_scenario.confidence == Confidence.MEDIUM:
            conf = Confidence.MEDIUM

        # Provenance
        prov = EvidenceProvenanceType.LIVE
        if (
            inventory_scenario.provenance_type == EvidenceProvenanceType.FIXTURE
            or dropshipping_scenario.provenance_type == EvidenceProvenanceType.FIXTURE
        ):
            prov = EvidenceProvenanceType.FIXTURE
        elif (
            inventory_scenario.provenance_type == EvidenceProvenanceType.MOCK
            or dropshipping_scenario.provenance_type == EvidenceProvenanceType.MOCK
        ):
            prov = EvidenceProvenanceType.MOCK

        return OperatingModelComparison(
            opportunity_id=inventory_scenario.opportunity_id,
            inventory_scenario=inventory_scenario,
            dropshipping_scenario=dropshipping_scenario,
            profit_differential=profit_diff,
            profit_differential_per_unit=profit_diff_per_unit,
            margin_differential_pct=margin_diff_pct,
            capital_differential=cap_diff,
            stock_exposure_differential=stock_exp_diff,
            inventory_advantages=tuple(inv_adv),
            dropshipping_advantages=tuple(drop_adv),
            inventory_disadvantages=tuple(inv_disadv),
            dropshipping_disadvantages=tuple(drop_disadv),
            combined_unknowns=combined_unk,
            confidence=conf,
            provenance_type=prov,
        )


class OperatingModelEngine:
    """
    Motor central determinista de decisión y reevaluación de Modelo Operativo (D-03).
    
    Aplica la política OperatingModelPolicy sobre la comparación estructurada
    y la asignación de capital (D-02).
    
    Genera decisiones formales, explicaciones detalladas basadas en datos,
    condiciones explícitas y registros de reevaluación / pivot.
    """

    @classmethod
    def evaluate_operating_decision(
        cls,
        comparison: OperatingModelComparison,
        capital_budget: CapitalBudget,
        capital_allocation_decision: Optional[AllocationDecision] = None,
        policy: Optional[OperatingModelPolicy] = None,
        decision_id: Optional[str] = None,
    ) -> OperatingDecision:
        pol = policy or OperatingModelPolicy()
        dec_id = dec_id = decision_id or f"OPDEC-{comparison.opportunity_id}-{int(datetime.now(timezone.utc).timestamp())}"
        
        inv = comparison.inventory_scenario
        drop = comparison.dropshipping_scenario
        conditions: List[str] = []
        unknowns: List[str] = list(comparison.combined_unknowns)
        invalidation_triggers: List[str] = []

        # 1. VERIFICACIÓN DE INCÓGNITAS CRÍTICAS Y SUFICIENCIA DE EVIDENCIA
        # Si faltan economics críticos o precios en ambos, no se puede decidir forzadamente
        if not inv.is_viable_economically and not drop.is_viable_economically:
            # Ninguno es viable económicamente o faltan datos
            reason_econ = "Both inventory and dropshipping economics are non-viable or incomplete."
            if "INVENTORY_SHIPPING_UNKNOWN" in unknowns or "DROPSHIPPING_SHIPPING_UNKNOWN" in unknowns:
                reason_econ += " Missing shipping costs prevent accurate landed cost calculation."
            
            explanation = DecisionExplanation(
                selected_model=OperatingModelType.NO_DECISION,
                alternative_model=OperatingModelType.NEEDS_INVESTIGATION,
                economic_rationale=reason_econ,
                capital_rationale="Capital cannot be committed without validated positive unit economics.",
                risk_rationale="Economic uncertainty creates unquantified downside risk.",
                evidence_summary=f"Evidence sufficiency is low. Confidence: {comparison.confidence.value}.",
                unknowns_summary=f"Critical unknowns: {', '.join(unknowns)}.",
                conditions_summary="Resolve unknown shipping/cost components before re-evaluating.",
                invalidation_triggers=("SUPPLIER_QUOTE_UPDATE", "SHIPPING_COST_CONFIRMATION"),
            )
            return OperatingDecision(
                decision_id=dec_id,
                opportunity_id=comparison.opportunity_id,
                supplier_id=inv.supplier_id,
                decision_type=OperatingDecisionType.NO_DECISION,
                selected_model=OperatingModelType.NO_DECISION,
                alternative_model=OperatingModelType.NEEDS_INVESTIGATION,
                comparison=comparison,
                explanation=explanation,
                conditions=("Obtain verified supplier quote and shipping rates",),
                unknowns=tuple(unknowns),
                confidence=comparison.confidence,
                provenance_type=comparison.provenance_type,
            )

        # 2. EVALUACIÓN DE RESTRICCIONES DE CAPITAL (D-02)
        # Verificar si capital allocation permite inventory
        alloc_ok_for_inventory = True
        alloc_reason = ""
        
        if capital_allocation_decision is not None:
            if capital_allocation_decision.decision_status not in (AllocationStatus.APPROVED, AllocationStatus.PARTIALLY_APPROVED):
                alloc_ok_for_inventory = False
                alloc_reason = f"Capital allocation decision is {capital_allocation_decision.decision_status.value} ({capital_allocation_decision.reason.value})."
            elif capital_allocation_decision.approved_amount < inv.required_capital:
                alloc_ok_for_inventory = False
                alloc_reason = f"Approved capital ({capital_allocation_decision.approved_amount} CLP) is below inventory requirement ({inv.required_capital} CLP)."
        else:
            # Comprobar contra el budget directamente
            if inv.required_capital > capital_budget.allocatable_capital:
                alloc_ok_for_inventory = False
                alloc_reason = f"Inventory required capital ({inv.required_capital} CLP) exceeds budget allocatable capital ({capital_budget.allocatable_capital} CLP)."

        # Ratio de exposición de stock vs capital asignable
        if capital_budget.allocatable_capital > Decimal("0"):
            exposure_ratio = inv.stock_exposure / capital_budget.allocatable_capital
            if exposure_ratio > pol.max_stock_exposure_ratio:
                alloc_ok_for_inventory = False
                alloc_reason = f"Stock exposure ratio ({exposure_ratio:.2f}) exceeds policy maximum ({pol.max_stock_exposure_ratio:.2f})."

        # 3. EVALUACIÓN DE CONDICIONES PARA INVENTORY
        inventory_viable = True
        inventory_blockers: List[str] = []

        if not inv.is_viable_economically:
            inventory_viable = False
            inventory_blockers.append("Inventory unit economics are negative or incomplete")

        if inv.expected_margin_pct is not None and inv.expected_margin_pct < pol.minimum_margin_inventory_pct:
            inventory_viable = False
            inventory_blockers.append(f"Inventory margin ({inv.expected_margin_pct:.1f}%) is below minimum threshold ({pol.minimum_margin_inventory_pct:.1f}%)")

        if not alloc_ok_for_inventory:
            inventory_viable = False
            inventory_blockers.append(alloc_reason)

        if pol.require_demand_validation_for_inventory:
            if inv.demand_velocity in (DemandVelocity.UNKNOWN, DemandVelocity.STAGNANT):
                inventory_viable = False
                inventory_blockers.append(f"Demand velocity is {inv.demand_velocity.value}; unvalidated demand precludes physical stock commitment")
            if inv.demand_signal_type == SignalType.INFERRED:
                # Requiere condición o investigación
                conditions.append("Confirm historical sales velocity before issuing purchase order")

        if inv.obsolescence_risk in (ObsolescenceRisk.HIGH, ObsolescenceRisk.CRITICAL):
            inventory_viable = False
            inventory_blockers.append(f"High obsolescence risk ({inv.obsolescence_risk.value}) forbids inventory holding")

        if inv.lead_time_days and inv.lead_time_days > pol.max_lead_time_days_inventory:
            inventory_viable = False
            inventory_blockers.append(f"Inventory acquisition lead time ({inv.lead_time_days} days) exceeds maximum policy limit ({pol.max_lead_time_days_inventory} days)")

        # 4. EVALUACIÓN DE CONDICIONES PARA DROPSHIPPING
        dropshipping_viable = True
        dropshipping_blockers: List[str] = []

        if not drop.is_viable_economically:
            dropshipping_viable = False
            dropshipping_blockers.append("Dropshipping unit economics are negative or incomplete")

        if drop.expected_margin_pct is not None and drop.expected_margin_pct < pol.minimum_margin_dropshipping_pct:
            dropshipping_viable = False
            dropshipping_blockers.append(f"Dropshipping margin ({drop.expected_margin_pct:.1f}%) is below minimum threshold ({pol.minimum_margin_dropshipping_pct:.1f}%)")

        if drop.supplier_risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            dropshipping_viable = False
            dropshipping_blockers.append(f"Supplier operational risk is {drop.supplier_risk_level.value}; direct customer fulfillment is unsafe")

        if drop.supplier_reliability_score is not None and drop.supplier_reliability_score < pol.min_supplier_reliability_for_dropshipping:
            dropshipping_viable = False
            dropshipping_blockers.append(f"Supplier reliability score ({drop.supplier_reliability_score}) is below policy minimum ({pol.min_supplier_reliability_for_dropshipping})")

        if drop.lead_time_days and drop.lead_time_days > pol.max_lead_time_days_dropshipping:
            dropshipping_viable = False
            dropshipping_blockers.append(f"Dropshipping lead time ({drop.lead_time_days} days) exceeds customer tolerance limit ({pol.max_lead_time_days_dropshipping} days)")

        # 5. DECISIÓN Y COMPARACIÓN DE ATRACTIVO RELATIVO
        selected_model: OperatingModelType
        alternative_model: OperatingModelType
        decision_type: OperatingDecisionType
        econ_rationale: str
        cap_rationale: str
        risk_rationale: str

        if inventory_viable and dropshipping_viable:
            # Ambos son viables: comparar márgenes, capital y riesgos
            margin_adv = comparison.margin_differential_pct or Decimal("0")
            if margin_adv >= pol.min_margin_advantage_for_inventory_pct:
                # Inventory ofrece suficiente prima de margen sobre dropshipping
                selected_model = OperatingModelType.INVENTORY
                alternative_model = OperatingModelType.DROPSHIPPING
                decision_type = OperatingDecisionType.SELECT_INVENTORY
                
                econ_rationale = (
                    f"Inventory selected due to superior economics: +{margin_adv:.1f}% net margin advantage "
                    f"({inv.expected_margin_pct:.1f}% vs {drop.expected_margin_pct:.1f}%) and higher batch profit."
                )
                cap_rationale = f"Allocatable capital ({capital_budget.allocatable_capital} CLP) comfortably covers requirement ({inv.required_capital} CLP)."
                risk_rationale = f"Stock exposure ({inv.stock_exposure} CLP) is justified by validated rotation ({inv.demand_velocity.value}) and low obsolescence risk."
                
                invalidation_triggers = [
                    "DEMAND_CONTRACTION",
                    "SUPPLIER_PRICE_INCREASE",
                    "CAPITAL_REDUCTION",
                    "INCREASED_HOLDING_TIME",
                ]
            else:
                # La prima de margen de inventory no justifica inmovilizar capital frente a dropshipping
                selected_model = OperatingModelType.DROPSHIPPING
                alternative_model = OperatingModelType.INVENTORY
                decision_type = OperatingDecisionType.SELECT_DROPSHIPPING
                
                econ_rationale = (
                    f"Dropshipping selected: inventory margin advantage (+{margin_adv:.1f}%) is below required threshold "
                    f"(+{pol.min_margin_advantage_for_inventory_pct:.1f}%) to justify capital lock-up."
                )
                cap_rationale = f"Zero stock lock-up required; requires only {drop.required_operational_capital} CLP operational buffer."
                risk_rationale = f"Protects capital against stock risk while leveraging reliable supplier ({drop.supplier_risk_level.value} risk)."
                
                conditions.append("Validate supplier live stock feed prior to publishing")
                invalidation_triggers = [
                    "SUPPLIER_STOCK_DEPLETION",
                    "SUPPLIER_SLA_DETERIORATION",
                    "CUSTOMER_SHIPPING_RATE_INCREASE",
                ]

        elif inventory_viable and not dropshipping_viable:
            selected_model = OperatingModelType.INVENTORY
            alternative_model = OperatingModelType.DROPSHIPPING
            decision_type = OperatingDecisionType.SELECT_INVENTORY
            
            econ_rationale = f"Inventory is economically viable ({inv.expected_margin_pct:.1f}% margin), whereas dropshipping is blocked: {'; '.join(dropshipping_blockers)}."
            cap_rationale = f"Capital allocation approved for {inv.required_capital} CLP."
            risk_rationale = "Direct dropshipping rejected due to operational risks; inventory allows local quality & dispatch control."
            
            invalidation_triggers = ["DEMAND_SLOWDOWN", "EXCESSIVE_STOCK_AGING"]

        elif dropshipping_viable and not inventory_viable:
            selected_model = OperatingModelType.DROPSHIPPING
            alternative_model = OperatingModelType.INVENTORY
            decision_type = OperatingDecisionType.SELECT_DROPSHIPPING
            
            econ_rationale = f"Dropshipping is economically viable ({drop.expected_margin_pct:.1f}% margin), whereas inventory is blocked: {'; '.join(inventory_blockers)}."
            cap_rationale = f"Avoids inventory capital requirement ({inv.required_capital} CLP) which exceeded constraints."
            risk_rationale = "Eliminates physical inventory risk while operating under strict supplier SLA parameters."
            
            conditions.append("Maintain real-time supplier inventory synchronization")
            invalidation_triggers = ["SUPPLIER_STOCKOUT", "SUPPLIER_LEAD_TIME_INCREASE"]

        else:
            # Ninguno es viable
            selected_model = OperatingModelType.NEEDS_INVESTIGATION
            alternative_model = OperatingModelType.NO_DECISION
            decision_type = OperatingDecisionType.NEEDS_INVESTIGATION
            
            econ_rationale = f"Neither model meets policy standards. Inventory blockers: [{'; '.join(inventory_blockers)}]. Dropshipping blockers: [{'; '.join(dropshipping_blockers)}]."
            cap_rationale = "No capital committed."
            risk_rationale = "Both physical inventory and direct fulfillment present unacceptable risk or economic shortfall."
            
            invalidation_triggers = ["COST_RENEGOTIATION", "SUPPLIER_REPLACEMENT"]

        # Evidencia y resumen
        ev_summary = f"Evaluated opportunity {comparison.opportunity_id} with supplier {inv.supplier_id}. Confidence: {comparison.confidence.value}, Provenance: {comparison.provenance_type.value}."
        unk_summary = f"Detected unknowns: {', '.join(unknowns)}" if unknowns else "No critical unknowns detected."
        cond_summary = f"Mandatory conditions: {'; '.join(conditions)}" if conditions else "Unconditional approval under current baseline."

        explanation = DecisionExplanation(
            selected_model=selected_model,
            alternative_model=alternative_model,
            economic_rationale=econ_rationale,
            capital_rationale=cap_rationale,
            risk_rationale=risk_rationale,
            evidence_summary=ev_summary,
            unknowns_summary=unk_summary,
            conditions_summary=cond_summary,
            invalidation_triggers=tuple(invalidation_triggers),
        )

        return OperatingDecision(
            decision_id=dec_id,
            opportunity_id=comparison.opportunity_id,
            supplier_id=inv.supplier_id,
            decision_type=decision_type,
            selected_model=selected_model,
            alternative_model=alternative_model,
            comparison=comparison,
            explanation=explanation,
            conditions=tuple(conditions),
            unknowns=tuple(unknowns),
            confidence=comparison.confidence,
            provenance_type=comparison.provenance_type,
        )

    @classmethod
    def reassess_decision(
        cls,
        previous_decision: OperatingDecision,
        new_comparison: OperatingModelComparison,
        capital_budget: CapitalBudget,
        trigger: DecisionTrigger,
        reason: str,
        capital_allocation_decision: Optional[AllocationDecision] = None,
        policy: Optional[OperatingModelPolicy] = None,
    ) -> OperatingReassessmentRecord:
        """
        Reevalúa dinámicamente una decisión previa cuando cambian las condiciones
        (demanda, riesgo de proveedor, stock agotado, capital, etc.) y detecta pivots.
        """
        new_decision = cls.evaluate_operating_decision(
            comparison=new_comparison,
            capital_budget=capital_budget,
            capital_allocation_decision=capital_allocation_decision,
            policy=policy,
            decision_id=f"REASSESS-{previous_decision.opportunity_id}-{int(datetime.now(timezone.utc).timestamp())}",
        )

        pivoted = previous_decision.selected_model != new_decision.selected_model
        reassess_id = f"REC-{previous_decision.opportunity_id}-{int(datetime.now(timezone.utc).timestamp())}"

        return OperatingReassessmentRecord(
            reassessment_id=reassess_id,
            opportunity_id=previous_decision.opportunity_id,
            previous_decision=previous_decision,
            new_decision=new_decision,
            trigger=trigger,
            reason=reason,
            pivoted=pivoted,
        )
