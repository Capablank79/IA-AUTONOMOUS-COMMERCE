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
    MarketListing
)

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

            opportunity_score = (
                demand_signal.score * Decimal("0.50")
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
