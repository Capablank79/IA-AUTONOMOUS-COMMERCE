from decimal import Decimal
from typing import List, Optional, Tuple, Sequence, Mapping, Any
from datetime import datetime, timezone
import statistics

from src.domain.market_intelligence.models import MarketEvidence, Confidence, MarketListing
from src.domain.opportunity.models import (
    OpportunityDecision,
    OpportunityReadiness,
    OpportunityExplanation,
    BestKnownOpportunity,
    OpportunityProgress,
    CompletionPolicy,
    EvidenceSufficiency,
    OpportunityRejection,
    RejectionReason,
    OpportunityEvaluationHistoryEntry,
    OpportunityComparisonResult,
    OpportunityComparisonDimension,
    Opportunity,
)

class OpportunityEngine:
    """
    Domain Engine responsable de evaluar la evidencia del mercado y producir
    una evaluación de oportunidad de negocio consistente, determinista, explicable,
    comparable y monitorizable.

    Capacidades del Hito B:
    - Opportunity scoring
    - Ranking
    - Readiness
    - Evidence sufficiency
    - Opportunity explanation
    - Opportunity comparison
    - Opportunity rejection
    - Opportunity monitoring (reevaluación temporal e historial de cambios)
    """

    def evaluate_evidence_sufficiency(self, evidence: MarketEvidence) -> EvidenceSufficiency:
        """
        Evalúa explícitamente la suficiencia de la evidencia de mercado disponible
        sin inventar datos faltantes.
        - SUFFICIENT: dispone de señales directas de demanda observada/tráfico con cobertura y precio.
        - PARTIAL: dispone de listado con al menos una señal de mercado (precio, sold_quantity o tendencia), pero sin analítica profunda de tráfico.
        - INSUFFICIENT: sólo listado básico sin señales de demanda, tráfico ni ventas verificables.
        """
        has_traffic_with_data = bool(
            evidence.traffic_signals and any(v.total_visits is not None for v in evidence.traffic_signals)
        )
        has_demand_signal = bool(
            evidence.demand_signals and any(d.label in ("OBSERVED_TRAFFIC", "NO_TRAFFIC") for d in evidence.demand_signals)
        )
        has_sold_quantity = evidence.listing.sold_quantity is not None
        has_reviews = bool(evidence.review_signals and any(r.total_reviews > 0 for r in evidence.review_signals))
        has_trend = bool(evidence.trend_signals and any(t.matched for t in evidence.trend_signals))
        has_price = bool(evidence.price_signals)

        signals_present_count = sum([
            has_traffic_with_data or has_demand_signal,
            has_sold_quantity,
            has_reviews,
            has_trend,
            has_price
        ])

        if (has_traffic_with_data or has_demand_signal or has_sold_quantity) and signals_present_count >= 2:
            return EvidenceSufficiency.SUFFICIENT
        elif signals_present_count >= 1:
            return EvidenceSufficiency.PARTIAL
        else:
            return EvidenceSufficiency.INSUFFICIENT

    def determine_readiness(
        self,
        evidence: MarketEvidence,
        score: Optional[Decimal] = None,
        sufficiency: Optional[EvidenceSufficiency] = None
    ) -> Tuple[OpportunityReadiness, Tuple[str, ...]]:
        """
        Determina el nivel de madurez/readiness de la oportunidad de manera independiente del score.
        Una oportunidad puede tener HIGH SCORE pero INSUFFICIENT EVIDENCE.

        Transición lógica:
        - INSUFFICIENT_EVIDENCE: Falta evidencia básica o demanda desconocida.
        - NEEDS_INVESTIGATION: Evidencia parcial, prometedora pero requiere profundizar señales.
        - READY: Evidencia suficiente y señales positivas para avanzar a Supplier Intelligence.
        - REJECTED: Tráfico nulo verificado, demanda nula o inviable.
        """
        reasons: List[str] = []
        eff_sufficiency = sufficiency or self.evaluate_evidence_sufficiency(evidence)
        eff_score = score if score is not None else self.calculate_deterministic_market_score(evidence)

        # Verificar tráfico negativo o nulo verificado
        has_zero_traffic = any(v.total_visits == 0 for v in evidence.traffic_signals if v.total_visits is not None)
        has_no_traffic_demand = any(d.label == "NO_TRAFFIC" for d in evidence.demand_signals)

        if has_zero_traffic or has_no_traffic_demand:
            reasons.append("Observed zero traffic in market window")
            return OpportunityReadiness.REJECTED, tuple(reasons)

        if eff_sufficiency == EvidenceSufficiency.INSUFFICIENT:
            reasons.append("Missing essential demand and traffic signals")
            reasons.append("Missing supplier data (cost, MOQ)")
            reasons.append("Missing economics data (fees, shipping, taxes)")
            return OpportunityReadiness.INSUFFICIENT_EVIDENCE, tuple(reasons)

        if eff_sufficiency == EvidenceSufficiency.PARTIAL:
            reasons.append("Partial evidence available; deeper traffic or review validation required")
            reasons.append("Missing supplier data (cost, MOQ)")
            reasons.append("Missing economics data (fees, shipping, taxes)")
            return OpportunityReadiness.NEEDS_INVESTIGATION, tuple(reasons)

        # Sufficiency is SUFFICIENT
        if eff_score >= Decimal("30.0"):
            reasons.append("Observed positive market signals with sufficient evidence")
            reasons.append("Missing supplier data (cost, MOQ)")
            reasons.append("Missing economics data (fees, shipping, taxes)")
            return OpportunityReadiness.READY, tuple(reasons)
        else:
            reasons.append("Sufficient evidence collected, but low composite market traction score")
            reasons.append("Missing supplier data (cost, MOQ)")
            reasons.append("Missing economics data (fees, shipping, taxes)")
            return OpportunityReadiness.REJECTED, tuple(reasons)

    def evaluate(self, evidence: MarketEvidence) -> OpportunityDecision:
        """
        Evalúa la evidencia de mercado produciendo una decisión estructurada con suficiencia,
        readiness, score, confianza, riesgos y razones de dominio.
        """
        sufficiency = self.evaluate_evidence_sufficiency(evidence)
        score = self.calculate_deterministic_market_score(evidence)
        readiness, reasons = self.determine_readiness(evidence, score=score, sufficiency=sufficiency)
        confidence = evidence.confidence

        explanation = self.generate_explanation(evidence, score=score)

        rejection: Optional[OpportunityRejection] = None
        if readiness == OpportunityReadiness.REJECTED:
            rejection_reason = RejectionReason.WEAK_DEMAND if any("traffic" in r.lower() for r in reasons) else RejectionReason.LOW_SCORE
            rejection = OpportunityRejection(
                product_id=evidence.listing.external_id,
                reason=rejection_reason,
                details="; ".join(reasons),
                confidence=confidence,
                evidence_snapshot=evidence
            )

        return OpportunityDecision(
            evidence=evidence,
            readiness=readiness,
            reasons=reasons,
            opportunity_score=score,
            confidence=confidence,
            evidence_sufficiency=sufficiency,
            rejection=rejection,
            explanation=explanation
        )

    def calculate_deterministic_market_score(self, evidence: MarketEvidence) -> Decimal:
        """
        Calcula un score de oportunidad de mercado determinista basado estrictamente
        en las señales observadas y derivadas (demanda, precio, tendencia, tráfico).
        Rango: 0.0 - 100.0.
        """
        score = Decimal("0.0")

        # 1. Componente de demanda (hasta 40 pts)
        if evidence.demand_signals:
            demand = evidence.demand_signals[0]
            if demand.score is not None:
                score += demand.score * Decimal("40.0")
            elif demand.label == "OBSERVED_TRAFFIC":
                score += Decimal("25.0")
        elif evidence.listing.sold_quantity is not None:
            sold = evidence.listing.sold_quantity
            if sold > 100:
                score += Decimal("40.0")
            elif sold > 50:
                score += Decimal("25.0")
            elif sold > 10:
                score += Decimal("10.0")

        # 2. Componente de tráfico / visitas (hasta 30 pts)
        if evidence.traffic_signals:
            visit = evidence.traffic_signals[0]
            if visit.total_visits is not None:
                if visit.total_visits > 500:
                    score += Decimal("30.0")
                elif visit.total_visits > 100:
                    score += Decimal("20.0")
                elif visit.total_visits > 0:
                    score += Decimal("10.0")

        # 3. Componente de tendencia (hasta 20 pts)
        if evidence.trend_signals:
            trend = evidence.trend_signals[0]
            score += trend.trend_score * Decimal("20.0")

        # 4. Componente de precio / competitividad (hasta 10 pts)
        if evidence.price_signals:
            price_sig = evidence.price_signals[0]
            if price_sig.position == "UNDER_MARKET":
                score += Decimal("10.0")
            elif price_sig.position == "AT_MARKET":
                score += Decimal("5.0")

        return min(Decimal("100.0"), max(Decimal("0.0"), round(score, 2)))

    def rank_opportunities(self, opportunities: Sequence[Any]) -> List[Any]:
        """
        Ordena deterministamente una lista de oportunidades o evidencias por score descendente,
        desempatando por nivel de confianza y frescura.
        """
        def get_sort_key(item: Any) -> Tuple[Decimal, int, int]:
            if isinstance(item, Opportunity):
                sc = item.score or Decimal("0.0")
                conf_val = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}.get(item.confidence.value, 0)
                suff_val = {"SUFFICIENT": 2, "PARTIAL": 1, "INSUFFICIENT": 0}.get(item.evidence_sufficiency.value, 0)
                return (sc, suff_val, conf_val)
            elif isinstance(item, BestKnownOpportunity):
                conf_val = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}.get(item.confidence.value, 0)
                return (item.score, 1, conf_val)
            elif isinstance(item, MarketEvidence):
                sc = self.calculate_deterministic_market_score(item)
                conf_val = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}.get(item.confidence.value, 0)
                return (sc, 1, conf_val)
            elif isinstance(item, OpportunityDecision):
                sc = item.opportunity_score or Decimal("0.0")
                conf_val = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}.get(item.confidence.value, 0)
                return (sc, 1, conf_val)
            return (Decimal("0.0"), 0, 0)

        return sorted(opportunities, key=get_sort_key, reverse=True)

    def reject_opportunity(
        self,
        evidence: MarketEvidence,
        reason: RejectionReason,
        details: str,
        confidence: Optional[Confidence] = None
    ) -> OpportunityDecision:
        """
        Rechaza explícitamente una oportunidad basándose en razones y evidencia verificables de dominio.
        """
        conf = confidence or evidence.confidence
        rejection = OpportunityRejection(
            product_id=evidence.listing.external_id,
            reason=reason,
            details=details,
            confidence=conf,
            evidence_snapshot=evidence
        )
        score = self.calculate_deterministic_market_score(evidence)
        sufficiency = self.evaluate_evidence_sufficiency(evidence)
        explanation = self.generate_explanation(evidence, score=score)

        return OpportunityDecision(
            evidence=evidence,
            readiness=OpportunityReadiness.REJECTED,
            reasons=(f"REJECTED: {reason.value} - {details}",),
            opportunity_score=score,
            confidence=conf,
            evidence_sufficiency=sufficiency,
            rejection=rejection,
            explanation=explanation
        )

    def compare_opportunities(
        self,
        candidate_a: Any,
        candidate_b: Any,
        *additional_candidates: Any
    ) -> OpportunityComparisonResult:
        """
        Compara dos o más oportunidades bajo criterios comunes y reproducibles:
        - Score de oportunidad
        - Nivel de confianza
        - Cobertura de evidencia
        - Incertidumbre y riesgos
        - Señales observadas y de demanda
        """
        all_items = [candidate_a, candidate_b] + list(additional_candidates)
        candidates: List[Opportunity] = []

        for item in all_items:
            if isinstance(item, Opportunity):
                candidates.append(item)
            elif isinstance(item, MarketEvidence):
                opp = self.create_opportunity(item)
                candidates.append(opp)
            elif isinstance(item, BestKnownOpportunity):
                opp = self.create_opportunity(item.evidence)
                candidates.append(opp)
            elif isinstance(item, OpportunityDecision):
                opp = self.create_opportunity(item.evidence)
                candidates.append(opp)

        candidate_ids = tuple(c.product_id for c in candidates)

        # 1. Dimensión Score
        score_dict = {c.product_id: float(c.score) if c.score is not None else 0.0 for c in candidates}
        best_score_id = max(score_dict, key=lambda k: score_dict[k])
        dim_score = OpportunityComparisonDimension(
            dimension_name="Score & Market Traction",
            winner_id=best_score_id,
            summary=f"Candidate {best_score_id} has highest opportunity score ({score_dict[best_score_id]}).",
            scores_by_candidate=score_dict
        )

        # 2. Dimensión Confianza
        conf_map = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
        conf_dict = {c.product_id: c.confidence.value for c in candidates}
        best_conf_id = max(candidates, key=lambda c: conf_map.get(c.confidence.value, 0)).product_id
        dim_conf = OpportunityComparisonDimension(
            dimension_name="Confidence Level",
            winner_id=best_conf_id,
            summary=f"Candidate {best_conf_id} has highest confidence ({conf_dict[best_conf_id]}).",
            scores_by_candidate=conf_dict
        )

        # 3. Dimensión Cobertura de Evidencia
        suff_map = {"SUFFICIENT": 2, "PARTIAL": 1, "INSUFFICIENT": 0}
        suff_dict = {c.product_id: c.evidence_sufficiency.value for c in candidates}
        best_suff_id = max(candidates, key=lambda c: suff_map.get(c.evidence_sufficiency.value, 0)).product_id
        dim_suff = OpportunityComparisonDimension(
            dimension_name="Evidence Coverage",
            winner_id=best_suff_id,
            summary=f"Candidate {best_suff_id} has best evidence coverage ({suff_dict[best_suff_id]}).",
            scores_by_candidate=suff_dict
        )

        # 4. Dimensión Riesgos / Incertidumbre (menor número de riesgos = mejor)
        risks_dict = {c.product_id: len(c.risks) for c in candidates}
        lowest_risk_id = min(candidates, key=lambda c: len(c.risks)).product_id
        dim_risk = OpportunityComparisonDimension(
            dimension_name="Uncertainty & Risks",
            winner_id=lowest_risk_id,
            summary=f"Candidate {lowest_risk_id} presents the least identified risks ({risks_dict[lowest_risk_id]} risks).",
            scores_by_candidate=risks_dict
        )

        dimensions = (dim_score, dim_conf, dim_suff, dim_risk)

        # Determinación global del mejor candidato
        # Criterio compuesto: Score (50%) + Cobertura (30%) + Confianza (20%)
        def composite_rank_value(c: Opportunity) -> float:
            sc = float(c.score or Decimal("0.0"))
            suff_pts = suff_map.get(c.evidence_sufficiency.value, 0) * 20.0
            conf_pts = conf_map.get(c.confidence.value, 0) * 10.0
            risk_penalty = len(c.risks) * 5.0
            return sc + suff_pts + conf_pts - risk_penalty

        ranked_candidates = sorted(candidates, key=composite_rank_value, reverse=True)
        winner = ranked_candidates[0]

        summary = f"Comparison of {len(candidates)} candidates evaluated across score, confidence, evidence coverage, and risks. Winner: {winner.product_id}."
        why_winner = (
            f"Candidate {winner.product_id} ('{winner.title}') ranked superior with composite score {winner.score}/100, "
            f"evidence sufficiency {winner.evidence_sufficiency.value}, and confidence {winner.confidence.value}."
        )

        return OpportunityComparisonResult(
            candidate_ids=candidate_ids,
            best_candidate_id=winner.product_id,
            dimensions=dimensions,
            comparison_summary=summary,
            why_winner=why_winner
        )

    def create_opportunity(
        self,
        evidence: MarketEvidence,
        opportunity_id: Optional[str] = None
    ) -> Opportunity:
        """
        Crea un objeto Opportunity completo a partir de MarketEvidence.
        """
        decision = self.evaluate(evidence)
        op_id = opportunity_id or f"opp-{evidence.listing.external_id}"
        explanation = decision.explanation or self.generate_explanation(evidence, score=decision.opportunity_score)

        return Opportunity(
            opportunity_id=op_id,
            product_id=evidence.listing.external_id,
            title=evidence.listing.title,
            listing=evidence.listing,
            evidence=evidence,
            score=decision.opportunity_score,
            confidence=decision.confidence,
            evidence_sufficiency=decision.evidence_sufficiency,
            readiness=decision.readiness,
            risks=explanation.risks,
            unknowns=explanation.unknowns,
            decision=decision,
            explanation=explanation,
            rejection=decision.rejection,
            history=(),
            provenance={
                "created_by": "OpportunityEngine",
                "marketplace": evidence.listing.marketplace.value,
                "seller_id": evidence.listing.seller_id,
                "source_listing_id": evidence.listing.external_id
            }
        )

    def reevaluate_opportunity(
        self,
        current_opportunity: Opportunity,
        new_evidence: MarketEvidence,
        reason: str = "New market evidence observed"
    ) -> Opportunity:
        """
        Reevalúa una oportunidad ante nueva evidencia conservando el historial temporal inmutable.
        Detecta mejora, deterioro, cambio de readiness o cambio de decisión sin sobrescribir el pasado.
        """
        now = datetime.now(timezone.utc)
        new_decision = self.evaluate(new_evidence)
        new_explanation = new_decision.explanation or self.generate_explanation(new_evidence, score=new_decision.opportunity_score)

        # Crear entrada de historial
        history_entry = OpportunityEvaluationHistoryEntry(
            timestamp=now,
            previous_score=current_opportunity.score,
            new_score=new_decision.opportunity_score,
            previous_readiness=current_opportunity.readiness,
            new_readiness=new_decision.readiness,
            previous_confidence=current_opportunity.confidence,
            new_confidence=new_decision.confidence,
            change_reason=reason,
            previous_evidence=current_opportunity.evidence,
            evidence_summary=f"Traffic signals: {len(new_evidence.traffic_signals)}, Reviews: {len(new_evidence.review_signals)}"
        )

        updated_history = current_opportunity.history + (history_entry,)

        return Opportunity(
            opportunity_id=current_opportunity.opportunity_id,
            product_id=new_evidence.listing.external_id,
            title=new_evidence.listing.title,
            listing=new_evidence.listing,
            evidence=new_evidence,
            score=new_decision.opportunity_score,
            confidence=new_decision.confidence,
            evidence_sufficiency=new_decision.evidence_sufficiency,
            readiness=new_decision.readiness,
            risks=new_explanation.risks,
            unknowns=new_explanation.unknowns,
            decision=new_decision,
            explanation=new_explanation,
            rejection=new_decision.rejection,
            history=updated_history,
            created_at=current_opportunity.created_at,
            updated_at=now,
            provenance=current_opportunity.provenance
        )

    def generate_explanation(self, evidence: MarketEvidence, score: Optional[Decimal] = None) -> OpportunityExplanation:
        """
        Genera una explicación estructurada estricta que distingue:
        WHY_WINNER, EVIDENCE (OBSERVED), DERIVED SIGNALS, INFERENCES, RISKS, UNKNOWNS.
        """
        listing = evidence.listing
        observed: List[str] = [
            f"Marketplace: {listing.marketplace.value}",
            f"Listing ID: {listing.external_id}",
            f"Title: {listing.title}",
            f"Price: {listing.price.amount} {listing.price.currency}",
            f"Available Quantity: {listing.available_quantity}",
            f"Condition: {listing.condition}",
            f"Seller ID: {listing.seller_id}"
        ]
        if listing.sold_quantity is not None:
            observed.append(f"Observed Sold Quantity: {listing.sold_quantity}")
        else:
            observed.append("Observed Sold Quantity: UNKNOWN")

        for visit in evidence.traffic_signals:
            visits_str = str(visit.total_visits) if visit.total_visits is not None else "UNKNOWN"
            observed.append(f"Traffic Window: {visit.window}, Visits: {visits_str}, Coverage: {round(visit.coverage_ratio*100, 1)}%")

        for review in evidence.review_signals:
            observed.append(f"Reviews Total: {review.total_reviews}, Rating: {review.average_rating}/5.0")

        derived: List[str] = []
        for demand in evidence.demand_signals:
            derived.append(f"Demand Label: {demand.label}, Signal Score: {demand.score}")
        for price_sig in evidence.price_signals:
            derived.append(f"Price Ratio: {price_sig.ratio}, Position: {price_sig.position}")
        for trend_sig in evidence.trend_signals:
            derived.append(f"Trend Match: {trend_sig.matched}, Rank: {trend_sig.rank}, Trend Score: {trend_sig.trend_score}")

        inferences: List[str] = []
        effective_score = score if score is not None else self.calculate_deterministic_market_score(evidence)
        if effective_score >= Decimal("50.0"):
            inferences.append("High market demand and visibility relative to baseline competition.")
        else:
            inferences.append("Moderate or nascent market traction requiring further validation.")

        risks: List[str] = []
        if listing.sold_quantity is None:
            risks.append("Sold quantity is not publicly disclosed by marketplace.")
        if not evidence.review_signals or not any(r.total_reviews > 0 for r in evidence.review_signals):
            risks.append("Limited customer feedback / review signals available.")
        if not evidence.traffic_signals:
            risks.append("Traffic analytics unverified or inaccessible.")

        unknowns: List[str] = [
            "Supplier cost and Minimum Order Quantity (MOQ)",
            "Logistics, import duties, and marketplace fulfillment fees",
            "Net operating profit margin and return on advertising spend"
        ]

        why_winner = (
            f"Product presents composite opportunity score of {effective_score}/100 with "
            f"validated price position ({listing.price.amount} {listing.price.currency}) "
            f"and active category demand."
        )

        return OpportunityExplanation(
            product_id=listing.external_id,
            title=listing.title,
            why_winner=why_winner,
            observed_evidence=tuple(observed),
            derived_signals=tuple(derived),
            inferred_insights=tuple(inferences),
            risks=tuple(risks),
            unknowns=tuple(unknowns),
            recommended_action="PROCEED_TO_SUPPLIER_AND_UNIT_ECONOMICS_EVALUATION" if effective_score >= Decimal("30.0") else "MONITOR_OR_PIVOT"
        )

    def validate_completion(
        self,
        best_known: Optional[BestKnownOpportunity],
        candidates_count: int,
        policy: Optional[CompletionPolicy] = None
    ) -> Tuple[bool, str]:
        """
        Valida deterministamente si se cumplen las condiciones para cerrar la misión con COMPLETE.
        """
        policy = policy or CompletionPolicy()

        if policy.require_best_known and best_known is None:
            return False, "Cannot complete: No best_known candidate identified yet"

        if candidates_count < policy.min_candidates:
            return False, f"Cannot complete: Insufficient evaluated candidates ({candidates_count} < {policy.min_candidates})"

        if best_known is not None:
            if best_known.score < policy.min_score:
                return False, f"Cannot complete: Best opportunity score ({best_known.score}) is below minimum threshold ({policy.min_score})"
            if best_known.confidence not in policy.min_confidence:
                return False, f"Cannot complete: Confidence {best_known.confidence.value} does not meet policy requirements"

        return True, "Deterministic validation passed"
