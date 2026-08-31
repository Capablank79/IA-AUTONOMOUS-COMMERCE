from typing import Dict, Any, Optional, List, Mapping, Sequence
from decimal import Decimal
import statistics

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.domain.market_intelligence.models import (
    SearchCriteria,
    Marketplace,
    MarketSnapshot,
    MarketListing,
    MarketEvidence,
    Confidence
)
from src.domain.market_intelligence.ports import (
    MarketplaceDataSource,
    VisitsDataSource,
    ReviewsDataSource
)
from src.domain.market_intelligence.services import (
    MarketEvidenceComposer,
    DemandIntelligenceService,
    MarketAnalysisService
)
from src.domain.opportunity.engine import OpportunityEngine
from src.domain.opportunity.models import (
    BestKnownOpportunity,
    OpportunityProgress,
    OpportunityExplanation,
    CompletionPolicy,
    Opportunity,
    RejectionReason,
    EvidenceSufficiency,
    OpportunityReadiness
)

class MarketDiscoveryActionExecutor(ActionExecutor):
    """
    ActionExecutor para misiones de descubrimiento comercial autónomo de mercado.
    Implementa acciones de exploración (búsqueda de catálogo, queries, snapshots),
    explotación / profundización (visitas de tráfico, reviews, tendencias) y
    evaluación determinista de candidatos sin hardcodear herramientas.
    """

    def __init__(
        self,
        marketplace_data_source: Optional[MarketplaceDataSource] = None,
        visits_data_source: Optional[VisitsDataSource] = None,
        reviews_data_source: Optional[ReviewsDataSource] = None,
        trends_data_source: Optional[Any] = None,
        trend_service: Optional[Any] = None,
        evidence_composer: Optional[MarketEvidenceComposer] = None,
        demand_intelligence: Optional[DemandIntelligenceService] = None,
        analysis_service: Optional[MarketAnalysisService] = None,
        opportunity_engine: Optional[OpportunityEngine] = None,
        completion_policy: Optional[CompletionPolicy] = None
    ):
        self.marketplace_data_source = marketplace_data_source
        self.visits_data_source = visits_data_source
        self.reviews_data_source = reviews_data_source
        self.trends_data_source = trends_data_source
        self.trend_service = trend_service
        self.evidence_composer = evidence_composer or MarketEvidenceComposer()
        self.demand_intelligence = demand_intelligence or DemandIntelligenceService()
        self.analysis_service = analysis_service or MarketAnalysisService()
        self.opportunity_engine = opportunity_engine or OpportunityEngine()
        self.completion_policy = completion_policy or CompletionPolicy()

        # Cache interno in-memory por instancia de ejecución
        self._cached_listings: Dict[str, MarketListing] = {}
        self._cached_evidences: Dict[str, MarketEvidence] = {}
        self._cached_opportunities: Dict[str, Opportunity] = {}
        self._evaluated_scores: Dict[str, Decimal] = {}
        self._external_calls_count: int = 0

    @property
    def external_calls_count(self) -> int:
        return self._external_calls_count

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        """
        Ejecuta dinámicamente la acción solicitada por el agente de decisión.
        Soporta modos de operación basados en parámetros o targets:
        - EXPLORE / SEARCH (Snapshot de queries o categorías)
        - DEEPEN / INVESTIGATE (Enriquecimiento con Visitas / Reviews / Tendencias)
        - EVALUATE / COMPARE (Scoring determinista y ranking)
        - PROMOTE / PIVOT (Actualización de target o cambio de estrategia)
        """
        action = decision.action
        params = decision.parameters
        target = decision.target or state.current_target

        op_type = params.get("operation") or params.get("type")

        # 1. Comparar oportunidades
        if op_type in ["COMPARE", "OPPORTUNITY_COMPARISON"]:
            item_a = params.get("item_a") or target
            item_b = params.get("item_b")
            additional = params.get("additional_items") or []
            return self._execute_compare(item_a=item_a, item_b=item_b, additional_items=additional)

        # 2. Rechazar oportunidad
        if op_type in ["REJECT", "REJECT_OPPORTUNITY"] or (action == LoopAction.REJECT and params.get("rejection_reason")):
            item_id = params.get("item_id") or target
            return self._execute_reject(item_id=item_id, params=params)

        # 3. Promover candidato o evaluar ranking general
        if action == LoopAction.PROMOTE or op_type in ["PROMOTE", "EVALUATE", "RANKING"]:
            item_id = params.get("item_id") or target
            return self._execute_promote_or_evaluate(item_id=item_id, params=params)

        # 2. Exploración / Búsqueda en Marketplace
        if op_type in ["EXPLORE", "SEARCH", "MARKET_SEARCH"] or (
            action == LoopAction.CONTINUE and target and not self._is_listing_id(target) and not op_type
        ) or (action == LoopAction.PIVOT and target and not self._is_listing_id(target)):
            query = params.get("query") or target or state.goal
            category = params.get("category")
            limit = int(params.get("limit", 20))
            return self._execute_explore(query=query, category=category, limit=limit)

        # 3. Profundización / Explotación (Traffic / Visits / Reviews / Trends)
        if op_type in ["DEEPEN", "INVESTIGATE", "FETCH_TRAFFIC", "FETCH_REVIEWS"] or (
            action == LoopAction.CONTINUE and target and self._is_listing_id(target)
        ) or (target and self._is_listing_id(target)):
            item_id = params.get("item_id") or target
            return self._execute_investigate(item_id=item_id, params=params)

        # 4. Fallback genérico de ejecución
        return {
            "status": "EXECUTED",
            "action": action.value,
            "target": target,
            "message": f"Action {action.value} acknowledged for target {target}"
        }

    def _is_listing_id(self, target: Optional[str]) -> bool:
        if not target:
            return False
        # Listings típicos de Mercado Libre empiezan con MLC, MLB, MLA, etc. o son alfanuméricos directos
        return any(target.startswith(prefix) for prefix in ["MLC", "MLA", "MLB", "MLM", "MCO", "ITEM-", "GENERIC-"])

    def _execute_explore(self, query: str, category: Optional[str], limit: int) -> Dict[str, Any]:
        if not self.marketplace_data_source:
            return {
                "status": "UNAVAILABLE",
                "error": "MarketplaceDataSource not configured"
            }

        self._external_calls_count += 1
        criteria = SearchCriteria(
            query=query,
            marketplace=Marketplace.MERCADO_LIBRE,
            category=category,
            limit=limit
        )

        snapshot = self.marketplace_data_source.fetch_snapshot(criteria)
        found_listings = []
        for listing in snapshot.listings:
            self._cached_listings[listing.external_id] = listing
            
            # Componer evidencia base inicial
            evidence = self.evidence_composer.compose(listing=listing)
            self._cached_evidences[listing.external_id] = evidence
            
            # Score preliminar
            score = self.opportunity_engine.calculate_deterministic_market_score(evidence)
            self._evaluated_scores[listing.external_id] = score

            # Registrar modelo Opportunity con suficiencia y readiness inicial
            opp = self.opportunity_engine.create_opportunity(evidence=evidence)
            self._cached_opportunities[listing.external_id] = opp

            found_listings.append({
                "item_id": listing.external_id,
                "title": listing.title,
                "price": float(listing.price.amount),
                "currency": listing.price.currency,
                "sold_quantity": listing.sold_quantity,
                "preliminary_score": float(score),
                "evidence_sufficiency": opp.evidence_sufficiency.value,
                "readiness": opp.readiness.value
            })

        # Ordenar por score preliminar
        found_listings.sort(key=lambda x: x["preliminary_score"], reverse=True)

        return {
            "status": "SUCCESS",
            "operation": "EXPLORE",
            "query": query,
            "category": category,
            "snapshot_id": snapshot.snapshot_id,
            "total_results": snapshot.total_results,
            "listings_count": len(found_listings),
            "top_candidates": found_listings[:5],
            "all_item_ids": [item["item_id"] for item in found_listings]
        }

    def _execute_investigate(self, item_id: Optional[str], params: Mapping[str, Any]) -> Dict[str, Any]:
        if not item_id:
            return {"status": "FAILED", "error": "item_id required for investigation"}

        listing = self._cached_listings.get(item_id)
        if not listing:
            return {"status": "FAILED", "error": f"Listing {item_id} not found in current exploration cache"}

        current_evidence = self._cached_evidences.get(item_id) or self.evidence_composer.compose(listing=listing)
        signals_added = []

        # A. Visits / Traffic Signal
        visit_signal = None
        if self.visits_data_source:
            try:
                self._external_calls_count += 1
                window_days = int(params.get("window_days", 30))
                visit_signal = self.visits_data_source.get_visits(item_id=item_id, window_days=window_days)
                signals_added.append("VISIT_SIGNAL")
            except Exception as e:
                signals_added.append(f"VISIT_SIGNAL_ERROR: {str(e)}")

        # B. Reviews Signal
        review_signal = None
        if self.reviews_data_source:
            try:
                self._external_calls_count += 1
                review_signal = self.reviews_data_source.get_reviews(item_id=item_id)
                signals_added.append("REVIEW_SIGNAL")
            except Exception as e:
                signals_added.append(f"REVIEW_SIGNAL_ERROR: {str(e)}")

        # C. Trend Signal
        trend_signals = list(current_evidence.trend_signals)
        if self.trends_data_source and not trend_signals:
            try:
                self._external_calls_count += 1
                trends = self.trends_data_source.get_trends()
                if trends:
                    trend_sig = self.analysis_service._calculate_trend(listing.title, trends)
                    if trend_sig.matched:
                        trend_signals.append(trend_sig)
                        signals_added.append("TREND_SIGNAL")
            except Exception as e:
                signals_added.append(f"TREND_SIGNAL_ERROR: {str(e)}")

        # D. Recomponer evidencia
        traffic_signals = [visit_signal] if visit_signal else list(current_evidence.traffic_signals)
        review_signals = [review_signal] if review_signal else list(current_evidence.review_signals)

        # Determinar nivel de confianza
        confidence = Confidence.UNKNOWN
        if traffic_signals and review_signals:
            confidence = Confidence.HIGH
        elif traffic_signals or review_signals:
            confidence = Confidence.MEDIUM
        elif listing.sold_quantity is not None:
            confidence = Confidence.LOW

        enriched_evidence = MarketEvidence(
            listing=listing,
            traffic_signals=traffic_signals,
            trend_signals=trend_signals,
            price_signals=list(current_evidence.price_signals),
            demand_signals=list(current_evidence.demand_signals),
            review_signals=review_signals,
            confidence=confidence
        )
        
        # Calcular señal de demanda actualizada
        demand_signal = self.demand_intelligence.calculate(enriched_evidence)
        enriched_evidence = self.evidence_composer.compose(
            listing=listing,
            visit_signal=traffic_signals[0] if traffic_signals else None,
            demand_signal=demand_signal
        )
        enriched_evidence = MarketEvidence(
            listing=enriched_evidence.listing,
            traffic_signals=enriched_evidence.traffic_signals,
            trend_signals=trend_signals,
            price_signals=list(current_evidence.price_signals),
            demand_signals=enriched_evidence.demand_signals,
            review_signals=review_signals,
            confidence=confidence
        )

        self._cached_evidences[item_id] = enriched_evidence

        # Actualizar o reevaluar modelo Opportunity
        existing_opp = self._cached_opportunities.get(item_id)
        if existing_opp:
            updated_opp = self.opportunity_engine.reevaluate_opportunity(
                current_opportunity=existing_opp,
                new_evidence=enriched_evidence,
                reason=f"Deepened with signals: {', '.join(signals_added)}"
            )
        else:
            updated_opp = self.opportunity_engine.create_opportunity(evidence=enriched_evidence)
        self._cached_opportunities[item_id] = updated_opp

        # Calcular nuevo score determinista enriquecido
        new_score = updated_opp.score or Decimal("0.0")
        self._evaluated_scores[item_id] = new_score

        explanation = updated_opp.explanation or self.opportunity_engine.generate_explanation(enriched_evidence, score=new_score)

        return {
            "status": "SUCCESS",
            "operation": "INVESTIGATE",
            "item_id": item_id,
            "title": listing.title,
            "signals_added": signals_added,
            "confidence": confidence.value,
            "evidence_sufficiency": updated_opp.evidence_sufficiency.value,
            "readiness": updated_opp.readiness.value,
            "updated_score": float(new_score),
            "visits": visit_signal.total_visits if visit_signal else None,
            "reviews_count": review_signal.total_reviews if review_signal else 0,
            "rating": review_signal.average_rating if review_signal else 0.0,
            "why_winner": explanation.why_winner,
            "risks": list(explanation.risks),
            "unknowns": list(explanation.unknowns),
            "history_count": len(updated_opp.history)
        }

    def _execute_compare(self, item_a: Optional[str], item_b: Optional[str], additional_items: Sequence[str] = ()) -> Dict[str, Any]:
        if not item_a or not item_b:
            return {"status": "FAILED", "error": "Both item_a and item_b are required for comparison"}

        opp_a = self._cached_opportunities.get(item_a) or self._cached_evidences.get(item_a)
        opp_b = self._cached_opportunities.get(item_b) or self._cached_evidences.get(item_b)

        if not opp_a or not opp_b:
            return {"status": "FAILED", "error": f"One or both items ({item_a}, {item_b}) not found in cache"}

        extra_opps = [self._cached_opportunities.get(i) or self._cached_evidences.get(i) for i in additional_items if i in self._cached_opportunities or i in self._cached_evidences]

        comparison = self.opportunity_engine.compare_opportunities(opp_a, opp_b, *extra_opps)

        return {
            "status": "SUCCESS",
            "operation": "COMPARE",
            "winner_id": comparison.winner_id,
            "compared_count": len(comparison.compared_opportunities),
            "summary_rationale": comparison.summary_rationale,
            "dimensions": [
                {
                    "name": d.dimension_name,
                    "winner_id": d.winner_id,
                    "scores": {k: float(v) if isinstance(v, Decimal) else v for k, v in d.scores_by_opportunity.items()},
                    "rationale": d.rationale
                } for d in comparison.dimensions
            ]
        }

    def _execute_reject(self, item_id: Optional[str], params: Mapping[str, Any]) -> Dict[str, Any]:
        if not item_id:
            return {"status": "FAILED", "error": "item_id required for rejection"}

        evidence = self._cached_evidences.get(item_id)
        if not evidence:
            return {"status": "FAILED", "error": f"Listing {item_id} not found in cache"}

        raw_reason = str(params.get("rejection_reason") or params.get("reason") or "OTHER")
        try:
            reason = RejectionReason(raw_reason)
        except ValueError:
            reason = RejectionReason.OTHER

        details = str(params.get("details") or params.get("explanation") or f"Rejected due to {reason.value}")
        
        decision = self.opportunity_engine.reject_opportunity(evidence=evidence, reason=reason, details=details)
        
        # Actualizar o registrar en modelo Opportunity
        opp = self.opportunity_engine.create_opportunity(evidence=evidence)
        self._cached_opportunities[item_id] = opp

        return {
            "status": "SUCCESS",
            "operation": "REJECT",
            "item_id": item_id,
            "readiness": decision.readiness.value,
            "rejection_reason": decision.rejection.reason.value if decision.rejection else reason.value,
            "rejection_details": decision.rejection.details if decision.rejection else details,
            "confidence": decision.confidence.value
        }

    def _execute_promote_or_evaluate(self, item_id: Optional[str], params: Mapping[str, Any]) -> Dict[str, Any]:
        if not item_id or item_id not in self._cached_evidences:
            # Si no hay item específico, retornar ranking consolidado de todos los candidatos conocidos
            ranking = []
            for i_id, evidence in self._cached_evidences.items():
                sc = self._evaluated_scores.get(i_id, Decimal("0.0"))
                opp = self._cached_opportunities.get(i_id)
                ranking.append({
                    "item_id": i_id,
                    "title": evidence.listing.title,
                    "score": float(sc),
                    "confidence": evidence.confidence.value,
                    "evidence_sufficiency": opp.evidence_sufficiency.value if opp else EvidenceSufficiency.INSUFFICIENT.value,
                    "readiness": opp.readiness.value if opp else OpportunityReadiness.INSUFFICIENT_EVIDENCE.value
                })
            ranking.sort(key=lambda x: x["score"], reverse=True)
            return {
                "status": "SUCCESS",
                "operation": "RANKING",
                "evaluated_count": len(ranking),
                "ranking": ranking
            }

        evidence = self._cached_evidences[item_id]
        score = self._evaluated_scores.get(item_id, self.opportunity_engine.calculate_deterministic_market_score(evidence))
        opp = self._cached_opportunities.get(item_id) or self.opportunity_engine.create_opportunity(evidence=evidence)
        explanation = opp.explanation or self.opportunity_engine.generate_explanation(evidence, score=score)

        return {
            "status": "SUCCESS",
            "operation": "PROMOTE",
            "item_id": item_id,
            "title": evidence.listing.title,
            "score": float(score),
            "confidence": evidence.confidence.value,
            "evidence_sufficiency": opp.evidence_sufficiency.value,
            "readiness": opp.readiness.value,
            "why_winner": explanation.why_winner,
            "observed_evidence": list(explanation.observed_evidence),
            "derived_signals": list(explanation.derived_signals),
            "risks": list(explanation.risks),
            "unknowns": list(explanation.unknowns),
            "recommended_action": explanation.recommended_action
        }

    def get_all_evidences(self) -> List[MarketEvidence]:
        return list(self._cached_evidences.values())

    def get_all_opportunities(self) -> List[Opportunity]:
        return list(self._cached_opportunities.values())

    def get_opportunity(self, item_id: str) -> Optional[Opportunity]:
        return self._cached_opportunities.get(item_id)

    def get_best_candidate(self) -> Optional[BestKnownOpportunity]:
        if not self._cached_evidences:
            return None

        best_id = max(self._evaluated_scores, key=lambda k: self._evaluated_scores[k])
        best_evidence = self._cached_evidences[best_id]
        best_score = self._evaluated_scores[best_id]
        explanation = self.opportunity_engine.generate_explanation(best_evidence, score=best_score)

        return BestKnownOpportunity(
            product_id=best_id,
            title=best_evidence.listing.title,
            score=best_score,
            confidence=best_evidence.confidence,
            evidence=best_evidence,
            iteration=1,
            explanation=explanation
        )
