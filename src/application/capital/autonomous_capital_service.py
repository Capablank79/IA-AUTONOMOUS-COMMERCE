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
)
from src.domain.profit.models import (
    EconomicEvaluationResult,
)
from src.domain.capital.models import (
    AllocationStatus,
    AllocationDecisionReason,
    CapitalBudget,
    CapitalExposure,
    AllocationPolicy,
    AllocationDecision,
    CapitalAllocation,
)
from src.domain.capital.engine import CapitalAllocationEngine
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits, LoopResult


class CapitalAllocationActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones para la Misión D-02: Capital Allocation Engine en AutonomousLoop.
    Maneja acciones deterministas como:
    - CHECK_BUDGET: Evalúa capital disponible, reservado y comprometido.
    - COMPUTE_EXPOSURE: Calcula los límites de exposición por oportunidad.
    - EVALUATE_ALLOCATION: Produce la decisión de asignación (APPROVED, PARTIALLY_APPROVED, REJECTED, NEEDS_INVESTIGATION).
    - CREATE_ALLOCATION: Registra la asignación formalmente y actualiza el presupuesto.
    - REASSESS_ALLOCATION: Reevalúa ante deterioro y reduce/libera/reasigna capital.
    """

    def __init__(
        self,
        opportunity: Opportunity,
        budget: CapitalBudget,
        policy: Optional[AllocationPolicy] = None,
        economic_evaluation: Optional[EconomicEvaluationResult] = None,
        supplier_recommendation: Optional[SupplierRecommendation] = None,
        supplier_risk_profile: Optional[SupplierRiskProfile] = None,
        requested_capital: Optional[Decimal] = None,
        existing_exposure: Decimal = Decimal("0"),
    ):
        self.opportunity = opportunity
        self.budget = budget
        self.policy = policy or AllocationPolicy()
        self.economic_evaluation = economic_evaluation
        self.supplier_recommendation = supplier_recommendation
        self.supplier_risk_profile = supplier_risk_profile
        self.requested_capital = requested_capital
        self.existing_exposure = existing_exposure

        self.latest_decision: Optional[AllocationDecision] = None
        self.active_allocation: Optional[CapitalAllocation] = None
        self.current_budget: CapitalBudget = budget

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        action_name = decision.parameters.get("action_type") or str(decision.action.value)
        params = dict(decision.parameters)

        if action_name in ("CHECK_BUDGET", "OBSERVE"):
            return self._execute_check_budget()
        elif action_name in ("COMPUTE_EXPOSURE", "EVALUATE_EXPOSURE"):
            return self._execute_compute_exposure()
        elif action_name in ("EVALUATE_ALLOCATION", "EVALUATE", "DECIDE"):
            return self._execute_evaluate_allocation(params)
        elif action_name in ("CREATE_ALLOCATION", "ALLOCATE", "COMMIT"):
            return self._execute_create_allocation()
        elif action_name in ("REASSESS_ALLOCATION", "REASSESS", "REVISE"):
            return self._execute_reassess_allocation(params)
        elif action_name in ("RELEASE_ALLOCATION", "RELEASE"):
            return self._execute_release_allocation()
        else:
            return self._execute_evaluate_allocation(params)

    def _execute_check_budget(self) -> Dict[str, Any]:
        return {
            "total_capital": str(self.current_budget.total_capital),
            "reserved_capital": str(self.current_budget.reserved_capital),
            "committed_capital": str(self.current_budget.committed_capital),
            "allocatable_capital": str(self.current_budget.allocatable_capital),
            "uncommitted_capital": str(self.current_budget.uncommitted_capital),
            "currency": self.current_budget.currency,
        }

    def _execute_compute_exposure(self) -> Dict[str, Any]:
        exposure = CapitalAllocationEngine.calculate_exposure(
            opportunity_id=self.opportunity.product_id,
            budget=self.current_budget,
            policy=self.policy,
            existing_exposure=self.existing_exposure,
        )
        return {
            "opportunity_id": exposure.opportunity_id,
            "existing_exposure": str(exposure.existing_exposure),
            "maximum_allowed_exposure": str(exposure.maximum_allowed_exposure),
            "remaining_opportunity_capacity": str(exposure.remaining_opportunity_capacity),
            "effective_available_ceiling": str(exposure.effective_available_ceiling),
        }

    def _execute_evaluate_allocation(self, params: Dict[str, Any]) -> Dict[str, Any]:
        req_cap = Decimal(str(params["requested_capital"])) if "requested_capital" in params else self.requested_capital
        
        self.latest_decision = CapitalAllocationEngine.evaluate_allocation(
            opportunity=self.opportunity,
            budget=self.current_budget,
            policy=self.policy,
            economic_evaluation=self.economic_evaluation,
            supplier_recommendation=self.supplier_recommendation,
            supplier_risk_profile=self.supplier_risk_profile,
            requested_capital=req_cap,
            existing_exposure=self.existing_exposure,
        )

        return {
            "decision_id": self.latest_decision.decision_id,
            "status": self.latest_decision.status.value,
            "reason": self.latest_decision.reason.value,
            "requested_capital": str(self.latest_decision.requested_capital),
            "approved_capital": str(self.latest_decision.approved_capital),
            "unapproved_capital": str(self.latest_decision.unapproved_capital),
            "maximum_allowed_exposure": str(self.latest_decision.maximum_allowed_exposure),
            "available_allocatable_capital": str(self.latest_decision.available_allocatable_capital),
            "remaining_allocatable_capital": str(self.latest_decision.remaining_allocatable_capital),
            "confidence": self.latest_decision.confidence.value,
            "provenance_type": self.latest_decision.provenance_type.value,
            "explanation": self.latest_decision.explanation,
            "unknowns": list(self.latest_decision.unknowns),
            "conditions": list(self.latest_decision.conditions),
        }

    def _execute_create_allocation(self) -> Dict[str, Any]:
        if self.latest_decision is None:
            self._execute_evaluate_allocation({})

        assert self.latest_decision is not None
        if self.latest_decision.approved_capital <= Decimal("0"):
            return {
                "status": "SKIPPED_NO_APPROVED_CAPITAL",
                "allocated_amount": "0",
                "reason": self.latest_decision.reason.value,
            }

        self.active_allocation, self.current_budget = CapitalAllocationEngine.create_allocation(
            budget=self.current_budget,
            decision=self.latest_decision,
        )

        return {
            "status": "ALLOCATION_CREATED",
            "allocation_id": self.active_allocation.allocation_id,
            "allocated_amount": str(self.active_allocation.allocated_amount),
            "new_committed_capital": str(self.current_budget.committed_capital),
            "new_allocatable_capital": str(self.current_budget.allocatable_capital),
        }

    def _execute_reassess_allocation(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.active_allocation is None:
            return {"status": "ERROR_NO_ACTIVE_ALLOCATION"}

        new_econ = params.get("new_economic_evaluation") or self.economic_evaluation
        new_sup = params.get("new_supplier_recommendation") or self.supplier_recommendation
        reason = str(params.get("reason", "Reassessment triggered"))

        self.active_allocation, self.current_budget, self.latest_decision = CapitalAllocationEngine.reassess_allocation_on_deterioration(
            allocation=self.active_allocation,
            budget=self.current_budget,
            opportunity=self.opportunity,
            new_economic_evaluation=new_econ,
            new_supplier_recommendation=new_sup,
            policy=self.policy,
            reason=reason,
        )

        return {
            "status": "REASSESSMENT_COMPLETED",
            "allocation_id": self.active_allocation.allocation_id,
            "new_allocation_status": self.active_allocation.status.value,
            "allocated_amount": str(self.active_allocation.allocated_amount),
            "decision_reason": self.latest_decision.reason.value,
            "remaining_allocatable_capital": str(self.current_budget.allocatable_capital),
        }

    def _execute_release_allocation(self) -> Dict[str, Any]:
        if self.active_allocation is None:
            return {"status": "ERROR_NO_ACTIVE_ALLOCATION"}

        self.active_allocation, self.current_budget = CapitalAllocationEngine.release_allocation(
            allocation=self.active_allocation,
            budget=self.current_budget,
            reason="Manual release requested",
        )

        return {
            "status": "ALLOCATION_RELEASED",
            "allocation_id": self.active_allocation.allocation_id,
            "allocated_amount": "0",
            "committed_capital": str(self.current_budget.committed_capital),
            "allocatable_capital": str(self.current_budget.allocatable_capital),
        }


class DeterministicCapitalDecisionProvider(DecisionProvider):
    """
    Proveedor determinista de decisiones de ciclo para la Misión D-02 en AutonomousLoop.
    Controla el avance por etapas:
    1. CHECK_BUDGET
    2. COMPUTE_EXPOSURE
    3. EVALUATE_ALLOCATION
    4. COMMIT_ALLOCATION / STOP
    """

    def decide(self, state: LoopState) -> LoopDecision:
        it = state.iteration

        if it == 0:
            return LoopDecision(
                action=LoopAction.CONTINUE,
                reason="Verifying capital budget and reserve status",
                parameters={"action_type": "CHECK_BUDGET"},
            )
        elif it == 1:
            return LoopDecision(
                action=LoopAction.CONTINUE,
                reason="Evaluating opportunity maximum exposure and remaining capacity",
                parameters={"action_type": "COMPUTE_EXPOSURE"},
            )
        elif it == 2:
            return LoopDecision(
                action=LoopAction.CONTINUE,
                reason="Evaluating risk-adjusted capital allocation decision",
                parameters={"action_type": "EVALUATE_ALLOCATION"},
            )
        elif it == 3:
            # Si en la iteración anterior se aprobó o parcialmente aprobó capital, comprometerlo
            return LoopDecision(
                action=LoopAction.CONTINUE,
                reason="Registering formal capital allocation and updating budget commitment",
                parameters={"action_type": "CREATE_ALLOCATION"},
            )
        else:
            return LoopDecision(
                action=LoopAction.COMPLETE,
                reason="Capital allocation process completed deterministically",
                parameters={"action_type": "COMPLETE"},
            )


class AutonomousCapitalService:
    """
    Servicio de aplicación orquestador para la Misión D-02: Capital Allocation Engine.
    Ejecuta el AutonomousLoop y emite el resultado formal de la misión.
    """

    def __init__(
        self,
        mission_repository: Optional[MissionRepository] = None,
        loop_limits: Optional[LoopLimits] = None,
    ):
        self.mission_repository = mission_repository
        self.loop_limits = loop_limits or LoopLimits(max_iterations=5)

    def run_mission(
        self,
        mission: Mission,
        opportunity: Opportunity,
        budget: CapitalBudget,
        policy: Optional[AllocationPolicy] = None,
        economic_evaluation: Optional[EconomicEvaluationResult] = None,
        supplier_recommendation: Optional[SupplierRecommendation] = None,
        supplier_risk_profile: Optional[SupplierRiskProfile] = None,
        requested_capital: Optional[Decimal] = None,
        existing_exposure: Decimal = Decimal("0"),
    ) -> Tuple[MissionResult, CapitalAllocationActionExecutor]:
        """
        Ejecuta la misión D-02 en el AutonomousLoop de forma determinista y reproducible.
        """
        executor = CapitalAllocationActionExecutor(
            opportunity=opportunity,
            budget=budget,
            policy=policy,
            economic_evaluation=economic_evaluation,
            supplier_recommendation=supplier_recommendation,
            supplier_risk_profile=supplier_risk_profile,
            requested_capital=requested_capital,
            existing_exposure=existing_exposure,
        )
        decision_provider = DeterministicCapitalDecisionProvider()

        def completion_validator(state: LoopState) -> Tuple[bool, str]:
            if executor.latest_decision is not None and (
                executor.active_allocation is not None
                or executor.latest_decision.approved_capital == Decimal("0")
                or executor.latest_decision.status in (AllocationStatus.REJECTED, AllocationStatus.NEEDS_INVESTIGATION)
            ):
                return True, "Capital allocation evaluation and commitment complete"
            return False, "Evaluation in progress"

        loop = AutonomousLoop(
            decision_provider=decision_provider,
            action_executor=executor,
            max_iterations=self.loop_limits.max_iterations,
            limits=self.loop_limits,
            completion_validator=completion_validator,
        )

        goal = mission.parameters.get("goal", f"Capital allocation for {opportunity.product_id}")
        loop_res = loop.run(
            mission_id=mission.mission_id,
            goal=goal,
            initial_target=opportunity.product_id,
        )

        assert executor.latest_decision is not None, "A decision must be produced during execution"

        mission_status = MissionStatus.COMPLETED if loop_res.status in ("COMPLETED", "CONVERGED") else MissionStatus.BLOCKED

        res = MissionResult(
            mission_id=mission.mission_id,
            status=mission_status,
            output={
                "decision_id": executor.latest_decision.decision_id,
                "allocation_status": executor.latest_decision.status.value,
                "reason": executor.latest_decision.reason.value,
                "requested_capital": str(executor.latest_decision.requested_capital),
                "approved_capital": str(executor.latest_decision.approved_capital),
                "unapproved_capital": str(executor.latest_decision.unapproved_capital),
                "maximum_allowed_exposure": str(executor.latest_decision.maximum_allowed_exposure),
                "available_allocatable_capital": str(executor.latest_decision.available_allocatable_capital),
                "remaining_allocatable_capital": str(executor.latest_decision.remaining_allocatable_capital),
                "currency": executor.latest_decision.currency,
                "allocation_ratio": str(executor.latest_decision.allocation_ratio),
                "profit_score": str(executor.latest_decision.profit_score) if executor.latest_decision.profit_score is not None else None,
                "risk_score": str(executor.latest_decision.risk_score) if executor.latest_decision.risk_score is not None else None,
                "opportunity_score": str(executor.latest_decision.opportunity_score) if executor.latest_decision.opportunity_score is not None else None,
                "allocation_score": str(executor.latest_decision.allocation_score) if executor.latest_decision.allocation_score is not None else None,
                "expected_profit": str(executor.latest_decision.expected_profit) if executor.latest_decision.expected_profit is not None else None,
                "expected_margin_pct": str(executor.latest_decision.expected_margin_pct) if executor.latest_decision.expected_margin_pct is not None else None,
                "confidence": executor.latest_decision.confidence.value,
                "provenance_type": executor.latest_decision.provenance_type.value,
                "explanation": executor.latest_decision.explanation,
                "unknowns": list(executor.latest_decision.unknowns),
                "conditions": list(executor.latest_decision.conditions),
                "loop_status": loop_res.status,
            },
            trace=loop_res.trace,
            errors=loop_res.errors,
            finished_at=datetime.now(timezone.utc),
        )

        if self.mission_repository:
            self.mission_repository.save_result(res)

        return res, executor
