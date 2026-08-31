from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
from decimal import Decimal

from src.domain.mission.models import (
    Mission,
    MissionStatus,
    MissionResult,
    MissionType,
    MissionTraceEntry,
    LoopState,
    LoopAction,
    LoopDecision,
)
from src.domain.mission.ports import DecisionProvider, MissionRepository
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits, LoopResult
from src.application.market_intelligence.market_discovery_action_executor import MarketDiscoveryActionExecutor
from src.domain.market_intelligence.ports import (
    MarketplaceDataSource,
    VisitsDataSource,
    ReviewsDataSource,
)
from src.domain.opportunity.engine import OpportunityEngine
from src.domain.opportunity.models import (
    BestKnownOpportunity,
    OpportunityProgress,
    CompletionPolicy,
)

class AutonomousMarketDiscoveryService:
    """
    Servicio de orquestación para el bucle autónomo de descubrimiento de oportunidades de mercado.
    Integra el AutonomousLoop agnóstico con el MarketDiscoveryActionExecutor, el OpportunityEngine
    y el DecisionProvider (LLM o heurístico/scripted).
    
    Gestiona el ciclo cognitivo:
    MISSION -> OBSERVE -> EVALUATE -> DECIDE -> ACT -> OBSERVE -> UPDATE STATE -> MEASURE PROGRESS -> CONVERGENCE
    """

    def __init__(
        self,
        decision_provider: DecisionProvider,
        marketplace_data_source: Optional[MarketplaceDataSource] = None,
        visits_data_source: Optional[VisitsDataSource] = None,
        reviews_data_source: Optional[ReviewsDataSource] = None,
        trends_data_source: Optional[Any] = None,
        opportunity_engine: Optional[OpportunityEngine] = None,
        completion_policy: Optional[CompletionPolicy] = None,
        mission_repository: Optional[MissionRepository] = None,
        default_max_iterations: int = 10,
        default_limits: Optional[LoopLimits] = None
    ):
        self.decision_provider = decision_provider
        self.marketplace_data_source = marketplace_data_source
        self.visits_data_source = visits_data_source
        self.reviews_data_source = reviews_data_source
        self.trends_data_source = trends_data_source
        self.opportunity_engine = opportunity_engine or OpportunityEngine()
        self.completion_policy = completion_policy or CompletionPolicy()
        self.mission_repository = mission_repository
        self.default_max_iterations = default_max_iterations
        self.default_limits = default_limits or LoopLimits(max_iterations=default_max_iterations)

    def execute_discovery_mission(
        self,
        query: str,
        mission_id: Optional[str] = None,
        initial_target: Optional[str] = None,
        limits: Optional[LoopLimits] = None
    ) -> MissionResult:
        """
        Ejecuta de principio a fin una misión comercial de descubrimiento autónomo.
        """
        mission_id = mission_id or f"discovery-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        limits = limits or self.default_limits

        executor = MarketDiscoveryActionExecutor(
            marketplace_data_source=self.marketplace_data_source,
            visits_data_source=self.visits_data_source,
            reviews_data_source=self.reviews_data_source,
            trends_data_source=self.trends_data_source,
            opportunity_engine=self.opportunity_engine,
            completion_policy=self.completion_policy
        )

        def completion_validator(state: LoopState) -> Tuple[bool, str]:
            candidates_count = len(executor.get_all_evidences())
            best_known = state.best_known or executor.get_best_candidate()
            return self.opportunity_engine.validate_completion(
                best_known=best_known,
                candidates_count=candidates_count,
                policy=self.completion_policy
            )

        def state_enhancer(state: LoopState, observation: Dict[str, Any]) -> LoopState:
            current_best = executor.get_best_candidate()
            evidences = tuple(executor.get_all_evidences())
            
            # Calcular progreso
            prev_best_score = state.best_known.score if state.best_known and hasattr(state.best_known, "score") else None
            curr_best_score = current_best.score if current_best and hasattr(current_best, "score") else None
            
            improvement = Decimal("0.0")
            if curr_best_score is not None and prev_best_score is not None:
                improvement = curr_best_score - prev_best_score

            evaluated_count = len(evidences)
            coverage_ratio = min(1.0, evaluated_count / 10.0) if evaluated_count > 0 else 0.0

            uncertainty = "HIGH"
            if current_best and current_best.confidence.value == "HIGH":
                uncertainty = "LOW"
            elif current_best and current_best.confidence.value == "MEDIUM":
                uncertainty = "MEDIUM"

            progress = OpportunityProgress(
                previous_best_score=prev_best_score,
                current_best_score=curr_best_score,
                improvement=improvement,
                evidence_coverage=coverage_ratio,
                search_coverage=evaluated_count,
                uncertainty_level=uncertainty,
                iterations_count=state.iteration,
                external_calls_count=executor.external_calls_count
            )

            return LoopState(
                mission_id=state.mission_id,
                iteration=state.iteration,
                goal=state.goal,
                current_target=state.current_target,
                observations=state.observations,
                evidences=evidences,
                decision_history=state.decision_history,
                best_known=current_best,
                progress=progress
            )

        loop = AutonomousLoop(
            decision_provider=self.decision_provider,
            action_executor=executor,
            max_iterations=limits.max_iterations,
            limits=limits,
            completion_validator=completion_validator,
            state_enhancer=state_enhancer
        )

        loop_result: LoopResult = loop.run(
            mission_id=mission_id,
            goal=f"Encontrar las mejores oportunidades de productos para vender en Mercado Libre Chile para la búsqueda '{query}'",
            initial_target=initial_target or query
        )

        # Consolidar ranking final de candidatos
        all_evidences = executor.get_all_evidences()
        ranking = []
        for ev in all_evidences:
            sc = self.opportunity_engine.calculate_deterministic_market_score(ev)
            expl = self.opportunity_engine.generate_explanation(ev, score=sc)
            ranking.append({
                "item_id": ev.listing.external_id,
                "title": ev.listing.title,
                "price": float(ev.listing.price.amount),
                "currency": ev.listing.price.currency,
                "sold_quantity": ev.listing.sold_quantity,
                "opportunity_score": float(sc),
                "confidence": ev.confidence.value,
                "why_winner": expl.why_winner,
                "observed_evidence": list(expl.observed_evidence),
                "derived_signals": list(expl.derived_signals),
                "risks": list(expl.risks),
                "unknowns": list(expl.unknowns),
                "recommended_action": expl.recommended_action
            })

        ranking.sort(key=lambda x: x["opportunity_score"], reverse=True)
        best_known_opp = loop_result.final_state.best_known or executor.get_best_candidate()

        # Determinar status del resultado
        mission_status = MissionStatus.COMPLETED
        if loop_result.status == "ERROR":
            mission_status = MissionStatus.FAILED
        elif loop_result.status in ["CALL_LIMIT_REACHED", "TIME_LIMIT_REACHED", "MAX_ITERATIONS_REACHED"] and not best_known_opp:
            mission_status = MissionStatus.BLOCKED

        # Convertir traza a MissionTraceEntry
        mission_trace = [
            MissionTraceEntry(
                step=f"ITERATION_{t.iteration}_{t.action.value}",
                status=MissionStatus.RUNNING if t.iteration < len(loop_result.trace) else mission_status,
                metadata={
                    "reason": t.reason,
                    "target": t.target,
                    "parameters": dict(t.parameters),
                    "observation": dict(t.observation)
                }
            )
            for t in loop_result.trace
        ]

        output = {
            "mission_id": mission_id,
            "query": query,
            "iterations_used": loop_result.final_state.iteration,
            "termination_reason": loop_result.termination_reason,
            "loop_status": loop_result.status,
            "total_candidates_found": len(all_evidences),
            "best_opportunity": {
                "product_id": best_known_opp.product_id,
                "title": best_known_opp.title,
                "score": float(best_known_opp.score),
                "confidence": best_known_opp.confidence.value,
                "why_winner": best_known_opp.explanation.why_winner if best_known_opp.explanation else "",
                "risks": list(best_known_opp.explanation.risks) if best_known_opp.explanation else [],
                "unknowns": list(best_known_opp.explanation.unknowns) if best_known_opp.explanation else []
            } if best_known_opp else None,
            "top_ranking": ranking[:10],
            "progress": {
                "search_coverage": len(all_evidences),
                "external_calls_count": executor.external_calls_count,
                "improvement": float(loop_result.final_state.progress.improvement) if loop_result.final_state.progress else 0.0,
                "uncertainty_level": loop_result.final_state.progress.uncertainty_level if loop_result.final_state.progress else "UNKNOWN"
            } if loop_result.final_state.progress else None
        }

        mission_result = MissionResult(
            mission_id=mission_id,
            status=mission_status,
            output=output,
            trace=mission_trace,
            evidences=list(all_evidences),
            errors=loop_result.errors,
            finished_at=datetime.now(timezone.utc)
        )

        if self.mission_repository:
            self.mission_repository.save_result(mission_result)

        return mission_result
