from typing import List

from src.domain.market_intelligence.models import MarketListing, MarketEvidence
from src.domain.market_intelligence.services import MarketEvidenceComposer
from src.application.market_intelligence.traffic_intelligence_service import TrafficIntelligenceService

class MarketEvidenceEnrichmentService:
    """
    Orchestrator service that enriches MarketListings with market intelligence signals
    (currently VisitSignal) to produce MarketEvidence.
    """

    def __init__(
        self,
        traffic_service: TrafficIntelligenceService,
        evidence_composer: MarketEvidenceComposer,
    ):
        self.traffic_service = traffic_service
        self.evidence_composer = evidence_composer

    def enrich_listing(
        self,
        user_id: str,
        listing: MarketListing,
        window_days: int,
    ) -> MarketEvidence:
        """
        Enriches a single listing with traffic signals to produce MarketEvidence.
        
        Error Policy:
        Any exception raised by TrafficIntelligenceService (e.g. API errors, rate limits)
        is deliberately propagated to the caller (Option A). This avoids silently 
        converting errors into VisitSignal(None), preserving explicit error handling.
        """
        # 1. Obtener la señal de visitas usando el external_id del listing como item_id
        visit_signal = self.traffic_service.get_visits(
            user_id=user_id,
            item_id=listing.external_id,
            window_days=window_days,
        )

        # 2. Componer la evidencia de mercado conservando el listing original
        evidence = self.evidence_composer.compose(
            listing=listing,
            visit_signal=visit_signal,
        )

        return evidence

    def enrich_listings(
        self,
        user_id: str,
        listings: List[MarketListing],
        window_days: int,
    ) -> List[MarketEvidence]:
        """
        Enriches multiple listings sequentially.
        No parallelism or async/await is introduced as per constraints.
        If an error occurs, it propagates and halts the enrichment process.
        """
        return [
            self.enrich_listing(user_id=user_id, listing=listing, window_days=window_days)
            for listing in listings
        ]
