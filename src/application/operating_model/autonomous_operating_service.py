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
    SupplierRiskProfile,
    CommercialQuote,
    ShippingOption,
)
from src.domain.capital.models import (
    CapitalBudget,
    AllocationDecision,
    AllocationStatus,
)
from src.domain.operating_model.models import (
    OperatingModelType,
    OperatingDecisionType,
    DecisionTrigger,
    DemandVelocity,
    ObsolescenceRisk,
    InventoryScenario,
    DropshippingScenario,
    OperatingModelComparison,
    OperatingModelPolicy,
    OperatingDecision,
    OperatingReassessmentRecord,
)
from src.domain.operating_model.engine import (
    OperatingModelEvaluator,
    OperatingModelEngine,
)
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits, LoopResult


class OperatingModelActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones para la Misión D-03: Inventory vs Dropshipping Engine en AutonomousLoop.
    Maneja acciones deterministas como:
    - EVALUATE_INVENTORY: Construye el escenario de compra y mantenimiento de inventario.
    - EVALUATE_DROPSHIPPING: Construye el escenario de despacho directo por proveedor.
    - COMPARE_MODELS: Compara los diferenciales de margen, profit, capital, riesgo y rotación.
    - DECIDE_OPERATING_MODEL: Ejecuta la política determinista y genera la decisión explicable.
    - REASSESS_MODEL: Reevalúa ante cambios de condiciones o contingencias y detecta pivots.
    """

    def __init__(
        self,
        opportunity: Opportunity,
        quote: CommercialQuote,
        budget: CapitalBudget,
        supplier_recommendation: Optional[SupplierRecommendation] = None,
        supplier_risk_profile: Optional[SupplierRiskProfile] = None,
        capital_allocation_decision: Optional[AllocationDecision] = None,
        policy: Optional[OperatingModelPolicy] = None,
        target_inventory_quantity: Optional[int] = None,
        shipping_option_inventory: Optional[ShippingOption] = None,
        shipping_option_dropshipping: Optional[ShippingOption] = None,
    ):
        self.opportunity = opportunity
        self.quote = quote
        self.budget = budget
        self.supplier_recommendation = supplier_recommendation
        self.supplier_risk_profile = supplier_risk_profile
        self.capital_allocation_decision = capital_allocation_decision
        self.policy = policy or OperatingModelPolicy()
        self.target_inventory_quantity = target_inventory_quantity
        self.shipping_option_inventory = shipping_option_inventory
        self.shipping_option_dropshipping = shipping_option_dropshipping

        self.inventory_scenario: Optional[InventoryScenario] = None
        self.dropshipping_scenario: Optional[DropshippingScenario] = None
        self.comparison: Optional[OperatingModelComparison] = None
        self.latest_decision: Optional[OperatingDecision] = None
        self.reassessment_history: List[OperatingReassessmentRecord] = []

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        action_name = decision.parameters.get("action_type") or str(decision.action.value)
        params = dict(decision.parameters)

        if action_name in ("EVALUATE_INVENTORY", "BUILD_INVENTORY"):
            return self._execute_evaluate_inventory()
        elif action_name in ("EVALUATE_DROPSHIPPING", "BUILD_DROPSHIPPING"):
            return self._execute_evaluate_dropshipping()
        elif action_name in ("COMPARE_MODELS", "COMPARE"):
            return self._execute_compare_models()
        elif action_name in ("DECIDE_OPERATING_MODEL", "DECIDE", "EVALUATE"):
            return self._execute_decide_model()
        elif action_name in ("REASSESS_MODEL", "REASSESS", "PIVOT"):
            return self._execute_reassess_model(params)
        else:
            return self._execute_decide_model()

    def _execute_evaluate_inventory(self) -> Dict[str, Any]:
        self.inventory_scenario = OperatingModelEvaluator.build_inventory_scenario(
            opportunity=self.opportunity,
            quote=self.quote,
            supplier_recommendation=self.supplier_recommendation,
            supplier_risk_profile=self.supplier_risk_profile,
            target_quantity=self.target_inventory_quantity,
            shipping_option=self.shipping_option_inventory,
        )
        return {
            "model": "INVENTORY",
            "target_quantity": self.inventory_scenario.target_quantity,
            "moq": self.inventory_scenario.moq,
            "required_capital": str(self.inventory_scenario.required_capital),
            "stock_exposure": str(self.inventory_scenario.stock_exposure),
            "expected_margin_pct": str(self.inventory_scenario.expected_margin_pct) if self.inventory_scenario.expected_margin_pct is not None else None,
            "expected_profit": str(self.inventory_scenario.expected_profit) if self.inventory_scenario.expected_profit is not None else None,
            "demand_velocity": self.inventory_scenario.demand_velocity.value,
            "obsolescence_risk": self.inventory_scenario.obsolescence_risk.value,
            "is_viable": self.inventory_scenario.is_viable_economically,
            "unknowns": list(self.inventory_scenario.unknowns),
        }

    def _execute_evaluate_dropshipping(self) -> Dict[str, Any]:
        self.dropshipping_scenario = OperatingModelEvaluator.build_dropshipping_scenario(
            opportunity=self.opportunity,
            quote=self.quote,
            supplier_recommendation=self.supplier_recommendation,
            supplier_risk_profile=self.supplier_risk_profile,
            direct_shipping_option=self.shipping_option_dropshipping,
        )
        return {
            "model": "DROPSHIPPING",
            "required_operational_capital": str(self.dropshipping_scenario.required_operational_capital),
            "expected_margin_pct": str(self.dropshipping_scenario.expected_margin_pct) if self.dropshipping_scenario.expected_margin_pct is not None else None,
            "expected_profit_per_unit": str(self.dropshipping_scenario.expected_profit_per_unit) if self.dropshipping_scenario.expected_profit_per_unit is not None else None,
            "supplier_risk_level": self.dropshipping_scenario.supplier_risk_level.value,
            "supplier_sla_compliant": self.dropshipping_scenario.supplier_sla_compliant,
            "is_viable": self.dropshipping_scenario.is_viable_economically,
            "unknowns": list(self.dropshipping_scenario.unknowns),
        }

    def _execute_compare_models(self) -> Dict[str, Any]:
        if self.inventory_scenario is None:
            self._execute_evaluate_inventory()
        if self.dropshipping_scenario is None:
            self._execute_evaluate_dropshipping()

        self.comparison = OperatingModelEvaluator.compare_scenarios(
            inventory_scenario=self.inventory_scenario,
            dropshipping_scenario=self.dropshipping_scenario,
        )
        return {
            "profit_differential_per_unit": str(self.comparison.profit_differential_per_unit) if self.comparison.profit_differential_per_unit is not None else None,
            "margin_differential_pct": str(self.comparison.margin_differential_pct) if self.comparison.margin_differential_pct is not None else None,
            "capital_differential": str(self.comparison.capital_differential),
            "inventory_advantages": list(self.comparison.inventory_advantages),
            "dropshipping_advantages": list(self.comparison.dropshipping_advantages),
            "inventory_disadvantages": list(self.comparison.inventory_disadvantages),
            "dropshipping_disadvantages": list(self.comparison.dropshipping_disadvantages),
            "combined_unknowns": list(self.comparison.combined_unknowns),
        }

    def _execute_decide_model(self) -> Dict[str, Any]:
        if self.comparison is None:
            self._execute_compare_models()

        self.latest_decision = OperatingModelEngine.evaluate_operating_decision(
            comparison=self.comparison,
            capital_budget=self.budget,
            capital_allocation_decision=self.capital_allocation_decision,
            policy=self.policy,
        )

        return {
            "decision_id": self.latest_decision.decision_id,
            "opportunity_id": self.latest_decision.opportunity_id,
            "supplier_id": self.latest_decision.supplier_id,
            "decision_type": self.latest_decision.decision_type.value,
            "selected_model": self.latest_decision.selected_model.value,
            "alternative_model": self.latest_decision.alternative_model.value,
            "conditions": list(self.latest_decision.conditions),
            "unknowns": list(self.latest_decision.unknowns),
            "confidence": self.latest_decision.confidence.value,
            "provenance": self.latest_decision.provenance_type.value,
            "economic_rationale": self.latest_decision.explanation.economic_rationale,
            "capital_rationale": self.latest_decision.explanation.capital_rationale,
            "risk_rationale": self.latest_decision.explanation.risk_rationale,
            "evidence_summary": self.latest_decision.explanation.evidence_summary,
        }

    def _execute_reassess_model(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.latest_decision is None:
            self._execute_decide_model()

        trigger_str = params.get("trigger", "DEMAND_CHANGE")
        try:
            trigger = DecisionTrigger(trigger_str)
        except ValueError:
            trigger = DecisionTrigger.MANUAL_TRIGGER

        reason = params.get("reason", "Dynamic condition reassessment")

        # Actualizar componentes según parámetros recibidos
        if "updated_quote" in params:
            self.quote = params["updated_quote"]
        if "updated_budget" in params:
            self.budget = params["updated_budget"]
        if "updated_risk_profile" in params:
            self.supplier_risk_profile = params["updated_risk_profile"]

        # Reconstruir escenarios y comparación
        self._execute_evaluate_inventory()
        self._execute_evaluate_dropshipping()
        self._execute_compare_models()

        record = OperatingModelEngine.reassess_decision(
            previous_decision=self.latest_decision,
            new_comparison=self.comparison,
            capital_budget=self.budget,
            trigger=trigger,
            reason=reason,
            capital_allocation_decision=self.capital_allocation_decision,
            policy=self.policy,
        )

        self.latest_decision = record.new_decision
        self.reassessment_history.append(record)

        return {
            "reassessment_id": record.reassessment_id,
            "previous_model": record.previous_decision.selected_model.value,
            "new_model": record.new_decision.selected_model.value,
            "pivoted": record.pivoted,
            "trigger": record.trigger.value,
            "reason": record.reason,
            "decision_type": record.new_decision.decision_type.value,
        }


class AutonomousOperatingModelService:
    """
    Servicio de aplicación para coordinar el análisis y decisión de modelo operativo
    (INVENTORY vs DROPSHIPPING) a través de AutonomousLoop.
    """

    def __init__(
        self,
        decision_provider: DecisionProvider,
        mission_repository: Optional[MissionRepository] = None,
        policy: Optional[OperatingModelPolicy] = None,
    ):
        self.decision_provider = decision_provider
        self.mission_repository = mission_repository
        self.policy = policy or OperatingModelPolicy()

    def run_operating_model_mission(
        self,
        mission_id: str,
        opportunity: Opportunity,
        quote: CommercialQuote,
        budget: CapitalBudget,
        supplier_recommendation: Optional[SupplierRecommendation] = None,
        supplier_risk_profile: Optional[SupplierRiskProfile] = None,
        capital_allocation_decision: Optional[AllocationDecision] = None,
        target_inventory_quantity: Optional[int] = None,
        max_iterations: int = 6,
    ) -> Tuple[LoopResult, OperatingDecision]:
        executor = OperatingModelActionExecutor(
            opportunity=opportunity,
            quote=quote,
            budget=budget,
            supplier_recommendation=supplier_recommendation,
            supplier_risk_profile=supplier_risk_profile,
            capital_allocation_decision=capital_allocation_decision,
            policy=self.policy,
            target_inventory_quantity=target_inventory_quantity,
        )

        def completion_validator(state: LoopState) -> Tuple[bool, str]:
            if executor.latest_decision is not None:
                return True, f"Operating model decided: {executor.latest_decision.selected_model.value}"
            return False, "Operating model decision not yet finalized"

        loop = AutonomousLoop(
            decision_provider=self.decision_provider,
            action_executor=executor,
            max_iterations=max_iterations,
            completion_validator=completion_validator,
        )

        goal = f"Evaluate optimal operating model (Inventory vs Dropshipping) for opportunity {opportunity.product_id}"
        result = loop.run(
            mission_id=mission_id,
            goal=goal,
            initial_target=opportunity.product_id,
        )

        if executor.latest_decision is None:
            # Forzar ejecución determinista si el loop terminó sin invocar decider
            executor._execute_decide_model()

        return result, executor.latest_decision
