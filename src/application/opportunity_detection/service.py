import uuid
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    OpportunityDetectionCriteria,
)
from src.domain.opportunity_detection.ports import (
    OpportunityDetectionEnginePort,
    OpportunityRepositoryPort,
)
from src.domain.opportunity_detection.engine import OpportunityDetectionEngine
from src.domain.market_monitoring.ports import MarketObservationRepository
from src.domain.market_monitoring.models import MarketObservation


class OpportunityDetectionService:
    """
    Servicio de Aplicación para Detección de Oportunidades Comerciales (Hito J.3).

    Responsabilidades:
    - Coordinar la lectura de MarketObservation producidas por J.2.
    - Delegar la detección, scoring explicable y clasificación al OpportunityDetectionEngine de dominio.
    - Persistir las oportunidades resultantes de forma atómica, segura e idempotente en OpportunityRepositoryPort.
    - Servir consultas de oportunidades filtradas por producto, tipo o estado.

    Límites Arquitectónicos:
    - NO crea DecisionRecord ni ejecuta acciones comerciales.
    - NO consulta Mercado Libre directamente (consume MarketObservation).
    - NO muta políticas ni invoca PolicyEngine.
    """

    def __init__(
        self,
        opportunity_repository: OpportunityRepositoryPort,
        observation_repository: Optional[MarketObservationRepository] = None,
        detection_engine: Optional[OpportunityDetectionEnginePort] = None,
    ):
        self.opportunity_repository = opportunity_repository
        self.observation_repository = observation_repository
        self.detection_engine = detection_engine or OpportunityDetectionEngine()

    def process_observations(
        self,
        observations: List[MarketObservation],
        criteria: Optional[OpportunityDetectionCriteria] = None,
        correlation_id: Optional[str] = None,
    ) -> List[OpportunityRecord]:
        """
        Procesa una lista directa de observaciones de mercado en memoria,
        detecta oportunidades comerciales, las persiste y las retorna.
        """
        if not observations:
            return []

        corr_id = correlation_id or f"corr-opp-det-{uuid.uuid4().hex[:12]}"
        opportunities = self.detection_engine.detect_opportunities(
            observations=observations,
            criteria=criteria,
            correlation_id=corr_id,
        )

        if opportunities:
            self.opportunity_repository.save_all(opportunities)

        return opportunities

    def detect_from_repository(
        self,
        entity_id: Optional[str] = None,
        criteria: Optional[OpportunityDetectionCriteria] = None,
        limit: int = 100,
        correlation_id: Optional[str] = None,
    ) -> List[OpportunityRecord]:
        """
        Lee observaciones de mercado directamente desde el repositorio de observaciones J.2,
        ejecuta la detección y persiste las oportunidades encontradas.
        """
        if self.observation_repository is None:
            raise ValueError("observation_repository is required for detect_from_repository")

        corr_id = correlation_id or f"corr-opp-repo-{uuid.uuid4().hex[:12]}"

        if entity_id:
            observations = self.observation_repository.list_by_entity(entity_id=entity_id, limit=limit)
        else:
            observations = self.observation_repository.list_all(limit=limit)

        return self.process_observations(
            observations=observations,
            criteria=criteria,
            correlation_id=corr_id,
        )

    def get_opportunity(self, opportunity_id: str) -> Optional[OpportunityRecord]:
        """Obtiene una oportunidad por ID."""
        return self.opportunity_repository.get_by_id(opportunity_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[OpportunityRecord]:
        """Obtiene una oportunidad por clave de idempotencia determinista."""
        return self.opportunity_repository.get_by_idempotency_key(idempotency_key)

    def list_opportunities_for_product(self, canonical_product_id: str, limit: int = 100) -> List[OpportunityRecord]:
        """Lista oportunidades para un producto específico."""
        return self.opportunity_repository.list_by_product(canonical_product_id, limit=limit)

    def list_opportunities_by_type(self, opportunity_type: OpportunityType, limit: int = 100) -> List[OpportunityRecord]:
        """Lista oportunidades filtradas por tipo."""
        return self.opportunity_repository.list_by_type(opportunity_type, limit=limit)

    def list_opportunities_by_status(self, status: OpportunityStatus, limit: int = 100) -> List[OpportunityRecord]:
        """Lista oportunidades filtradas por estado."""
        return self.opportunity_repository.list_by_status(status, limit=limit)

    def list_all_opportunities(self, limit: int = 1000) -> List[OpportunityRecord]:
        """Lista todas las oportunidades persistidas."""
        return self.opportunity_repository.list_all(limit=limit)
