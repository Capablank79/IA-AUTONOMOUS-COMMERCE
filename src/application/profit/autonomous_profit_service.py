from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, List, Tuple, Sequence
from types import MappingProxyType

from src.domain.mission.models import (
    Mission,
    MissionStatus,
    MissionResult,
    MissionType,
    LoopState,
    LoopAction,
    LoopDecision,
)
from src.domain.mission.ports import DecisionProvider, ActionExecutor, MissionRepository
from src.domain.opportunity.models import Opportunity
from src.domain.supplier_intelligence.models import (
    SupplierCandidate,
    SupplierRecommendation,
    CommercialQuote,
    EvidenceProvenanceType,
    PriceTier,
    ShippingOption,
)
from src.domain.profit.models import (
    CostComponent,
    CostComponentType,
    CostComponentStatus,
    SalePrice,
    SalePriceType,
    ExchangeRate,
    ProfitStatus,
    EconomicEvaluationResult,
    MarketplaceFeeStructure,
)
from src.domain.profit.engine import (
    ProfitEngine,
    LandedCostCalculator,
    UnitEconomicsCalculator,
    BreakEvenCalculator,
    EconomicScenarioAnalyzer,
    EconomicInvestigationDetector,
)
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits, LoopResult


class ProfitEvaluationActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones para la Misión D-01: Profit Engine & Landed Cost en AutonomousLoop.
    Maneja acciones como:
    - COLLECT_COSTS: Recolecta componentes de costos desde la recomendación del proveedor y cotizaciones.
    - COMPUTE_LANDED_COST: Calcula costo puesto en destino determinista.
    - COMPUTE_UNIT_ECONOMICS: Evalúa gross profit, net profit, márgenes y break-even.
    - INVESTIGATE_MISSING_COST: Solicita / simula obtención de un dato faltante (sin inventar).
    - GENERATE_SCENARIOS: Genera análisis de escenarios (Base, Conservador, Optimista).
    """

    def __init__(
        self,
        opportunity: Opportunity,
        recommendation: SupplierRecommendation,
        quote: Optional[CommercialQuote] = None,
        shipping_option: Optional[ShippingOption] = None,
        marketplace_fee_structure: Optional[MarketplaceFeeStructure] = None,
        exchange_rate: Optional[ExchangeRate] = None,
        import_duty_rate: Optional[Decimal] = None,
        tax_rate: Optional[Decimal] = None,
    ):
        self.opportunity = opportunity
        self.recommendation = recommendation
        self.quote = quote
        self.shipping_option = shipping_option
        self.marketplace_fee_structure = marketplace_fee_structure
        self.exchange_rate = exchange_rate
        self.import_duty_rate = import_duty_rate
        self.tax_rate = tax_rate

        self._profit_engine = ProfitEngine()
        self._latest_evaluation: Optional[EconomicEvaluationResult] = None
        self._investigated_components: Dict[CostComponentType, CostComponent] = {}

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        action_name = decision.parameters.get("action_type") or str(decision.action.value)
        params = dict(decision.parameters)

        if action_name in ("COLLECT_COSTS", "OBSERVE", "CONTINUE"):
            res, _ = self._execute_collect_costs()
            # Si ya tenemos los costos básicos, calcular economics inmediatamente
            econ_res, _ = self._execute_compute_economics(params)
            res.update(econ_res)
            return res
        elif action_name in ("COMPUTE_ECONOMICS", "EVALUATE"):
            res, _ = self._execute_compute_economics(params)
            return res
        elif action_name == "INVESTIGATE_MISSING_COST":
            res, _ = self._execute_investigate_missing_cost(params)
            # Recomputar economics con la nueva evidencia
            econ_res, _ = self._execute_compute_economics(params)
            res.update(econ_res)
            return res
        elif action_name == "GENERATE_SCENARIOS":
            res, _ = self._execute_generate_scenarios()
            return res
        else:
            res, _ = self._execute_compute_economics(params)
            return res

    def _execute_collect_costs(self) -> Tuple[Dict[str, Any], Optional[str]]:
        # Extraer precio de venta observado de la oportunidad
        observed_price = None
        if hasattr(self.opportunity, "listing") and self.opportunity.listing and self.opportunity.listing.price:
            observed_price = self.opportunity.listing.price.amount
        elif hasattr(self.opportunity, "target_price") and self.opportunity.target_price:
            observed_price = self.opportunity.target_price
        elif hasattr(self.opportunity, "price_clp") and self.opportunity.price_clp:
            observed_price = self.opportunity.price_clp
        else:
            observed_price = Decimal("10000")

        sale_price = SalePrice(
            amount=observed_price,
            currency="CLP",
            price_type=SalePriceType.OBSERVED_SALE_PRICE,
            confidence=self.opportunity.confidence,
            provenance_type=EvidenceProvenanceType.FIXTURE,
            source="MERCADO_LIBRE_LISTING",
        )

        # Extraer costo de compra de la cotización o recomendación
        purchase_cost: CostComponent
        shipping_cost: CostComponent

        if self.quote:
            purchase_cost = CostComponent(
                component_type=CostComponentType.PRODUCT_COST,
                status=CostComponentStatus.KNOWN,
                amount=self.quote.unit_price,
                currency=self.quote.currency,
                confidence=self.quote.confidence,
                provenance_type=self.quote.provenance_type,
                source=f"QUOTE_{self.quote.quote_id}",
                is_per_unit=True,
            )
            if self.quote.shipping_cost is not None:
                shipping_cost = CostComponent(
                    component_type=CostComponentType.SHIPPING_COST,
                    status=CostComponentStatus.KNOWN,
                    amount=self.quote.shipping_cost,
                    currency=self.quote.currency,
                    confidence=self.quote.confidence,
                    provenance_type=self.quote.provenance_type,
                    source=f"QUOTE_SHIPPING_{self.quote.quote_id}",
                    is_per_unit=False,
                )
            elif self.shipping_option and self.shipping_option.cost is not None:
                shipping_cost = CostComponent(
                    component_type=CostComponentType.SHIPPING_COST,
                    status=CostComponentStatus.KNOWN,
                    amount=self.shipping_option.cost,
                    currency=self.shipping_option.currency,
                    confidence=self.shipping_option.confidence,
                    provenance_type=self.shipping_option.provenance_type,
                    source=f"SHIPPING_OPTION_{self.shipping_option.carrier}",
                    is_per_unit=False,
                )
            else:
                shipping_cost = CostComponent(
                    component_type=CostComponentType.SHIPPING_COST,
                    status=CostComponentStatus.UNKNOWN,
                    currency=self.quote.currency,
                    is_per_unit=False,
                )
        else:
            purchase_cost = CostComponent(
                component_type=CostComponentType.PRODUCT_COST,
                status=CostComponentStatus.UNKNOWN,
                currency="CLP",
                is_per_unit=True,
            )
            shipping_cost = CostComponent(
                component_type=CostComponentType.SHIPPING_COST,
                status=CostComponentStatus.UNKNOWN,
                currency="CLP",
                is_per_unit=False,
            )

        # Marketplace fee
        marketplace_fee: CostComponent
        if self.marketplace_fee_structure:
            marketplace_fee = CostComponent(
                component_type=CostComponentType.MARKETPLACE_FEES,
                status=CostComponentStatus.KNOWN,
                currency=sale_price.currency,
                fee_rate=self.marketplace_fee_structure.fee_rate,
                fixed_fee_amount=self.marketplace_fee_structure.fixed_fee,
                confidence=self.marketplace_fee_structure.confidence,
                source=self.marketplace_fee_structure.source,
            )
        else:
            marketplace_fee = CostComponent(
                component_type=CostComponentType.MARKETPLACE_FEES,
                status=CostComponentStatus.UNKNOWN,
                currency=sale_price.currency,
            )

        return {
            "status": "COSTS_COLLECTED",
            "sale_price_amount": str(sale_price.amount),
            "purchase_cost_status": purchase_cost.status.value,
            "shipping_cost_status": shipping_cost.status.value,
            "marketplace_fee_status": marketplace_fee.status.value,
        }, None

    def _execute_compute_economics(self, params: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
        observed_price = None
        if hasattr(self.opportunity, "listing") and self.opportunity.listing and self.opportunity.listing.price:
            observed_price = self.opportunity.listing.price.amount
        elif hasattr(self.opportunity, "target_price") and self.opportunity.target_price:
            observed_price = self.opportunity.target_price
        elif hasattr(self.opportunity, "price_clp") and self.opportunity.price_clp:
            observed_price = self.opportunity.price_clp
        else:
            observed_price = Decimal("10000")

        sale_price = SalePrice(
            amount=observed_price,
            currency="CLP",
            price_type=SalePriceType.OBSERVED_SALE_PRICE,
            confidence=self.opportunity.confidence,
            provenance_type=EvidenceProvenanceType.FIXTURE,
            source="MERCADO_LIBRE_LISTING",
        )

        # Resolver componentes con overrides de investigación previa si existen
        purchase_cost = self._investigated_components.get(CostComponentType.PRODUCT_COST)
        if purchase_cost is None:
            if self.quote:
                purchase_cost = CostComponent(
                    component_type=CostComponentType.PRODUCT_COST,
                    status=CostComponentStatus.KNOWN,
                    amount=self.quote.unit_price,
                    currency=self.quote.currency,
                    confidence=self.quote.confidence,
                    provenance_type=self.quote.provenance_type,
                    source=f"QUOTE_{self.quote.quote_id}",
                    is_per_unit=True,
                )
            else:
                purchase_cost = CostComponent(
                    component_type=CostComponentType.PRODUCT_COST,
                    status=CostComponentStatus.UNKNOWN,
                    currency="CLP",
                    is_per_unit=True,
                )

        shipping_cost = self._investigated_components.get(CostComponentType.SHIPPING_COST)
        if shipping_cost is None:
            if self.quote and self.quote.shipping_cost is not None:
                shipping_cost = CostComponent(
                    component_type=CostComponentType.SHIPPING_COST,
                    status=CostComponentStatus.KNOWN,
                    amount=self.quote.shipping_cost,
                    currency=self.quote.currency,
                    confidence=self.quote.confidence,
                    provenance_type=self.quote.provenance_type,
                    source=f"QUOTE_SHIPPING_{self.quote.quote_id}",
                    is_per_unit=False,
                )
            elif self.shipping_option and self.shipping_option.cost is not None:
                shipping_cost = CostComponent(
                    component_type=CostComponentType.SHIPPING_COST,
                    status=CostComponentStatus.KNOWN,
                    amount=self.shipping_option.cost,
                    currency=self.shipping_option.currency,
                    confidence=self.shipping_option.confidence,
                    provenance_type=self.shipping_option.provenance_type,
                    source=f"SHIPPING_OPTION_{self.shipping_option.carrier}",
                    is_per_unit=False,
                )
            else:
                shipping_cost = CostComponent(
                    component_type=CostComponentType.SHIPPING_COST,
                    status=CostComponentStatus.UNKNOWN,
                    currency=purchase_cost.currency,
                    is_per_unit=False,
                )

        marketplace_fee = self._investigated_components.get(CostComponentType.MARKETPLACE_FEES)
        if marketplace_fee is None:
            if self.marketplace_fee_structure:
                marketplace_fee = CostComponent(
                    component_type=CostComponentType.MARKETPLACE_FEES,
                    status=CostComponentStatus.KNOWN,
                    currency=sale_price.currency,
                    fee_rate=self.marketplace_fee_structure.fee_rate,
                    fixed_fee_amount=self.marketplace_fee_structure.fixed_fee,
                    confidence=self.marketplace_fee_structure.confidence,
                    source=self.marketplace_fee_structure.source,
                )
            else:
                marketplace_fee = CostComponent(
                    component_type=CostComponentType.MARKETPLACE_FEES,
                    status=CostComponentStatus.UNKNOWN,
                    currency=sale_price.currency,
                )

        # Quantity scenarios
        qty_scenarios = [1]
        price_tiers: List[PriceTier] = []
        moq_qty: Optional[int] = None
        if self.quote:
            if hasattr(self.quote, "moq") and self.quote.moq and self.quote.moq.quantity and self.quote.moq.quantity > 1:
                moq_qty = self.quote.moq.quantity
            elif hasattr(self.quote, "moq_info") and self.quote.moq_info and self.quote.moq_info.moq > 1:
                moq_qty = self.quote.moq_info.moq
            
            if moq_qty and moq_qty not in qty_scenarios:
                qty_scenarios.append(moq_qty)

            if self.quote.price_tiers:
                price_tiers = list(self.quote.price_tiers)
                for pt in self.quote.price_tiers:
                    if pt.min_quantity not in qty_scenarios:
                        qty_scenarios.append(pt.min_quantity)

        # Si hay un MOQ relevante, priorizarlo como escenario primario
        primary_qty = moq_qty if moq_qty and moq_qty > 1 else 1

        product_id = getattr(self.opportunity, "opportunity_id", None) or getattr(self.opportunity, "product_id", "PROD_UNKNOWN")
        supplier_id = "UNKNOWN_SUPPLIER"
        if hasattr(self.recommendation, "primary_supplier") and self.recommendation.primary_supplier:
            supplier_id = self.recommendation.primary_supplier.supplier_id
        elif hasattr(self.recommendation, "recommended_supplier_id"):
            supplier_id = self.recommendation.recommended_supplier_id

        evaluation = self._profit_engine.evaluate_opportunity_economics(
            product_id=product_id,
            supplier_id=supplier_id,
            sale_price=sale_price,
            purchase_cost=purchase_cost,
            shipping_cost=shipping_cost,
            quantity_scenarios=qty_scenarios,
            primary_quantity_scenario=primary_qty,
            price_tiers=price_tiers,
            marketplace_fees=marketplace_fee,
            exchange_rate=self.exchange_rate,
        )

        self._latest_evaluation = evaluation

        return {
            "status": "ECONOMICS_COMPUTED",
            "overall_status": evaluation.overall_status.value,
            "overall_confidence": evaluation.overall_confidence.value,
            "gross_profit": str(evaluation.primary_unit_economics.gross_profit) if evaluation.primary_unit_economics.gross_profit is not None else None,
            "net_profit": str(evaluation.primary_unit_economics.net_profit) if evaluation.primary_unit_economics.net_profit is not None else None,
            "gross_margin_pct": str(evaluation.primary_unit_economics.gross_margin_pct) if evaluation.primary_unit_economics.gross_margin_pct is not None else None,
            "net_margin_pct": str(evaluation.primary_unit_economics.net_margin_pct) if evaluation.primary_unit_economics.net_margin_pct is not None else None,
            "break_even_price": str(evaluation.break_even.break_even_sale_price) if evaluation.break_even.break_even_sale_price is not None else None,
            "unknowns": list(evaluation.primary_unit_economics.unknowns),
            "investigation_needs_count": len(evaluation.investigation_needs),
        }, None

    def _execute_investigate_missing_cost(self, params: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
        component_str = params.get("component_type", "SHIPPING_COST")
        comp_type = CostComponentType(component_str)

        # Si en los parámetros viene un dato verificado de investigación, registrarlo
        if "amount" in params:
            amt = Decimal(str(params["amount"]))
            comp = CostComponent(
                component_type=comp_type,
                status=CostComponentStatus.KNOWN,
                amount=amt,
                currency=params.get("currency", "CLP"),
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source=params.get("source", "INVESTIGATION_SERVICE"),
            )
            self._investigated_components[comp_type] = comp
            return {
                "status": "INVESTIGATION_COMPLETED",
                "resolved_component": comp_type.value,
                "amount": str(amt),
            }, None
        elif "fee_rate" in params:
            fee_rate = Decimal(str(params["fee_rate"]))
            comp = CostComponent(
                component_type=comp_type,
                status=CostComponentStatus.KNOWN,
                currency=params.get("currency", "CLP"),
                fee_rate=fee_rate,
                fixed_fee_amount=Decimal(str(params.get("fixed_fee", "0"))),
                confidence=Confidence.HIGH,
                provenance_type=EvidenceProvenanceType.DERIVED,
                source=params.get("source", "INVESTIGATION_TARIFF_LOOKUP"),
            )
            self._investigated_components[comp_type] = comp
            return {
                "status": "INVESTIGATION_COMPLETED",
                "resolved_component": comp_type.value,
                "fee_rate": str(fee_rate),
            }, None
        else:
            return {
                "status": "INVESTIGATION_PENDING",
                "missing_component": comp_type.value,
                "message": "No external evidence provided to resolve unknown cost without fabrication.",
            }, None

    def _execute_generate_scenarios(self) -> Tuple[Dict[str, Any], Optional[str]]:
        if self._latest_evaluation and self._latest_evaluation.scenarios:
            sc = self._latest_evaluation.scenarios
            return {
                "status": "SCENARIOS_GENERATED",
                "summary": sc.comparison_summary,
                "base_net_margin": str(sc.base_scenario.net_margin_pct),
                "cons_net_margin": str(sc.conservative_scenario.net_margin_pct),
                "opt_net_margin": str(sc.optimistic_scenario.net_margin_pct),
            }, None
        return {"status": "NO_EVALUATION_AVAILABLE"}, None

    def get_latest_evaluation(self) -> Optional[EconomicEvaluationResult]:
        return self._latest_evaluation


class DefaultProfitDecisionProvider(DecisionProvider):
    """
    DecisionProvider determinista por defecto para evaluación económica de la Misión D-01.
    Ejecuta el ciclo:
    1. COLLECT_COSTS / COMPUTE_ECONOMICS (evaluación inicial).
    2. COMPLETE para converger la misión.
    """
    def __init__(self):
        self._step = 0

    def decide(self, state: LoopState) -> LoopDecision:
        self._step += 1
        if self._step == 1:
            return LoopDecision(
                action=LoopAction.CONTINUE,
                target=state.current_target,
                parameters={"action_type": "COLLECT_COSTS"},
                reason="Collect cost components and compute preliminary unit economics",
                confidence=0.9,
            )
        else:
            return LoopDecision(
                action=LoopAction.COMPLETE,
                reason="Profit engine evaluation completed",
                confidence=0.98,
            )


class AutonomousProfitService:
    """
    Servicio de orquestación autónoma de la Misión D-01: Profit Engine & Landed Cost.
    Conecta una oportunidad y su proveedor seleccionado en una evaluación económica determinista.
    """

    def __init__(
        self,
        decision_provider: Optional[DecisionProvider] = None,
        mission_repository: Optional[MissionRepository] = None,
        default_max_iterations: int = 5,
        default_limits: Optional[LoopLimits] = None,
    ):
        self.decision_provider = decision_provider or DefaultProfitDecisionProvider()
        self.mission_repository = mission_repository
        self.default_max_iterations = default_max_iterations
        self.default_limits = default_limits or LoopLimits(max_iterations=default_max_iterations)

    def execute_profit_mission(
        self,
        opportunity: Opportunity,
        recommendation: SupplierRecommendation,
        quote: Optional[CommercialQuote] = None,
        shipping_option: Optional[ShippingOption] = None,
        marketplace_fee_structure: Optional[MarketplaceFeeStructure] = None,
        exchange_rate: Optional[ExchangeRate] = None,
        mission_id: Optional[str] = None,
        limits: Optional[LoopLimits] = None,
    ) -> MissionResult:
        mission_id = mission_id or f"profit-eval-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        limits = limits or self.default_limits

        executor = ProfitEvaluationActionExecutor(
            opportunity=opportunity,
            recommendation=recommendation,
            quote=quote,
            shipping_option=shipping_option,
            marketplace_fee_structure=marketplace_fee_structure,
            exchange_rate=exchange_rate,
        )

        # Realizar una evaluación inicial directa para asegurar estado base disponible
        executor._execute_compute_economics({})

        def completion_validator(state: LoopState) -> Tuple[bool, str]:
            eval_res = executor.get_latest_evaluation()
            if eval_res is None:
                return False, "No economic evaluation has been computed yet."
            if eval_res.overall_status == ProfitStatus.PROFIT_COMPLETE:
                return True, "Deterministic unit economics, landed cost and net margins successfully completed without unknowns."
            if eval_res.overall_status == ProfitStatus.NOT_COMPARABLE_CURRENCY:
                return True, "Evaluation completed with status NOT_COMPARABLE_CURRENCY (Multi-currency without verified FX)."
            if eval_res.overall_status in (ProfitStatus.PROFIT_PARTIAL, ProfitStatus.PROFIT_INCOMPLETE, ProfitStatus.PROFIT_UNKNOWN):
                return True, f"Evaluation concluded with status {eval_res.overall_status.value} with explicit unknowns/investigation needs."
            return False, "Evaluation in progress."

        loop = AutonomousLoop(
            decision_provider=self.decision_provider,
            action_executor=executor,
            max_iterations=limits.max_iterations,
            limits=limits,
            completion_validator=completion_validator,
        )

        mission = Mission(
            mission_id=mission_id,
            type=MissionType.PROFIT_EVALUATION if hasattr(MissionType, "PROFIT_EVALUATION") else MissionType.SUPPLIER_DISCOVERY,
            parameters={"target": opportunity.opportunity_id, "max_iterations": limits.max_iterations},
        )
        if self.mission_repository:
            self.mission_repository.save_mission(mission)

        goal = f"Evaluate deterministic unit economics, landed cost and profit for opportunity '{opportunity.opportunity_id}'"
        loop_result: LoopResult = loop.run(
            mission_id=mission_id,
            goal=goal,
            initial_target=opportunity.opportunity_id,
        )

        eval_res = executor.get_latest_evaluation()

        status = MissionStatus.COMPLETED if loop_result.status in ("COMPLETED", "CONVERGED") else MissionStatus.FAILED
        if eval_res and eval_res.overall_status in (ProfitStatus.PROFIT_UNKNOWN, ProfitStatus.NOT_COMPARABLE_CURRENCY):
            status = MissionStatus.BLOCKED

        mission_res = MissionResult(
            mission_id=mission_id,
            status=status,
            output={
                "loop_status": loop_result.status,
                "economic_evaluation": eval_res,
                "overall_status": eval_res.overall_status.value if eval_res else None,
                "break_even": eval_res.break_even if eval_res else None,
                "final_state": loop_result.final_state,
                "iterations_count": len(loop_result.trace),
            },
            errors=loop_result.errors,
            finished_at=datetime.now(timezone.utc),
        )

        if self.mission_repository:
            self.mission_repository.save_result(mission_res)

        return mission_res
