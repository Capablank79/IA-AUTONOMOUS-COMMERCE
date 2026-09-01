"""
Servicio de Aplicación para Detección de Cambios (Change Detection Service - Hito J.4).

Coordina:
- Comparación determinista de observaciones de mercado sucesivas (J.2 MarketObservation).
- Comparación determinista de oportunidades sucesivas (J.3 OpportunityRecord).
- Integración opcional con H.7 TemporalStateService para snapshots históricos.
- Persistencia durable, atómica e idempotente de ChangeRecord.
- Soporte para detección continua sin efectos secundarios comerciales.

Límites:
- NO crea DecisionRecord.
- NO ejecuta acciones comerciales.
- NO genera alertas distribuidas (J.6).
- NO emite eventos a un Event Bus (J.5).
- NO consulta directamente a marketplaces (consume observaciones de J.2).
"""

import uuid
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from src.domain.change_detection.models import (
    ChangeRecord,
    ChangeSubjectType,
    ChangeType,
)
from src.domain.change_detection.ports import (
    ChangeDetectionEnginePort,
    ChangeRecordRepositoryPort,
)
from src.domain.change_detection.engine import ChangeDetectionEngine
from src.domain.market_monitoring.models import MarketObservation
from src.domain.market_monitoring.ports import MarketObservationRepository
from src.domain.opportunity_detection.models import OpportunityRecord
from src.domain.opportunity_detection.ports import OpportunityRepositoryPort
from src.domain.temporal_state.ports import TemporalStateRepository


class ChangeDetectionService:
    """
    Servicio de Aplicación para Detección de Cambios (Hito J.4).
    """

    def __init__(
        self,
        change_repository: ChangeRecordRepositoryPort,
        observation_repository: Optional[MarketObservationRepository] = None,
        opportunity_repository: Optional[OpportunityRepositoryPort] = None,
        temporal_state_repository: Optional[TemporalStateRepository] = None,
        engine: Optional[ChangeDetectionEnginePort] = None,
    ):
        self.change_repository = change_repository
        self.observation_repository = observation_repository
        self.opportunity_repository = opportunity_repository
        self.temporal_state_repository = temporal_state_repository
        self.engine = engine or ChangeDetectionEngine()

    def detect_observation_changes(
        self,
        observations: List[MarketObservation],
        correlation_id: Optional[str] = None,
    ) -> List[ChangeRecord]:
        """
        Procesa una serie cronológica de observaciones para una o varias entidades.
        Detecta cambios deterministas entre pares consecutivos (T0 -> T1 -> T2),
        persiste los ChangeRecord resultantes de forma atómica e idempotente y los retorna.
        """
        if not observations:
            return []

        corr_id = correlation_id or f"corr-chg-obs-{uuid.uuid4().hex[:12]}"

        # Agrupar por entity_id
        grouped: Dict[str, List[MarketObservation]] = {}
        for obs in observations:
            grouped.setdefault(obs.entity_id, []).append(obs)

        all_changes: List[ChangeRecord] = []

        for entity_id, entity_obs_list in grouped.items():
            # Ordenar determinísticamente por observed_at
            sorted_obs = sorted(entity_obs_list, key=lambda x: x.observed_at)

            # Si tenemos repositorio de observaciones, buscar la última observación anterior al primer elemento
            prev_baseline: Optional[MarketObservation] = None
            if self.observation_repository:
                history = self.observation_repository.list_by_entity(entity_id=entity_id, limit=500)
                # Filtrar observaciones estrictamente anteriores al primer elemento
                earlier = [h for h in history if h.observed_at < sorted_obs[0].observed_at]
                if earlier:
                    prev_baseline = sorted(earlier, key=lambda h: h.observed_at)[-1]

            prev_obs = prev_baseline
            for current_obs in sorted_obs:
                change_rec = self.engine.compare_observations(
                    previous_observation=prev_obs,
                    current_observation=current_obs,
                    correlation_id=corr_id,
                )
                all_changes.append(change_rec)
                prev_obs = current_obs

        if all_changes:
            self.change_repository.save_all(all_changes)

        return all_changes

    def detect_opportunity_changes(
        self,
        opportunities: List[OpportunityRecord],
        correlation_id: Optional[str] = None,
    ) -> List[ChangeRecord]:
        """
        Procesa una serie de registros de oportunidad para uno o varios productos.
        Detecta transiciones de estado, variaciones de score y métricas observadas.
        """
        if not opportunities:
            return []

        corr_id = correlation_id or f"corr-chg-opp-{uuid.uuid4().hex[:12]}"

        grouped: Dict[str, List[OpportunityRecord]] = {}
        for opp in opportunities:
            grouped.setdefault(opp.canonical_product_id, []).append(opp)

        all_changes: List[ChangeRecord] = []

        for prod_id, opp_list in grouped.items():
            sorted_opps = sorted(opp_list, key=lambda x: x.detected_at)

            prev_baseline: Optional[OpportunityRecord] = None
            if self.opportunity_repository:
                history = self.opportunity_repository.list_by_product(canonical_product_id=prod_id, limit=500)
                earlier = [h for h in history if h.detected_at < sorted_opps[0].detected_at]
                if earlier:
                    prev_baseline = sorted(earlier, key=lambda h: h.detected_at)[-1]

            prev_opp = prev_baseline
            for current_opp in sorted_opps:
                change_rec = self.engine.compare_opportunities(
                    previous_opportunity=prev_opp,
                    current_opportunity=current_opp,
                    correlation_id=corr_id,
                )
                all_changes.append(change_rec)
                prev_opp = current_opp

        if all_changes:
            self.change_repository.save_all(all_changes)

        return all_changes

    def detect_changes_for_entity(
        self,
        entity_id: str,
        limit: int = 100,
        correlation_id: Optional[str] = None,
    ) -> List[ChangeRecord]:
        """
        Lee la historia de observaciones de una entidad desde el repositorio J.2,
        detecta toda la cadena de cambios históricos y los persiste.
        """
        if self.observation_repository is None:
            raise ValueError("observation_repository is required for detect_changes_for_entity")

        observations = self.observation_repository.list_by_entity(entity_id=entity_id, limit=limit)
        return self.detect_observation_changes(observations, correlation_id=correlation_id)

    def get_change(self, change_id: str) -> Optional[ChangeRecord]:
        """Obtiene un ChangeRecord por su ID."""
        return self.change_repository.get_by_id(change_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ChangeRecord]:
        """Obtiene un ChangeRecord por su clave determinista de idempotencia."""
        return self.change_repository.get_by_idempotency_key(idempotency_key)

    def list_changes_for_subject(
        self,
        subject_type: ChangeSubjectType,
        subject_id: str,
        limit: int = 100,
    ) -> List[ChangeRecord]:
        """Lista cambios para un sujeto específico."""
        return self.change_repository.list_by_subject(subject_type, subject_id, limit=limit)

    def list_all_changes(self, limit: int = 1000) -> List[ChangeRecord]:
        """Lista todos los cambios registrados."""
        return self.change_repository.list_all(limit=limit)
