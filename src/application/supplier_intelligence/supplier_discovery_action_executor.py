from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, List, Sequence, Tuple
import statistics

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.domain.opportunity.models import Opportunity
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierEvidence,
    SupplierCandidate,
    SupplierReadiness,
    SupplierRejectionReason,
    ProductMatchGrade,
    BestKnownSupplier,
    EvidenceProvenanceType,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    ContingencyTrigger,
)
from src.domain.supplier_intelligence.ports import (
    SupplierSource,
    SupplierRepository,
    SupplierDataSource,
)
from src.domain.supplier_intelligence.services import (
    SupplierNormalizer,
    ProductMatcher,
    SupplierScorer,
)


class SupplierDiscoveryActionExecutor(ActionExecutor):
    """
    ActionExecutor para misiones de descubrimiento y recopilación de evidencia de proveedores (Hito C-01).
    Soporta múltiples fuentes (SupplierSource), normalización, product matching,
    scoring determinista, comparación de candidatos, tracking de best_known_supplier y pivot ante fuentes insuficientes.
    """

    def __init__(
        self,
        sources: Sequence[SupplierSource],
        repository: Optional[SupplierRepository] = None,
        target_opportunity: Optional[Opportunity] = None,
    ):
        self.sources = list(sources)
        self.repository = repository
        self.target_opportunity = target_opportunity

        # Cache interno in-memory por ejecución
        self._cached_candidates: Dict[str, SupplierCandidate] = {}
        self._best_known_supplier: Optional[BestKnownSupplier] = None
        self._best_supplier_history: List[Dict[str, Any]] = []
        self._latest_recommendation: Optional[SupplierRecommendation] = None
        self._external_calls_count: int = 0
        self._queried_sources: List[str] = []

    @property
    def latest_recommendation(self) -> Optional[SupplierRecommendation]:
        return self._latest_recommendation

    @property
    def external_calls_count(self) -> int:
        return self._external_calls_count

    @property
    def best_known_supplier(self) -> Optional[BestKnownSupplier]:
        return self._best_known_supplier

    @property
    def best_supplier_history(self) -> List[Dict[str, Any]]:
        return list(self._best_supplier_history)

    def get_all_candidates(self) -> List[SupplierCandidate]:
        return list(self._cached_candidates.values())

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        """
        Ejecuta la acción autónoma solicitada por el DecisionProvider.
        Acciones soportadas:
        - EXPLORE / DISCOVER (Búsqueda en fuentes de proveedores)
        - EVALUATE / RANK (Scoring determinista y ranking)
        - INVESTIGATE / DEEPEN (Recopilación de cotización o detalles de un proveedor)
        - COMPARE (Comparación lado a lado comercial y MOQ)
        - COMPARE_RISK / ASSESS_RISK (Evaluación de confiabilidad, logística y riesgo multidimensional)
        - REJECT (Descarte justificado de un proveedor)
        - PIVOT (Cambio de fuente o de estrategia de búsqueda)
        """
        action = decision.action
        params = decision.parameters
        target = decision.target or state.current_target
        op_type = params.get("operation") or params.get("type")

        # 1. DISCOVER / EXPLORE
        if op_type in ["DISCOVER", "EXPLORE", "SEARCH_SUPPLIERS"] or (
            action == LoopAction.CONTINUE and not self._is_supplier_id(target) and not op_type
        ):
            return self._execute_discover(params=params, state=state, target=target)

        # 2. EVALUATE / RANK
        if op_type in ["EVALUATE", "RANK", "SCORING"] or action == LoopAction.PROMOTE:
            return self._execute_evaluate_and_rank(params=params, state=state)

        # 3. INVESTIGATE
        if op_type in ["INVESTIGATE", "DEEPEN", "FETCH_QUOTE"] or (
            target and self._is_supplier_id(target) and action == LoopAction.CONTINUE
        ):
            supplier_id = params.get("supplier_id") or target
            return self._execute_investigate(supplier_id=supplier_id, params=params, state=state)

        # 4. COMPARE
        if op_type in ["COMPARE", "COMPARE_SUPPLIERS", "COMPARE_QUOTES"]:
            return self._execute_compare(params=params)

        # 4.b COMPARE_RISK / ASSESS_RISK
        if op_type in ["COMPARE_RISK", "ASSESS_RISK", "EVALUATE_RELIABILITY"]:
            return self._execute_compare_risk(params=params, state=state)

        # 4.c RECOMMEND_SUPPLIER / GENERATE_RECOMMENDATION
        if op_type in ["RECOMMEND_SUPPLIER", "GENERATE_RECOMMENDATION", "RECOMMEND"]:
            return self._execute_recommend(params=params, state=state)

        # 4.d REEVALUATE_RECOMMENDATION / CONTINGENCY_PIVOT
        if op_type in ["REEVALUATE_RECOMMENDATION", "CONTINGENCY_PIVOT", "ACTIVATE_FALLBACK"]:
            return self._execute_contingency_pivot(params=params, state=state)

        # 5. REJECT
        if op_type in ["REJECT", "REJECT_SUPPLIER"] or action == LoopAction.REJECT:
            supplier_id = params.get("supplier_id") or target
            return self._execute_reject(supplier_id=supplier_id, params=params)

        # 6. PIVOT
        if action == LoopAction.PIVOT or op_type == "PIVOT":
            return self._execute_pivot(params=params, state=state)

        # Fallback genérico
        return {
            "status": "EXECUTED",
            "action": action.value,
            "target": target,
            "message": f"Supplier action {action.value} executed.",
        }

    def _is_supplier_id(self, target: Optional[str]) -> bool:
        if not target:
            return False
        return target.startswith("SUP-") or target.startswith("SUPPLIER-")

    def _get_target_product_info(self, params: Dict[str, Any], target: Optional[str]) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[Decimal]]:
        query = params.get("query") or target or ""
        brand = params.get("brand")
        model = params.get("model")
        sku = params.get("sku")
        market_price = None

        if self.target_opportunity is not None:
            # Extraer de la oportunidad
            if not query:
                query = self.target_opportunity.title
            if hasattr(self.target_opportunity, "listing") and self.target_opportunity.listing:
                market_price = self.target_opportunity.listing.price.amount
            elif hasattr(self.target_opportunity, "market_evidence") and hasattr(self.target_opportunity.market_evidence, "market_price"):
                market_price = self.target_opportunity.market_evidence.market_price
            
            # Extraer sku/brand si existen en provenance o raw_data
            prov = self.target_opportunity.provenance if hasattr(self.target_opportunity, "provenance") else {}
            if isinstance(prov, dict) or hasattr(prov, "get"):
                if not brand and prov.get("brand"):
                    brand = prov["brand"]
                if not model and prov.get("model"):
                    model = prov["model"]
                if not sku and prov.get("sku"):
                    sku = prov["sku"]

        if params.get("target_market_price"):
            market_price = Decimal(str(params["target_market_price"]))

        return query, brand, model, sku, market_price

    def _execute_discover(self, params: Dict[str, Any], state: LoopState, target: Optional[str]) -> Dict[str, Any]:
        query, brand, model, sku, market_price = self._get_target_product_info(params, target)
        source_filter = params.get("source_name")
        limit_per_source = int(params.get("limit", 10))

        if not self.sources:
            return {
                "status": "UNAVAILABLE",
                "error": "No SupplierSource configured",
                "candidates_found": 0,
            }

        all_raw_candidates: List[SupplierCandidate] = []
        for src in self.sources:
            if source_filter and src.source_name != source_filter:
                continue
            self._external_calls_count += 1
            self._queried_sources.append(src.source_name)
            found = src.search_suppliers(
                query=query,
                brand=brand,
                model=model,
                sku=sku,
                limit=limit_per_source,
            )
            all_raw_candidates.extend(found)

        # Normalizar y deduplicar
        deduped = SupplierNormalizer.deduplicate_candidates(all_raw_candidates)

        # Evaluar scores preliminares y clasificar
        scored_candidates: List[SupplierCandidate] = []
        for cand in deduped:
            score_breakdown = SupplierScorer.calculate_score(cand, target_market_price=market_price)
            updated_cand = SupplierCandidate(
                supplier=cand.supplier,
                evidence=cand.evidence,
                product_match=cand.product_match,
                readiness=SupplierReadiness.EVALUATED if cand.product_match.grade != ProductMatchGrade.NO_MATCH else SupplierReadiness.REJECTED,
                score_breakdown=score_breakdown,
                risks=cand.risks,
                unknowns=cand.unknowns,
                rejection_reason=SupplierRejectionReason.NO_PRODUCT_MATCH if cand.product_match.grade == ProductMatchGrade.NO_MATCH else None,
                created_at=cand.created_at,
            )
            scored_candidates.append(updated_cand)
            self._cached_candidates[cand.supplier.supplier_id] = updated_cand

            # Persistir si repository disponible
            if self.repository:
                self.repository.save_supplier(cand.supplier)
                self.repository.save_evidence(cand.evidence)

        # Ranking determinista
        ranked = SupplierScorer.rank_candidates(scored_candidates, target_market_price=market_price)
        for r_cand in ranked:
            self._cached_candidates[r_cand.supplier.supplier_id] = r_cand

        # Actualizar mejor candidato si procede
        if ranked:
            top = ranked[0]
            if top.product_match.grade != ProductMatchGrade.NO_MATCH:
                self._update_best_supplier(top, iteration=state.iteration + 1, reason=f"Top ranked candidate discovered ({top.score}/100)")

        return {
            "status": "SUCCESS",
            "operation": "DISCOVER",
            "query": query,
            "sources_queried": list(set(self._queried_sources)),
            "raw_candidates_count": len(all_raw_candidates),
            "deduplicated_count": len(deduped),
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
                for c in ranked
            ],
            "best_supplier": self._best_known_supplier.name if self._best_known_supplier else None,
        }

    def _execute_evaluate_and_rank(self, params: Dict[str, Any], state: LoopState) -> Dict[str, Any]:
        query, brand, model, sku, market_price = self._get_target_product_info(params, None)
        all_candidates = list(self._cached_candidates.values())

        if not all_candidates:
            return {
                "status": "EMPTY",
                "message": "No candidates available to rank. Execute DISCOVER first.",
                "ranked_count": 0,
            }

        ranked = SupplierScorer.rank_candidates(all_candidates, target_market_price=market_price)
        for r_cand in ranked:
            self._cached_candidates[r_cand.supplier.supplier_id] = r_cand

        if ranked:
            top = ranked[0]
            if top.product_match.grade != ProductMatchGrade.NO_MATCH:
                self._update_best_supplier(top, iteration=state.iteration + 1, reason=f"Evaluated and ranked as #1 ({top.score}/100)")

        return {
            "status": "SUCCESS",
            "operation": "RANK",
            "ranked_count": len(ranked),
            "top_candidate": {
                "supplier_id": ranked[0].supplier.supplier_id,
                "name": ranked[0].supplier.name,
                "score": float(ranked[0].score) if ranked[0].score else 0.0,
                "product_match": ranked[0].product_match.grade.value,
            } if ranked else None,
            "rankings": [
                {
                    "rank": c.rank,
                    "supplier_id": c.supplier.supplier_id,
                    "name": c.supplier.name,
                    "score": float(c.score) if c.score else 0.0,
                    "breakdown": {
                        "match": float(c.score_breakdown.match_score),
                        "price": float(c.score_breakdown.price_score),
                        "availability": float(c.score_breakdown.availability_score),
                        "lead_time": float(c.score_breakdown.lead_time_score),
                        "reliability": float(c.score_breakdown.reliability_score),
                    } if c.score_breakdown else {},
                }
                for c in ranked
            ]
        }

    def _execute_investigate(self, supplier_id: Optional[str], params: Dict[str, Any], state: LoopState) -> Dict[str, Any]:
        if not supplier_id or supplier_id not in self._cached_candidates:
            return {
                "status": "NOT_FOUND",
                "error": f"Supplier candidate '{supplier_id}' not found in cached candidates.",
            }

        candidate = self._cached_candidates[supplier_id]
        # Simular o aplicar profundización con cotización confirmada si viene en parámetros
        quote_price = params.get("confirmed_wholesale_price")
        quote_shipping = params.get("confirmed_shipping_cost")
        quote_lead_time = params.get("confirmed_lead_time_days")

        updated_evidence = candidate.evidence
        if quote_price is not None:
            from src.domain.supplier_intelligence.models import ConfirmedQuote
            quote = ConfirmedQuote(
                quote_id=params.get("quote_id", f"Q-{supplier_id}"),
                wholesale_price=Decimal(str(quote_price)),
                shipping_cost=Decimal(str(quote_shipping or 0)),
                lead_time_days=int(quote_lead_time or 1),
                currency=params.get("currency", "CLP"),
            )
            updated_evidence = SupplierEvidence(
                supplier_id=candidate.evidence.supplier_id,
                sku=candidate.evidence.sku,
                wholesale_price=Decimal(str(quote_price)),
                currency=params.get("currency", "CLP"),
                minimum_order_quantity=candidate.evidence.minimum_order_quantity,
                stock_available=True,
                shipping_cost=Decimal(str(quote_shipping or 0)),
                lead_time_days=int(quote_lead_time or 1),
                confidence=candidate.evidence.confidence,
                signal_type=candidate.evidence.signal_type,
                provenance_type=candidate.evidence.provenance_type,
                source=candidate.evidence.source,
                quote=quote,
            )

        # Recalcular score
        updated_candidate = SupplierCandidate(
            supplier=candidate.supplier,
            evidence=updated_evidence,
            product_match=candidate.product_match,
            readiness=SupplierReadiness.READY_FOR_ECONOMICS if updated_evidence.quote else SupplierReadiness.EVALUATED,
            risks=candidate.risks,
            unknowns=(),
        )

        score_bd = SupplierScorer.calculate_score(updated_candidate)
        final_cand = SupplierCandidate(
            supplier=updated_candidate.supplier,
            evidence=updated_candidate.evidence,
            product_match=updated_candidate.product_match,
            readiness=updated_candidate.readiness,
            score_breakdown=score_bd,
            risks=updated_candidate.risks,
            unknowns=(),
        )

        self._cached_candidates[supplier_id] = final_cand
        self._update_best_supplier(final_cand, iteration=state.iteration + 1, reason=f"Deepened investigation completed for {supplier_id}")

        return {
            "status": "SUCCESS",
            "operation": "INVESTIGATE",
            "supplier_id": supplier_id,
            "name": final_cand.supplier.name,
            "readiness": final_cand.readiness.value,
            "new_score": float(final_cand.score) if final_cand.score else 0.0,
        }

    def _execute_compare(self, params: Dict[str, Any]) -> Dict[str, Any]:
        sup_a_id = params.get("supplier_a")
        sup_b_id = params.get("supplier_b")
        quantities = params.get("analysis_quantities", [1, 10, 50, 100])

        candidates_to_compare: List[SupplierCandidate] = []
        if sup_a_id and sup_b_id:
            cand_a = self._cached_candidates.get(sup_a_id)
            cand_b = self._cached_candidates.get(sup_b_id)
            if not cand_a or not cand_b:
                return {"status": "NOT_FOUND", "error": "One or both suppliers not found in cache"}
            candidates_to_compare = [cand_a, cand_b]
        else:
            candidates_to_compare = list(self._cached_candidates.values())

        if not candidates_to_compare:
            return {"status": "EMPTY", "error": "No candidates to compare"}

        target_title = self.target_opportunity.title if self.target_opportunity else "Target Product"
        target_sku = params.get("sku")
        target_price = params.get("target_market_price")
        dec_target_price = Decimal(str(target_price)) if target_price else None

        from src.domain.supplier_intelligence.services import QuoteComparator
        comparison_res = QuoteComparator.compare_candidates(
            candidates=candidates_to_compare,
            target_product_title=target_title,
            target_sku=target_sku,
            target_market_price=dec_target_price,
            analysis_quantities=quantities,
        )

        best_cand_dict = None
        if comparison_res.best_commercial_candidate:
            bc = comparison_res.best_commercial_candidate
            best_cand_dict = {
                "supplier_id": bc.supplier_id,
                "supplier_name": bc.supplier_name,
                "quote_id": bc.quote_id,
                "currency": bc.currency,
                "unit_price": float(bc.unit_price) if bc.unit_price else None,
                "moq": bc.moq,
                "lead_time_days": bc.lead_time_days,
                "shipping_cost": float(bc.shipping_cost) if bc.shipping_cost else None,
                "commercial_score": float(bc.commercial_score),
                "confidence": bc.confidence.value,
                "freshness": bc.freshness.value,
                "provenance_type": bc.provenance_type.value,
                "why_best": bc.why_best,
                "key_advantages": list(bc.key_advantages),
                "remaining_unknowns": list(bc.remaining_unknowns),
            }

        return {
            "status": "SUCCESS",
            "operation": "COMPARE",
            "candidates_compared": len(candidates_to_compare),
            "target_product": target_title,
            "ranked_items": [
                {
                    "rank": item.rank,
                    "supplier_id": item.supplier.supplier_id,
                    "supplier_name": item.supplier.name,
                    "quote_id": item.quote.quote_id,
                    "currency": item.quote.currency,
                    "unit_price": float(item.quote.unit_price) if item.quote.unit_price else None,
                    "moq": item.quote.moq.quantity,
                    "moq_type": item.quote.moq.moq_type.value,
                    "shipping_cost": float(item.quote.shipping_cost) if item.quote.shipping_cost else None,
                    "lead_time_days": item.quote.lead_time_days,
                    "stock_available": item.quote.stock_available,
                    "commercial_score": float(item.commercial_score) if item.commercial_score else 0.0,
                    "comparability": item.comparability_status.value,
                    "freshness": item.quote.freshness.value,
                    "provenance": item.quote.provenance_type.value,
                    "scenarios": [
                        {
                            "qty": sc.scenario_quantity,
                            "unit_price": float(sc.unit_price) if sc.unit_price else None,
                            "total_goods": float(sc.total_goods_cost) if sc.total_goods_cost else None,
                            "is_moq_satisfied": sc.is_moq_satisfied,
                        }
                        for sc in item.scenario_evaluations.values()
                    ],
                    "knowns": list(item.knowns),
                    "unknowns": list(item.unknowns),
                    "risks": list(item.risks),
                    "advantages": list(item.advantages),
                }
                for item in comparison_res.ranked_items
            ],
            "best_commercial_candidate": best_cand_dict,
            "conflicts": [
                {
                    "quote_a": c.quote_a_id,
                    "quote_b": c.quote_b_id,
                    "supplier_id": c.supplier_id,
                    "conflict_type": c.conflict_type,
                    "status": c.resolution_status.value,
                    "resolved_quote_id": c.resolved_quote_id,
                }
                for c in comparison_res.conflicts
            ],
            "non_comparable_reasons": list(comparison_res.non_comparable_reasons),
        }

    def _execute_compare_risk(self, params: Dict[str, Any], state: LoopState) -> Dict[str, Any]:
        """
        Ejecuta la evaluación multidimensional de riesgo, confiabilidad logística y desempeño histórico (C-03).
        """
        candidates_to_assess = list(self._cached_candidates.values())
        if not candidates_to_assess:
            return {"status": "EMPTY", "error": "No candidates to evaluate for risk"}

        target_title = self.target_opportunity.title if self.target_opportunity else "Target Product"
        target_sku = params.get("sku")
        target_price = params.get("target_market_price")
        dec_target_price = Decimal(str(target_price)) if target_price else None

        from src.domain.supplier_intelligence.services import SupplierRiskComparator
        supplier_histories = params.get("supplier_histories")

        risk_eval_res = SupplierRiskComparator.evaluate_and_compare(
            candidates=candidates_to_assess,
            target_product_title=target_title,
            target_sku=target_sku,
            target_market_price=dec_target_price,
            supplier_histories=supplier_histories,
            iteration=state.iteration + 1,
        )

        # Actualizar best_known si procede
        best_cand = risk_eval_res.best_supplier_candidate
        if best_cand:
            # Buscar el candidato original
            matching_orig = self._cached_candidates.get(best_cand.supplier_id)
            if matching_orig:
                self._update_best_supplier(
                    matching_orig,
                    iteration=state.iteration + 1,
                    reason=f"Selected best supplier based on risk and reliability: {best_cand.why_best} (Composite Score: {best_cand.composite_suitability_score})",
                )

        return {
            "status": "SUCCESS",
            "operation": "COMPARE_RISK",
            "evaluated_candidates_count": len(risk_eval_res.items),
            "target_product": target_title,
            "best_supplier_candidate": {
                "supplier_id": best_cand.supplier_id,
                "supplier_name": best_cand.supplier_name,
                "composite_suitability_score": float(best_cand.composite_suitability_score),
                "commercial_score": float(best_cand.commercial_score) if best_cand.commercial_score is not None else None,
                "reliability_score": float(best_cand.reliability_score) if best_cand.reliability_score is not None else None,
                "overall_risk_score": float(best_cand.overall_risk_score) if best_cand.overall_risk_score is not None else None,
                "confidence": best_cand.confidence.value,
                "provenance_type": best_cand.provenance_type.value,
                "why_best": best_cand.why_best,
                "key_strengths": list(best_cand.key_strengths),
                "identified_risks": list(best_cand.identified_risks),
                "remaining_unknowns": list(best_cand.remaining_unknowns),
            } if best_cand else None,
            "items": [
                {
                    "rank": item.rank,
                    "supplier_id": item.supplier.supplier_id,
                    "supplier_name": item.supplier.name,
                    "composite_suitability_score": float(item.composite_suitability_score) if item.composite_suitability_score is not None else None,
                    "commercial_score": float(item.preliminary_commercial_score) if item.preliminary_commercial_score is not None else None,
                    "reliability_score": float(item.reliability.reliability_score) if item.reliability.reliability_score is not None else None,
                    "risk_score": float(item.risk_profile.overall_risk_score) if item.risk_profile.overall_risk_score is not None else None,
                    "risk_level": item.risk_profile.overall_risk_level.value,
                    "lead_time_days": item.lead_time_profile.observed_days,
                    "lead_time_variance": item.lead_time_profile.historical_variance_days,
                    "shipping_cost": float(item.shipping_option.shipping_cost) if item.shipping_option.shipping_cost is not None else None,
                    "shipping_method": item.shipping_option.method.value,
                    "shipping_is_free": item.shipping_option.is_free_shipping_observed,
                    "sla_compliance_rate": item.reliability.sla_compliance_rate,
                    "historical_trend": item.historical_performance.lead_time_trend.value,
                    "is_reject_recommended": item.risk_profile.is_reject_recommended,
                    "rejection_reasons": [r.value for r in item.risk_profile.rejection_reasons],
                    "unknowns": list(item.unknowns),
                }
                for item in risk_eval_res.items
            ],
            "rejected_suppliers": [
                {
                    "supplier_id": rj.supplier.supplier_id,
                    "risk_level": rj.risk_profile.overall_risk_level.value,
                    "reasons": [r.value for r in rj.risk_profile.rejection_reasons],
                    "explanation": list(rj.risk_profile.explanation),
                }
                for rj in risk_eval_res.rejected_candidates
            ],
            "unknown_dimensions": list(risk_eval_res.non_comparable_logistics_reasons),
            "unresolved_conflicts": [],
        }

    def _execute_recommend(self, params: Dict[str, Any], state: LoopState) -> Dict[str, Any]:
        """
        Ejecuta la generación determinista de la recomendación de proveedores (C-04: C.13).
        """
        candidates_to_assess = list(self._cached_candidates.values())
        if not candidates_to_assess:
            return {"status": "EMPTY", "error": "No candidates to recommend"}

        target_title = self.target_opportunity.title if self.target_opportunity else "Target Product"
        target_sku = params.get("sku")
        target_price = params.get("target_market_price")
        dec_target_price = Decimal(str(target_price)) if target_price else None
        opp_id = self.target_opportunity.opportunity_id if self.target_opportunity else params.get("opportunity_id", "opp-default")

        from src.domain.supplier_intelligence.services import SupplierRiskComparator, SupplierRecommendationEngine
        supplier_histories = params.get("supplier_histories")

        risk_eval_res = SupplierRiskComparator.evaluate_and_compare(
            candidates=candidates_to_assess,
            target_product_title=target_title,
            target_sku=target_sku,
            target_market_price=dec_target_price,
            supplier_histories=supplier_histories,
            iteration=state.iteration + 1,
        )

        recommendation = SupplierRecommendationEngine.generate_recommendation(
            risk_evaluation_result=risk_eval_res,
            opportunity_id=opp_id,
            iteration=state.iteration + 1,
        )
        self._latest_recommendation = recommendation

        return {
            "status": "SUCCESS",
            "operation": "RECOMMEND_SUPPLIER",
            "recommendation_id": recommendation.recommendation_id,
            "opportunity_id": recommendation.opportunity_id,
            "target_product": recommendation.target_product_title,
            "decision": recommendation.decision.value,
            "decision_reason": recommendation.decision_reason,
            "confidence": recommendation.confidence.value,
            "provenance": recommendation.provenance.value,
            "freshness": recommendation.freshness.value,
            "primary_supplier": {
                "supplier_id": recommendation.primary_supplier.supplier_id,
                "supplier_name": recommendation.primary_supplier.supplier_name,
                "composite_score": float(recommendation.primary_supplier.composite_suitability_score),
                "commercial_score": float(recommendation.primary_supplier.commercial_score) if recommendation.primary_supplier.commercial_score is not None else None,
                "reliability_score": float(recommendation.primary_supplier.reliability_score) if recommendation.primary_supplier.reliability_score is not None else None,
                "risk_score": float(recommendation.primary_supplier.overall_risk_score) if recommendation.primary_supplier.overall_risk_score is not None else None,
                "selection_reason": recommendation.primary_supplier.selection_reason,
                "why_over_fallback": recommendation.primary_supplier.why_over_fallback,
                "commercial_position": recommendation.primary_supplier.commercial_position,
                "logistics_position": recommendation.primary_supplier.logistics_position,
                "strengths": list(recommendation.primary_supplier.key_strengths),
                "risks": list(recommendation.primary_supplier.identified_risks),
                "unknowns": list(recommendation.primary_supplier.unknowns),
                "invalidation_criteria": list(recommendation.primary_supplier.invalidation_criteria),
            } if recommendation.primary_supplier else None,
            "fallback_supplier": {
                "supplier_id": recommendation.fallback_supplier.supplier_id,
                "supplier_name": recommendation.fallback_supplier.supplier_name,
                "composite_score": float(recommendation.fallback_supplier.composite_suitability_score),
                "commercial_score": float(recommendation.fallback_supplier.commercial_score) if recommendation.fallback_supplier.commercial_score is not None else None,
                "reliability_score": float(recommendation.fallback_supplier.reliability_score) if recommendation.fallback_supplier.reliability_score is not None else None,
                "risk_score": float(recommendation.fallback_supplier.overall_risk_score) if recommendation.fallback_supplier.overall_risk_score is not None else None,
                "fallback_reason": recommendation.fallback_supplier.fallback_reason,
                "tradeoffs_vs_primary": recommendation.fallback_supplier.tradeoffs_vs_primary,
                "activation_conditions": list(recommendation.fallback_supplier.activation_conditions),
                "risks": list(recommendation.fallback_supplier.identified_risks),
                "unknowns": list(recommendation.fallback_supplier.unknowns),
            } if recommendation.fallback_supplier else None,
            "conditions": [
                {
                    "code": c.code,
                    "description": c.description,
                    "is_critical": c.is_critical,
                    "suggested_action": c.suggested_action,
                }
                for c in recommendation.conditions
            ],
            "unknowns": list(recommendation.unknowns),
            "rejection_reasons": list(recommendation.rejection_reasons),
            "explanation": {
                "observed_facts": list(recommendation.explanation.observed_facts),
                "derived_metrics": list(recommendation.explanation.derived_metrics),
                "inferred_signals": list(recommendation.explanation.inferred_signals),
                "summary": recommendation.explanation.recommendation_summary,
                "why_selected": recommendation.explanation.why_selected,
                "why_over_alternatives": recommendation.explanation.why_over_alternatives,
                "contingency_plan": recommendation.explanation.contingency_plan,
            } if recommendation.explanation else None,
        }

    def _execute_contingency_pivot(self, params: Dict[str, Any], state: LoopState) -> Dict[str, Any]:
        """
        Ejecuta la reevaluación de contingencia e invalidación del proveedor primario (C-04).
        """
        if not self._latest_recommendation:
            return {"status": "ERROR", "error": "No existing recommendation to re-evaluate"}

        trigger_str = params.get("trigger", "STOCK_UNAVAILABLE")
        trigger_details = params.get("details", "")
        try:
            trigger_enum = ContingencyTrigger(trigger_str)
        except ValueError:
            trigger_enum = ContingencyTrigger.MANUAL_OVERRIDE

        from src.domain.supplier_intelligence.services import SupplierRecommendationEngine
        new_rec, pivoted = SupplierRecommendationEngine.reevaluate_and_pivot_fallback(
            recommendation=self._latest_recommendation,
            trigger=trigger_enum,
            trigger_details=trigger_details,
        )
        self._latest_recommendation = new_rec

        return {
            "status": "SUCCESS",
            "operation": "CONTINGENCY_PIVOT",
            "pivoted_successfully": pivoted,
            "trigger": trigger_enum.value,
            "trigger_details": trigger_details,
            "decision": new_rec.decision.value,
            "decision_reason": new_rec.decision_reason,
            "new_primary": {
                "supplier_id": new_rec.primary_supplier.supplier_id,
                "supplier_name": new_rec.primary_supplier.supplier_name,
                "composite_score": float(new_rec.primary_supplier.composite_suitability_score),
            } if new_rec.primary_supplier else None,
            "fallback_available": new_rec.fallback_supplier is not None,
        }

    def _execute_reject(self, supplier_id: Optional[str], params: Dict[str, Any]) -> Dict[str, Any]:
        if not supplier_id or supplier_id not in self._cached_candidates:
            return {"status": "NOT_FOUND", "error": f"Supplier {supplier_id} not found"}

        reason_str = params.get("rejection_reason", "OTHER")
        try:
            reason_enum = SupplierRejectionReason(reason_str)
        except ValueError:
            reason_enum = SupplierRejectionReason.OTHER

        candidate = self._cached_candidates[supplier_id]
        rejected_cand = SupplierCandidate(
            supplier=candidate.supplier,
            evidence=candidate.evidence,
            product_match=candidate.product_match,
            readiness=SupplierReadiness.REJECTED,
            score_breakdown=candidate.score_breakdown,
            rank=None,
            risks=candidate.risks,
            unknowns=candidate.unknowns,
            rejection_reason=reason_enum,
            created_at=candidate.created_at,
        )
        self._cached_candidates[supplier_id] = rejected_cand

        return {
            "status": "SUCCESS",
            "operation": "REJECT",
            "supplier_id": supplier_id,
            "rejection_reason": reason_enum.value,
        }

    def _execute_pivot(self, params: Dict[str, Any], state: LoopState) -> Dict[str, Any]:
        new_source = params.get("pivot_to_source")
        new_query = params.get("new_query")

        return {
            "status": "SUCCESS",
            "operation": "PIVOT",
            "message": f"Pivoting search strategy. New source: {new_source}, New query: {new_query}",
            "previous_candidates_count": len(self._cached_candidates),
        }

    def _update_best_supplier(self, candidate: SupplierCandidate, iteration: int, reason: str) -> None:
        if candidate.score_breakdown is None:
            return

        current_score = candidate.score_breakdown.total_score
        previous_best = self._best_known_supplier

        # Reemplazar si no había o si el nuevo score es estrictamente mayor
        if previous_best is None or current_score > previous_best.score:
            new_best = BestKnownSupplier(
                supplier_id=candidate.supplier.supplier_id,
                name=candidate.supplier.name,
                source=candidate.supplier.source,
                source_type=candidate.supplier.source_type,
                sku=candidate.evidence.sku,
                score=current_score,
                confidence=candidate.evidence.confidence,
                product_match_grade=candidate.product_match.grade,
                readiness=candidate.readiness,
                iteration=iteration,
                why_best=reason,
                evidence_snapshot=candidate.evidence,
            )

            self._best_supplier_history.append({
                "iteration": iteration,
                "previous_best_id": previous_best.supplier_id if previous_best else None,
                "previous_best_name": previous_best.name if previous_best else None,
                "previous_score": float(previous_best.score) if previous_best else None,
                "current_best_id": new_best.supplier_id,
                "current_best_name": new_best.name,
                "current_score": float(new_best.score),
                "reason": reason,
            })

            self._best_known_supplier = new_best
