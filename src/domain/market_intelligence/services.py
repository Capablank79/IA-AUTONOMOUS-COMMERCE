from typing import List
from decimal import Decimal
from datetime import datetime
import statistics

from .models import (
    MarketSnapshot,
    MarketOpportunity,
    DemandSignal,
    PriceSignal,
    TrendSignal,
    MarketListing,
    MarketEvidence,
    VisitSignal,
    Confidence
)

class MarketEvidenceComposer:
    """
    Domain Service for composing MarketEvidence from its constituent parts.
    It acts as a pure factory that preserves all signals exactly as they were observed
    or derived, without applying any business scoring or arbitrary thresholds.
    """

    def compose(
        self,
        listing: MarketListing,
        visit_signal: VisitSignal | None = None,
        trend_signal: TrendSignal | None = None,
        price_signal: PriceSignal | None = None,
        demand_signal: DemandSignal | None = None,
    ) -> MarketEvidence:

        traffic_signals = [visit_signal] if visit_signal else []
        trend_signals = [trend_signal] if trend_signal else []
        price_signals = [price_signal] if price_signal else []
        demand_signals = [demand_signal] if demand_signal else []

        # Determinar confianza agregada basada en las señales disponibles
        confidence = Confidence.UNKNOWN
        if visit_signal:
            confidence = visit_signal.confidence
        elif demand_signal:
            confidence = demand_signal.confidence

        return MarketEvidence(
            listing=listing,
            traffic_signals=traffic_signals,
            trend_signals=trend_signals,
            price_signals=price_signals,
            demand_signals=demand_signals,
            confidence=confidence,
        )

class DemandIntelligenceService:
    """
    Domain Service for deriving DemandSignal from MarketEvidence.
    This service explicitly separates traffic visibility from sales conversion.
    It does not infer sales or revenue from visits, and does not invent scoring
    formulas without SPEC backing.
    """

    def calculate(self, evidence: MarketEvidence) -> DemandSignal:
        if not evidence.traffic_signals:
            return DemandSignal(score=None, label="UNKNOWN")

        visit_signal = evidence.traffic_signals[0]

        if visit_signal.total_visits is None:
            return DemandSignal(score=None, label="UNKNOWN")

        if visit_signal.total_visits == 0:
            return DemandSignal(score=None, label="NO_TRAFFIC")

        return DemandSignal(score=None, label="OBSERVED_TRAFFIC")

class MarketAnalysisService:
    """
    Domain Service for analyzing market snapshots and detecting opportunities.
    """

    def analyze(self, snapshot: MarketSnapshot) -> List[MarketOpportunity]:
        if not snapshot.listings:
            return []

        opportunities = []
        prices = [listing.price.amount for listing in snapshot.listings]
        median_price = Decimal(str(statistics.median(prices)))

        for listing in snapshot.listings:
            demand_signal = self._calculate_demand(listing)
            price_signal = self._calculate_price_signal(listing, median_price)

            trend_signal = self._calculate_trend(
                snapshot.search_criteria.query,
                snapshot.trends,
            )

            # Composite opportunity score:
            # - Demand: 50%
            # - Price: 30%
            # - Trend: 20%
            #
            # Price score preserves the original business rule:
            # lower price ratio = better opportunity.
            if price_signal.ratio > 0:
                price_score = Decimal("1.0") / price_signal.ratio
            else:
                price_score = Decimal("0.0")

            demand_score = demand_signal.score if demand_signal.score is not None else Decimal("0.0")
            opportunity_score = (
                demand_score * Decimal("0.50")
                + price_score * Decimal("0.30")
                + trend_signal.trend_score * Decimal("0.20")
            ) * Decimal("100.0")

            opportunity = MarketOpportunity(
                snapshot_id=snapshot.snapshot_id,
                listing=listing,
                demand_signal=demand_signal,
                price_signal=price_signal,
                trend_signal=trend_signal,
                opportunity_score=round(opportunity_score, 2),
                detected_at=datetime.utcnow()
            )
            opportunities.append(opportunity)

        return opportunities

    def _calculate_trend(
        self,
        query: str,
        trends: list[dict],
    ) -> TrendSignal:
        normalized_query = query.strip().lower()
        total_trends = len(trends)

        for trend in trends:
            keyword = str(trend.get("keyword", "")).strip().lower()

            if keyword == normalized_query:
                rank = int(trend["rank"])

                if total_trends > 0:
                    trend_score = (
                        Decimal(total_trends - rank + 1)
                        / Decimal(total_trends)
                    )
                else:
                    trend_score = Decimal("0")

                return TrendSignal(
                    keyword=trend["keyword"],
                    rank=rank,
                    matched=True,
                    trend_score=trend_score.quantize(Decimal("0.01")),
                )

        return TrendSignal(
            keyword=query,
            rank=0,
            matched=False,
            trend_score=Decimal("0"),
        )

    def _calculate_demand(self, listing: MarketListing) -> DemandSignal:
        # Simple demand calculation for MVP based on sold_quantity
        sold = listing.sold_quantity
        if sold is None:
            return DemandSignal(score=None, label="UNKNOWN")

        if sold > 100:
            label = "HIGH"
            score = Decimal("1.0")
        elif sold > 50:
            label = "MEDIUM"
            score = Decimal("0.6")
        elif sold > 10:
            label = "LOW"
            score = Decimal("0.2")
        else:
            label = "NONE"
            score = Decimal("0.0")

        return DemandSignal(score=score, label=label)

    def _calculate_price_signal(self, listing: MarketListing, median_price: Decimal) -> PriceSignal:
        if median_price == 0:
            ratio = Decimal("1.0")
        else:
            ratio = listing.price.amount / median_price

        if ratio < Decimal("0.8"):
            position = "UNDER_MARKET"
        elif ratio > Decimal("1.2"):
            position = "OVER_MARKET"
        else:
            position = "AT_MARKET"

        return PriceSignal(ratio=ratio, position=position)
