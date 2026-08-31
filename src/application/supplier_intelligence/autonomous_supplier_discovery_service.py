from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, List, Tuple, Sequence

from src.domain.mission.models import (
    Mission,
    MissionStatus,
    MissionResult,
    MissionType,
    LoopState,
    LoopAction,
    LoopDecision,
)
from src.domain.mission.ports import DecisionProvider, MissionRepository
from src.domain.opportunity.models import Opportunity
from src.domain.supplier_intelligence.models import (
    SupplierCandidate,
    BestKnownSupplier,
    SupplierReadiness,
    ProductMatchGrade,
)
from src.domain.supplier_intelligence.ports import SupplierSource, SupplierRepository
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits, LoopResult
from src.application.supplier_intelligence.supplier_discovery_action_executor import (
    SupplierDiscoveryActionExecutor,
)


class AutonomousSupplierDiscoveryService:
    """
    Servicio de orquestación de la Misión C-01: Supplier Discovery & Evidence Loop.
    Conecta una oportunidad validada del Hito B ("¿Dónde puedo abastecer este producto?"),
    con el bucle cognitivo autónomo para descubrir, normalizar, contrastar evidencia
    y producir un ranking preliminar determinista con trazabilidad inmutable.
    """

    def __init__(
        self,
        decision_provider: DecisionProvider,
        sources: Sequence[SupplierSource],
        supplier_repository: Optional[SupplierRepository] = None,
        mission_repository: Optional[MissionRepository] = None,
        default_max_iterations: int = 10,
        default_limits: Optional[LoopLimits] = None,
    ):
        self.decision_provider = decision_provider
        self.sources = list(sources)
        self.supplier_repository = supplier_repository
        self.mission_repository = mission_repository
        self.default_max_iterations = default_max_iterations
        self.default_limits = default_limits or LoopLimits(max_iterations=default_max_iterations)

    def execute_supplier_discovery_mission(
        self,
        opportunity: Opportunity,
        mission_id: Optional[str] = None,
        limits: Optional[LoopLimits] = None,
    ) -> MissionResult:
        """
        Ejecuta de principio a fin la misión autónoma de descubrimiento de proveedores
        para una oportunidad específica de mercado.
        """
        mission_id = mission_id or f"sup-disc-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        limits = limits or self.default_limits

        executor = SupplierDiscoveryActionExecutor(
            sources=self.sources,
            repository=self.supplier_repository,
            target_opportunity=opportunity,
        )

        def completion_validator(state: LoopState) -> Tuple[bool, str]:
            candidates = executor.get_all_candidates()
            if not candidates:
                return False, "No supplier candidates discovered yet."
            
            # Verificar si hay al menos un candidato evaluado con match válido
            valid_candidates = [
                c for c in candidates
                if c.product_match.grade in [ProductMatchGrade.EXACT_MATCH, ProductMatchGrade.CLOSE_MATCH, ProductMatchGrade.VARIANT]
                and c.readiness in [SupplierReadiness.EVALUATED, SupplierReadiness.READY_FOR_ECONOMICS]
            ]
            if not valid_candidates:
                return False, "No valid matching supplier candidates have been evaluated."

            if executor.best_known_supplier is None:
                return False, "No best known supplier candidate has been determined."

            return True, f"Supplier discovery validated with {len(valid_candidates)} matching candidates."

        def state_enhancer(state: LoopState, observation: Dict[str, Any]) -> LoopState:
            best_sup = executor.best_known_supplier
            progress_dict = {
                "total_candidates": len(executor.get_all_candidates()),
                "best_supplier_id": best_sup.supplier_id if best_sup else None,
                "best_supplier_score": float(best_sup.score) if best_sup else None,
                "best_supplier_history": executor.best_supplier_history,
            }
            return LoopState(
                mission_id=state.mission_id,
                iteration=state.iteration,
                goal=state.goal,
                current_target=state.current_target,
                observations=state.observations + (observation,),
                evidences=state.evidences,
                decision_history=state.decision_history,
                best_known=best_sup,
                progress=progress_dict,
            )

        loop = AutonomousLoop(
            decision_provider=self.decision_provider,
            action_executor=executor,
            max_iterations=limits.max_iterations,
            limits=limits,
            completion_validator=completion_validator,
            state_enhancer=state_enhancer,
        )

        sku_val = opportunity.provenance.get("sku") if isinstance(opportunity.provenance, dict) or hasattr(opportunity.provenance, "get") else None
        loop_result = loop.run(
            mission_id=mission_id,
            goal=f"Discover and rank verified suppliers for opportunity '{opportunity.title}' (SKU: {sku_val or 'N/A'})",
            initial_target=opportunity.title,
        )

        candidates = executor.get_all_candidates()
        best_supplier = executor.best_known_supplier

        # Mapeo a MissionResult
        mission_status = MissionStatus.COMPLETED if loop_result.status == "COMPLETED" else MissionStatus.FAILED
        
        output_payload = {
            "opportunity_id": opportunity.opportunity_id,
            "opportunity_title": opportunity.title,
            "candidates_count": len(candidates),
            "candidates": [
                {
                    "supplier_id": c.supplier.supplier_id,
                    "name": c.supplier.name,
                    "source": c.supplier.source,
                    "provenance": c.evidence.provenance_type.value,
                    "product_match": c.product_match.grade.value,
                    "score": float(c.score) if c.score else 0.0,
                    "rank": c.rank,
                    "wholesale_price": float(c.evidence.wholesale_price) if c.evidence.wholesale_price else None,
                    "stock_available": c.evidence.stock_available,
                    "moq": c.evidence.minimum_order_quantity,
                    "shipping_cost": float(c.evidence.shipping_cost) if c.evidence.shipping_cost else None,
                    "lead_time_days": c.evidence.lead_time_days,
                    "readiness": c.readiness.value,
                    "unknowns": list(c.unknowns),
                    "risks": list(c.risks),
                }
                for c in candidates
            ],
            "best_known_supplier": {
                "supplier_id": best_supplier.supplier_id,
                "name": best_supplier.name,
                "source": best_supplier.source,
                "score": float(best_supplier.score),
                "product_match": best_supplier.product_match_grade.value,
                "why_best": best_supplier.why_best,
                "iteration": best_supplier.iteration,
            } if best_supplier else None,
            "best_supplier_evolution": executor.best_supplier_history,
            "termination_reason": loop_result.termination_reason,
            "trace_iterations": len(loop_result.trace),
        }

        result = MissionResult(
            mission_id=mission_id,
            status=mission_status,
            output=output_payload,
            errors=loop_result.errors,
            finished_at=datetime.now(timezone.utc),
        )

        if self.mission_repository:
            # Guardar la misión si el repositorio está disponible
            mission_entity = Mission(
                mission_id=mission_id,
                name=f"Supplier Discovery: {opportunity.title[:30]}",
                mission_type=MissionType.MARKET_DISCOVERY,
                status=mission_status,
                goal=f"Discover suppliers for {opportunity.title}",
                created_at=datetime.now(timezone.utc),
                result=result,
            )
            self.mission_repository.save(mission_entity)

        return result
