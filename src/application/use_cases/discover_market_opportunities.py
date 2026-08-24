from typing import List
from src.domain.market_intelligence.models import (
    SearchCriteria,
    MarketOpportunity,
    MarketSnapshot
)
from src.domain.market_intelligence.ports import (
    MarketplaceDataSource,
    MarketSnapshotRepository
)
from src.domain.market_intelligence.services import MarketAnalysisService

class DiscoverMarketOpportunitiesUseCase:
    """
    Application Use Case for discovering market opportunities.
    Orchestrates data fetching, persistence, and analysis.
    """
    
    def __init__(
        self,
        data_source: MarketplaceDataSource,
        repository: MarketSnapshotRepository,
        analysis_service: MarketAnalysisService
    ):
        self.data_source = data_source
        self.repository = repository
        self.analysis_service = analysis_service

    def execute(self, criteria: SearchCriteria) -> List[MarketOpportunity]:
        # 1. Obtener MarketSnapshot mediante MarketplaceDataSource.
        snapshot = self.data_source.fetch_snapshot(criteria)
        
        # 2. Persistir el Snapshot mediante MarketSnapshotRepository.
        self.repository.save(snapshot)
        
        # 3. Ejecutar MarketAnalysisService.
        # 4. Obtener List[MarketOpportunity].
        opportunities = self.analysis_service.analyze(snapshot)
        
        # Ordenar las oportunidades por opportunity_score descendente
        opportunities.sort(key=lambda opp: opp.opportunity_score, reverse=True)
        
        # 5. Retornar las oportunidades.
        return opportunities
