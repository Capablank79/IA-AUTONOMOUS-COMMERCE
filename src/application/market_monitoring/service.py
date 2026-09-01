from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict, Any
import uuid

from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    ObservationSourceType,
    NormalizedPrice,
    ObservedSellerInfo,
    ObservedCompetitionInfo,
)
from src.domain.market_monitoring.ports import (
    MarketObservationSourcePort,
    MarketObservationRepository,
)
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType
from src.domain.scheduling.models import Clock, SystemClock


class MarketMonitoringService:
    """
    Servicio de Aplicación para Market Monitoring (Hito J.2).
    Orquesta la observación periódica y por demanda de fuentes de mercado,
    normaliza payloads, valida rangos numéricos y tipos, preserva UNKNOWN,
    gestiona errores de fuente sin fabricar datos falsos y persiste de forma idempotente.
    """

    def __init__(
        self,
        repository: MarketObservationRepository,
        sources: Optional[List[MarketObservationSourcePort]] = None,
        clock: Optional[Clock] = None,
    ):
        self.repository = repository
        self._sources: Dict[str, MarketObservationSourcePort] = {}
        self.clock = clock or SystemClock()

        if sources:
            for src in sources:
                self.register_source(src)

    def register_source(self, source: MarketObservationSourcePort) -> None:
        """Registra un adaptador de fuente de observación."""
        self._sources[source.source_name] = source

    def get_source(self, source_name: str) -> Optional[MarketObservationSourcePort]:
        return self._sources.get(source_name)

    def monitor(
        self,
        source_name: Optional[str] = None,
        query: Optional[str] = None,
        entity_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 50,
        correlation_id: Optional[str] = None,
    ) -> List[MarketObservation]:
        """
        Ejecuta ciclo de monitoreo.
        Si se especifica `source_name`, consulta esa fuente puntual; de lo contrario,
        consulta todas las fuentes registradas.
        Persiste todas las observaciones resultantes de forma idempotente y segura.
        """
        corr_id = correlation_id or f"mon-corr-{uuid.uuid4().hex[:12]}"
        sources_to_query = []

        if source_name:
            src = self.get_source(source_name)
            if not src:
                raise ValueError(f"Source '{source_name}' is not registered in MarketMonitoringService")
            sources_to_query.append(src)
        else:
            sources_to_query = list(self._sources.values())

        if not sources_to_query:
            return []

        all_observations: List[MarketObservation] = []

        for source in sources_to_query:
            try:
                obs_list = source.observe(
                    query=query,
                    entity_id=entity_id,
                    category=category,
                    limit=limit,
                    correlation_id=corr_id,
                )
                for obs in obs_list:
                    # Validar y normalizar antes de guardar
                    validated_obs = self._validate_and_normalize(obs)
                    all_observations.append(validated_obs)
            except Exception as e:
                # Si una fuente falla de forma no controlada a nivel adapter,
                # capturamos y registramos una observación con SOURCE_FAILURE
                now = self.clock.now()
                failure_obs = MarketObservation(
                    observation_id=f"obs-fail-{uuid.uuid4().hex[:12]}",
                    source=source.source_name,
                    source_type=ObservationSourceType.MARKETPLACE_API,
                    observed_at=now,
                    collected_at=now,
                    marketplace=Marketplace.GENERIC,
                    entity_id=entity_id or query or "UNKNOWN_ENTITY",
                    status=ObservationStatus.SOURCE_FAILURE,
                    provenance="SYSTEM",
                    confidence=Confidence.UNKNOWN,
                    signal_type=SignalType.OBSERVED,
                    correlation_id=corr_id,
                    error_message=f"Unhandled exception in source {source.source_name}: {str(e)}",
                )
                all_observations.append(failure_obs)

        # Persistencia en repositorio de forma atómica e idempotente
        self.repository.save_all(all_observations)
        return all_observations

    def _validate_and_normalize(self, obs: MarketObservation) -> MarketObservation:
        """
        Valida que los datos observados cumplan con las reglas de calidad de J.2:
        - Si el precio es negativo, falla / rechaza según contrato.
        - Si el stock es negativo, rechaza.
        - Preserva UNKNOWN (None permanece None, no se convierte en 0).
        """
        if obs.price is not None:
            if obs.price.amount < Decimal("0"):
                raise ValueError(f"Invalid negative price in observation {obs.observation_id}: {obs.price.amount}")

        if obs.stock is not None and obs.stock < 0:
            raise ValueError(f"Invalid negative stock in observation {obs.observation_id}: {obs.stock}")

        if obs.sold_quantity is not None and obs.sold_quantity < 0:
            raise ValueError(f"Invalid negative sold_quantity in observation {obs.observation_id}: {obs.sold_quantity}")

        return obs
