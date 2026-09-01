"""
Servicio de Aplicación para Integración y Publicación de Eventos (Event Publishing Service - Hito J.5).

Proporciona un puente de alto nivel para conectar:
- Detección de Cambios (J.4) -> EventBus.publish(ChangeDetectedEvent)
- Monitoreo de Mercado (J.2) -> EventBus.publish(MarketObservationCreatedEvent)
- Detección de Oportunidades (J.3) -> EventBus.publish(OpportunityDetectedEvent)

Mantiene la total separación ontológica:
FACT (J.2, J.3, J.4) -> EVENT (J.5) -> CONSUMER (Handlers)
"""

from typing import List, Optional

from src.domain.events.models import EventRecord, DeliveryRecord
from src.domain.events.ports import EventPublisherPort
from src.domain.change_detection.models import ChangeRecord
from src.domain.market_monitoring.models import MarketObservation
from src.domain.opportunity_detection.models import OpportunityRecord
from src.application.events.adapters import (
    build_change_detected_event,
    build_market_observation_event,
    build_opportunity_detected_event,
)


class EventIntegrationService:
    """
    Servicio que coordina la emisión de eventos de dominio hacia el EventBus.
    """

    def __init__(self, event_bus: EventPublisherPort):
        self.event_bus = event_bus

    def publish_change(self, change_record: ChangeRecord) -> List[DeliveryRecord]:
        """
        Transforma un ChangeRecord (J.4) en un EventRecord canónico y lo publica en el bus.
        """
        event = build_change_detected_event(change_record)
        return self.event_bus.publish(event)

    def publish_changes(self, change_records: List[ChangeRecord]) -> List[DeliveryRecord]:
        """
        Publica múltiples ChangeRecords preservando orden y recolectando entregas.
        """
        results: List[DeliveryRecord] = []
        for chg in change_records:
            results.extend(self.publish_change(chg))
        return results

    def publish_observation(self, observation: MarketObservation) -> List[DeliveryRecord]:
        """
        Transforma un MarketObservation (J.2) en un EventRecord canónico y lo publica en el bus.
        """
        event = build_market_observation_event(observation)
        return self.event_bus.publish(event)

    def publish_opportunity(self, opportunity: OpportunityRecord) -> List[DeliveryRecord]:
        """
        Transforma un OpportunityRecord (J.3) en un EventRecord canónico y lo publica en el bus.
        """
        event = build_opportunity_detected_event(opportunity)
        return self.event_bus.publish(event)
