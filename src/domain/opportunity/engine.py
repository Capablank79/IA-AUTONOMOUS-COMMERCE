from decimal import Decimal
from typing import List

from src.domain.market_intelligence.models import MarketEvidence, Confidence
from src.domain.opportunity.models import OpportunityDecision, OpportunityReadiness

class OpportunityEngine:
    """
    Domain Engine responsable de evaluar la evidencia del mercado y producir
    una decisión de oportunidad. 
    Actualmente es un MVP que respeta la ausencia de fórmulas comerciales
    arbitrarias y la separación entre tráfico (Visibility) y ventas (Demand).
    """

    def evaluate(self, evidence: MarketEvidence) -> OpportunityDecision:
        reasons: List[str] = []
        readiness = OpportunityReadiness.INSUFFICIENT_EVIDENCE
        confidence = evidence.confidence

        # Evaluamos la evidencia de demanda (visibilidad)
        has_traffic = False
        no_traffic = False

        if not evidence.demand_signals:
            reasons.append("Missing demand signal")
        else:
            demand = evidence.demand_signals[0]
            if demand.label == "UNKNOWN":
                reasons.append("Demand is unknown")
            elif demand.label == "NO_TRAFFIC":
                no_traffic = True
                reasons.append("Observed zero traffic")
            elif demand.label == "OBSERVED_TRAFFIC":
                has_traffic = True
                reasons.append("Observed positive traffic")

        # Registramos los Gaps documentados que impiden calcular el score final
        reasons.append("Missing supplier data (cost, MOQ)")
        reasons.append("Missing economics data (fees, shipping, taxes)")

        if no_traffic:
            readiness = OpportunityReadiness.INSUFFICIENT_EVIDENCE
        elif has_traffic:
            readiness = OpportunityReadiness.SUFFICIENT_EVIDENCE
        else:
            readiness = OpportunityReadiness.INSUFFICIENT_EVIDENCE

        # Como la SPEC aún no define la matemática para convertir DemandSignal,
        # TrendSignal, PriceSignal y VisitSignal en un score de oportunidad comercial sin costos,
        # no inventamos una fórmula y retornamos opportunity_score = None.
        
        return OpportunityDecision(
            evidence=evidence,
            readiness=readiness,
            reasons=reasons,
            opportunity_score=None,
            confidence=confidence
        )
