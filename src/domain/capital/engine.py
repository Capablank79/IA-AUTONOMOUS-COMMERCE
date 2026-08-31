from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict, Any, Mapping, Sequence
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.opportunity.models import Opportunity, OpportunityReadiness
from src.domain.supplier_intelligence.models import (
    SupplierCandidate,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    SupplierRiskProfile,
    EvidenceProvenanceType,
    RiskLevel,
    QuoteFreshness,
)
from src.domain.profit.models import (
    EconomicEvaluationResult,
    UnitEconomics,
    EconomicScenarioType,
    ProfitStatus,
)
from .models import (
    AllocationStatus,
    AllocationDecisionReason,
    CapitalBudget,
    CapitalExposure,
    CapitalDownsideAnalysis,
    AllocationPolicy,
    AllocationDecision,
    CapitalAllocation,
    AllocationHistoryEntry,
)


class CapitalAllocationEngine:
    """
    Motor determinista, explicable y seguro de asignación de capital (D-02).
    
    Responde formalmente a:
    "Dada una oportunidad económicamente evaluada, ¿cuánto capital puede asignarse de forma
    prudente, cuál es la exposición máxima permitida, cuánto capital debe permanecer protegido
    y qué condiciones deben cumplirse antes de comprometer capital?"
    """

    @classmethod
    def calculate_exposure(
        cls,
        opportunity_id: str,
        budget: CapitalBudget,
        policy: AllocationPolicy,
        existing_exposure: Decimal = Decimal("0"),
    ) -> CapitalExposure:
        """
        Calcula los límites y capacidad de exposición determinista para una oportunidad.
        """
        # Calcular límite porcentual sobre el allocatable capital del budget
        pct_cap = budget.allocatable_capital * policy.max_exposure_per_opportunity_pct
        
        if policy.max_exposure_absolute_amount is not None:
            max_allowed = min(pct_cap, policy.max_exposure_absolute_amount)
        else:
            max_allowed = pct_cap

        return CapitalExposure(
            opportunity_id=opportunity_id,
            existing_exposure=existing_exposure,
            maximum_allowed_exposure=max_allowed,
            allocatable_budget_capital=budget.allocatable_capital,
            currency=budget.currency,
        )

    @classmethod
    def evaluate_allocation(
        cls,
        opportunity: Opportunity,
        budget: CapitalBudget,
        policy: Optional[AllocationPolicy] = None,
        economic_evaluation: Optional[EconomicEvaluationResult] = None,
        supplier_recommendation: Optional[SupplierRecommendation] = None,
        supplier_risk_profile: Optional[SupplierRiskProfile] = None,
        requested_capital: Optional[Decimal] = None,
        existing_exposure: Decimal = Decimal("0"),
        decision_id: Optional[str] = None,
    ) -> AllocationDecision:
        """
        Evalúa determinísticamente la asignación de capital combinando:
        - Capital Budget & Reserved Capital
        - Maximum Exposure por oportunidad y global
        - Unit Economics & Profit Margins (D-01)
        - Supplier Risk & Reliability (C-03, C-04)
        - Opportunity Confidence & Evidence Sufficiency (B-01 a B-05)
        - Detección estricta de UNKNOWNs y preservación de procedencia
        """
        active_policy = policy or AllocationPolicy()
        dec_id = decision_id or f"DEC-CAP-{opportunity.product_id}-{int(datetime.now(timezone.utc).timestamp())}"
        currency = budget.currency
        conditions: List[str] = []
        unknowns: List[str] = []

        # 1. Determinar capital solicitado
        # Si no se provee explícitamente, se deriva del MOQ o escenario económico principal si existe
        derived_requested = requested_capital
        if derived_requested is None:
            if economic_evaluation is not None and economic_evaluation.primary_unit_economics.landed_cost.total_landed_cost is not None:
                derived_requested = economic_evaluation.primary_unit_economics.landed_cost.total_landed_cost
            else:
                derived_requested = Decimal("0")
                unknowns.append("REQUESTED_CAPITAL_NOT_SPECIFIED")

        if derived_requested < Decimal("0"):
            raise ValueError("requested_capital cannot be negative")

        # 2. Calcular Exposición
        exposure = cls.calculate_exposure(
            opportunity_id=opportunity.product_id,
            budget=budget,
            policy=active_policy,
            existing_exposure=existing_exposure,
        )

        # 3. Extraer señales económicas (D-01)
        has_complete_economics = False
        net_margin_pct: Optional[Decimal] = None
        gross_margin_pct: Optional[Decimal] = None
        expected_profit: Optional[Decimal] = None
        profit_status = ProfitStatus.PROFIT_UNKNOWN
        economic_confidence = Confidence.UNKNOWN

        if economic_evaluation is not None:
            econ = economic_evaluation.primary_unit_economics
            profit_status = economic_evaluation.overall_status
            economic_confidence = economic_evaluation.overall_confidence
            expected_profit = econ.net_profit if econ.net_profit is not None else econ.gross_profit
            net_margin_pct = econ.net_margin_pct
            gross_margin_pct = econ.gross_margin_pct
            has_complete_economics = (profit_status == ProfitStatus.PROFIT_COMPLETE)
            
            # Recolectar unknowns económicos
            for u in econ.unknowns:
                if u not in unknowns:
                    unknowns.append(u)
        else:
            unknowns.append("ECONOMIC_EVALUATION_MISSING")

        # 4. Extraer señales de Proveedor y Riesgo (C-03, C-04)
        supplier_id: Optional[str] = None
        supplier_risk_score: Optional[Decimal] = None
        supplier_confidence = Confidence.UNKNOWN
        is_supplier_valid = True

        if supplier_recommendation is not None:
            if supplier_recommendation.primary_supplier is not None:
                supplier_id = supplier_recommendation.primary_supplier.supplier_id
                supplier_confidence = supplier_recommendation.primary_supplier.confidence
                supplier_risk_score = supplier_recommendation.primary_supplier.overall_risk_score
            if supplier_recommendation.decision in (
                SupplierRecommendationDecision.REJECT,
                SupplierRecommendationDecision.NO_RECOMMENDATION,
            ):
                is_supplier_valid = False
                conditions.append("Rechazo previo de recomendación de proveedor")
        elif supplier_risk_profile is not None:
            supplier_id = supplier_risk_profile.supplier_id
            supplier_risk_score = supplier_risk_profile.overall_risk_score
            supplier_confidence = supplier_risk_profile.confidence
            if supplier_risk_profile.overall_risk_level == RiskLevel.HIGH:
                conditions.append("Supplier evaluated as HIGH risk")

        # 5. Extraer señales de Oportunidad (Hito B)
        opp_score = opportunity.score
        opp_confidence = opportunity.confidence
        opp_readiness = opportunity.readiness

        # 6. Scoring multidimensional determinista
        # Profit Score (0-100)
        profit_score: Optional[Decimal] = None
        if net_margin_pct is not None:
            if net_margin_pct <= Decimal("0"):
                profit_score = Decimal("0")
            else:
                # 30% margin -> 100 score
                score_calc = (net_margin_pct / Decimal("30.0")) * Decimal("100.0")
                profit_score = min(Decimal("100.0"), max(Decimal("0.0"), score_calc)).quantize(Decimal("0.1"))
        elif gross_margin_pct is not None:
            score_calc = (gross_margin_pct / Decimal("40.0")) * Decimal("100.0")
            profit_score = min(Decimal("100.0"), max(Decimal("0.0"), score_calc)).quantize(Decimal("0.1"))

        # Risk Score (0-100, donde 100 es máximo riesgo)
        risk_score: Optional[Decimal] = None
        if supplier_risk_score is not None:
            risk_score = supplier_risk_score
        elif supplier_recommendation is not None and supplier_recommendation.primary_supplier:
            risk_score = supplier_recommendation.primary_supplier.overall_risk_score or Decimal("40.0")
        else:
            risk_score = Decimal("50.0")

        # Allocation Score (0-100) combinando profit, opportunity score y riesgo invertido
        allocation_score: Optional[Decimal] = None
        if profit_score is not None and opp_score is not None and risk_score is not None:
            safety_score = Decimal("100.0") - risk_score
            weighted = (profit_score * Decimal("0.4")) + (opp_score * Decimal("0.3")) + (safety_score * Decimal("0.3"))
            allocation_score = weighted.quantize(Decimal("0.1"))

        # 7. Síntesis de Confianza Global
        # Determinada como el mínimo de las confianzas para no sobrestimar
        conf_rank = {Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1, Confidence.UNKNOWN: 0}
        min_conf_val = min(
            conf_rank.get(opp_confidence, 0),
            conf_rank.get(economic_confidence, 0) if economic_evaluation is not None else 0,
            conf_rank.get(supplier_confidence, 0) if (supplier_recommendation or supplier_risk_profile) else 0,
        )
        inv_conf = {3: Confidence.HIGH, 2: Confidence.MEDIUM, 1: Confidence.LOW, 0: Confidence.UNKNOWN}
        overall_confidence = inv_conf.get(min_conf_val, Confidence.UNKNOWN)

        # 8. Provenance
        provenance = EvidenceProvenanceType.DERIVED
        if economic_evaluation is not None and economic_evaluation.primary_unit_economics.provenance_type == EvidenceProvenanceType.LIVE:
            provenance = EvidenceProvenanceType.LIVE
        elif economic_evaluation is not None and economic_evaluation.primary_unit_economics.provenance_type == EvidenceProvenanceType.FIXTURE:
            provenance = EvidenceProvenanceType.FIXTURE

        # 9. REGLAS DE DECISIÓN DETERMINISTA DE CAPITAL
        decision_status: AllocationStatus
        decision_reason: AllocationDecisionReason
        approved_capital: Decimal = Decimal("0")
        explanation_lines: List[str] = []

        # Comprobación A: Bloqueos críticos por falta de evidencia o condiciones no viables
        if profit_status == ProfitStatus.NOT_COMPARABLE_CURRENCY:
            decision_status = AllocationStatus.REJECTED
            decision_reason = AllocationDecisionReason.CURRENCY_MISMATCH_NO_FX
            explanation_lines.append("Allocation rejected: Currency mismatch without valid exchange rate.")
        elif not is_supplier_valid:
            decision_status = AllocationStatus.REJECTED
            decision_reason = AllocationDecisionReason.EXCESSIVE_SUPPLIER_OR_MARKET_RISK
            explanation_lines.append("Allocation rejected: Supplier recommendation invalid or high risk.")
        elif risk_score is not None and risk_score > active_policy.max_risk_score_allowed:
            decision_status = AllocationStatus.REJECTED
            decision_reason = AllocationDecisionReason.EXCESSIVE_SUPPLIER_OR_MARKET_RISK
            explanation_lines.append(f"Allocation rejected: Risk score ({risk_score}) exceeds policy limit ({active_policy.max_risk_score_allowed}).")
        elif net_margin_pct is not None and net_margin_pct < active_policy.min_net_margin_pct:
            decision_status = AllocationStatus.REJECTED
            decision_reason = AllocationDecisionReason.NEGATIVE_OR_INSUFFICIENT_MARGIN
            explanation_lines.append(f"Allocation rejected: Net margin ({net_margin_pct:.2f}%) below minimum required ({active_policy.min_net_margin_pct}%).")
        elif budget.allocatable_capital <= Decimal("0"):
            decision_status = AllocationStatus.REJECTED
            decision_reason = AllocationDecisionReason.INSUFFICIENT_AVAILABLE_CAPITAL
            explanation_lines.append("Allocation rejected: Zero allocatable capital remaining in budget.")
        elif not has_complete_economics and active_policy.require_known_economics:
            # Falta evidencia económica crítica (ej: shipping o landed cost desconocido)
            if active_policy.allow_partial_allocation and budget.allocatable_capital > Decimal("0") and overall_confidence in (Confidence.LOW, Confidence.MEDIUM):
                # Se puede otorgar una asignación muy limitada o de prueba si la política lo autoriza
                limited_cap = min(
                    budget.allocatable_capital * active_policy.limited_allocation_cap_pct,
                    exposure.remaining_opportunity_capacity,
                    derived_requested,
                )
                if limited_cap > Decimal("0"):
                    decision_status = AllocationStatus.LIMITED_ALLOCATION
                    decision_reason = AllocationDecisionReason.LOW_CONFIDENCE_REQUIRES_INVESTIGATION
                    approved_capital = limited_cap
                    conditions.append("Requires cost investigation before final execution")
                    explanation_lines.append(f"Limited allocation granted ({approved_capital} {currency}) due to incomplete economics / investigation required.")
                else:
                    decision_status = AllocationStatus.NEEDS_INVESTIGATION
                    decision_reason = AllocationDecisionReason.INSUFFICIENT_ECONOMIC_EVIDENCE
                    explanation_lines.append("Allocation held: Incomplete economic evidence requires investigation.")
            else:
                decision_status = AllocationStatus.NEEDS_INVESTIGATION
                decision_reason = AllocationDecisionReason.INSUFFICIENT_ECONOMIC_EVIDENCE
                explanation_lines.append("Allocation held: Critical economic components are UNKNOWN.")
        else:
            # Economics completos o política permisiva
            # Comprobar techo de exposición y presupuesto disponible
            effective_ceiling = exposure.effective_available_ceiling

            if effective_ceiling <= Decimal("0"):
                decision_status = AllocationStatus.REJECTED
                if exposure.remaining_opportunity_capacity <= Decimal("0"):
                    decision_reason = AllocationDecisionReason.CAPPED_BY_MAXIMUM_EXPOSURE
                    explanation_lines.append("Allocation rejected: Opportunity has reached its maximum exposure ceiling.")
                else:
                    decision_reason = AllocationDecisionReason.INSUFFICIENT_AVAILABLE_CAPITAL
                    explanation_lines.append("Allocation rejected: No allocatable capital left in budget.")
            elif derived_requested <= effective_ceiling:
                # Se aprueba el 100% de lo solicitado
                decision_status = AllocationStatus.APPROVED
                decision_reason = AllocationDecisionReason.APPROVED_FULL_BUDGET
                approved_capital = derived_requested
                explanation_lines.append(f"Full allocation approved for {approved_capital} {currency}.")
            else:
                # Requested > effective_ceiling -> Asignación Parcial si está permitida
                if active_policy.allow_partial_allocation:
                    decision_status = AllocationStatus.PARTIALLY_APPROVED
                    approved_capital = effective_ceiling
                    if exposure.remaining_opportunity_capacity < budget.allocatable_capital:
                        decision_reason = AllocationDecisionReason.CAPPED_BY_MAXIMUM_EXPOSURE
                        explanation_lines.append(
                            f"Partially approved for {approved_capital} {currency}: Capped by opportunity maximum exposure ({exposure.maximum_allowed_exposure} {currency})."
                        )
                    else:
                        decision_reason = AllocationDecisionReason.CAPPED_BY_AVAILABLE_CAPITAL
                        explanation_lines.append(
                            f"Partially approved for {approved_capital} {currency}: Capped by total allocatable budget ({budget.allocatable_capital} {currency})."
                        )
                else:
                    decision_status = AllocationStatus.REJECTED
                    decision_reason = AllocationDecisionReason.INSUFFICIENT_AVAILABLE_CAPITAL
                    explanation_lines.append("Allocation rejected: Requested capital exceeds ceiling and partial allocation is disabled.")

        unapproved_capital = derived_requested - approved_capital if derived_requested > approved_capital else Decimal("0")
        allocation_ratio = (approved_capital / derived_requested) if derived_requested > Decimal("0") else Decimal("0")
        remaining_allocatable = budget.allocatable_capital - approved_capital

        # 10. Downside Analysis y Escenarios
        # Downside determinista: capital en riesgo = capital aprobado
        downside_unknowns: List[str] = []
        if profit_status != ProfitStatus.PROFIT_COMPLETE:
            downside_unknowns.append("NET_DOWNSIDE_UNCERTAIN_DUE_TO_INCOMPLETE_ECONOMICS")
        
        downside = CapitalDownsideAnalysis(
            capital_at_risk=approved_capital,
            liquidity_constraints=tuple(conditions),
            capital_horizon_days=30 if economic_evaluation is not None else None,
            is_downside_known=len(downside_unknowns) == 0,
            unknowns=tuple(downside_unknowns),
        )

        # Asignaciones por Escenario (D-01 Scenarios)
        scenario_allocs: Dict[EconomicScenarioType, Decimal] = {}
        if economic_evaluation is not None and economic_evaluation.scenarios is not None:
            sc_res = economic_evaluation.scenarios
            # Conservador
            cons_econ = sc_res.conservative_scenario
            if cons_econ.landed_cost.total_landed_cost is not None and cons_econ.net_margin_pct is not None:
                if cons_econ.net_margin_pct >= active_policy.min_net_margin_pct:
                    scenario_allocs[EconomicScenarioType.CONSERVATIVE] = min(cons_econ.landed_cost.total_landed_cost, effective_ceiling)
                else:
                    scenario_allocs[EconomicScenarioType.CONSERVATIVE] = Decimal("0")
            # Base
            base_econ = sc_res.base_scenario
            if base_econ.landed_cost.total_landed_cost is not None:
                scenario_allocs[EconomicScenarioType.BASE] = min(base_econ.landed_cost.total_landed_cost, effective_ceiling)
            # Optimista
            opt_econ = sc_res.optimistic_scenario
            if opt_econ.landed_cost.total_landed_cost is not None:
                scenario_allocs[EconomicScenarioType.OPTIMISTIC] = min(opt_econ.landed_cost.total_landed_cost, effective_ceiling)

        explanation = " ".join(explanation_lines)

        return AllocationDecision(
            decision_id=dec_id,
            opportunity_id=opportunity.product_id,
            supplier_id=supplier_id,
            status=decision_status,
            reason=decision_reason,
            requested_capital=derived_requested,
            approved_capital=approved_capital,
            unapproved_capital=unapproved_capital,
            maximum_allowed_exposure=exposure.maximum_allowed_exposure,
            available_allocatable_capital=budget.allocatable_capital,
            remaining_allocatable_capital=remaining_allocatable,
            currency=currency,
            allocation_ratio=allocation_ratio.quantize(Decimal("0.0001")),
            profit_score=profit_score,
            risk_score=risk_score,
            opportunity_score=opp_score,
            allocation_score=allocation_score,
            expected_profit=expected_profit,
            expected_margin_pct=net_margin_pct if net_margin_pct is not None else gross_margin_pct,
            confidence=overall_confidence,
            provenance_type=provenance,
            downside_analysis=downside,
            scenario_allocations=scenario_allocs,
            conditions=tuple(conditions),
            unknowns=tuple(unknowns),
            explanation=explanation,
        )

    @classmethod
    def create_allocation(
        cls,
        budget: CapitalBudget,
        decision: AllocationDecision,
        allocation_id: Optional[str] = None,
    ) -> Tuple[CapitalAllocation, CapitalBudget]:
        """
        Crea formalmente una CapitalAllocation inmutable y actualiza el CapitalBudget con el nuevo commitment.
        Protege contra doble asignación y sobreasignación de presupuesto.
        """
        if decision.approved_capital > budget.allocatable_capital:
            raise ValueError(
                f"Cannot create allocation: approved capital ({decision.approved_capital}) exceeds budget allocatable capital ({budget.allocatable_capital})"
            )

        alloc_id = allocation_id or f"ALLOC-{decision.opportunity_id}-{int(datetime.now(timezone.utc).timestamp())}"
        
        # Crear asignación
        allocation = CapitalAllocation(
            allocation_id=alloc_id,
            budget_id=budget.budget_id,
            opportunity_id=decision.opportunity_id,
            supplier_id=decision.supplier_id,
            allocated_amount=decision.approved_capital,
            currency=budget.currency,
            status=decision.status,
            decision=decision,
        )

        # Actualizar budget
        updated_budget = budget.with_commitment(decision.approved_capital)

        return allocation, updated_budget

    @classmethod
    def release_allocation(
        cls,
        allocation: CapitalAllocation,
        budget: CapitalBudget,
        reason: str = "Capital released due to invalidation or reassessment",
    ) -> Tuple[CapitalAllocation, CapitalBudget]:
        """
        Libera determinísticamente el capital comprometido en una asignación y actualiza el presupuesto.
        """
        if allocation.status == AllocationStatus.RELEASED:
            return allocation, budget

        released_amount = allocation.allocated_amount
        released_alloc = allocation.release(reason=reason)
        updated_budget = budget.with_release(released_amount)

        return released_alloc, updated_budget

    @classmethod
    def reallocate_capital(
        cls,
        allocation: CapitalAllocation,
        budget: CapitalBudget,
        new_decision: AllocationDecision,
        reason: str = "Capital reallocated to new evaluated amount",
    ) -> Tuple[CapitalAllocation, CapitalBudget]:
        """
        Reasigna el capital modificando el monto asignado y ajustando el presupuesto de forma atómica y determinista.
        """
        diff = new_decision.approved_capital - allocation.allocated_amount

        if diff > Decimal("0"):
            # Requiere más capital
            if diff > budget.allocatable_capital:
                raise ValueError(
                    f"Cannot increase allocation by {diff}: exceeds available budget allocatable capital ({budget.allocatable_capital})"
                )
            updated_budget = budget.with_commitment(diff)
        elif diff < Decimal("0"):
            # Libera parte del capital
            release_amount = abs(diff)
            updated_budget = budget.with_release(release_amount)
        else:
            updated_budget = budget

        reallocated_alloc = allocation.reallocate(
            new_amount=new_decision.approved_capital,
            new_status=new_decision.status,
            new_decision=new_decision,
            reason=reason,
        )

        return reallocated_alloc, updated_budget

    @classmethod
    def reassess_allocation_on_deterioration(
        cls,
        allocation: CapitalAllocation,
        budget: CapitalBudget,
        opportunity: Opportunity,
        new_economic_evaluation: Optional[EconomicEvaluationResult] = None,
        new_supplier_recommendation: Optional[SupplierRecommendation] = None,
        policy: Optional[AllocationPolicy] = None,
        reason: str = "Reassessment triggered by market/supplier deterioration",
    ) -> Tuple[CapitalAllocation, CapitalBudget, AllocationDecision]:
        """
        Reevalúa una asignación activa ante deterioro de profit, aumento de riesgo,
        invalidación de proveedor o reducción de demanda.
        
        Permite el ciclo: ALLOCATED -> INVALIDATED -> REASSESS -> REDUCE / RELEASE / REALLOCATE
        """
        # Calcular el capital liberado temporalmente para evaluar con el presupuesto limpio
        temp_budget = budget.with_release(allocation.allocated_amount) if allocation.allocated_amount > Decimal("0") else budget

        new_decision = cls.evaluate_allocation(
            opportunity=opportunity,
            budget=temp_budget,
            policy=policy,
            economic_evaluation=new_economic_evaluation,
            supplier_recommendation=new_supplier_recommendation,
            requested_capital=allocation.decision.requested_capital,
            existing_exposure=Decimal("0"),
        )

        if new_decision.approved_capital == Decimal("0") or new_decision.status in (AllocationStatus.REJECTED, AllocationStatus.NEEDS_INVESTIGATION):
            # Liberar completamente
            rel_alloc, upd_budget = cls.release_allocation(allocation, budget, reason=f"{reason}: {new_decision.reason.value}")
            return rel_alloc, upd_budget, new_decision
        else:
            # Reasignar al nuevo monto aprobado
            re_alloc, upd_budget = cls.reallocate_capital(allocation, budget, new_decision, reason=f"{reason}: {new_decision.reason.value}")
            return re_alloc, upd_budget, new_decision
