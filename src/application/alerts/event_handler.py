"""
Manejador de Eventos para Alertas Autónomas (Autonomous Alerts - Hito J.6).

Implementa EventHandlerPort de J.5 para consumir eventos del EventBus y delegar
en AlertService la evaluación, persistencia y despacho.

Límites:
- NO acopla J.5 al dominio de alertas.
- NO emite decisiones ni ejecuta acciones comerciales.
- Idempotente y con aislamiento de fallos.
"""

from typing import Optional
from src.domain.events.models import EventRecord, EventType
from src.domain.events.ports import EventHandlerPort
from src.application.alerts.alert_service import AlertService


class AutonomousAlertEventHandler(EventHandlerPort):
    """
    Consumidor de eventos para el sistema de alertas autónomas.
    """

    def __init__(self, alert_service: AlertService, handler_id: str = "autonomous-alert-handler"):
        if not alert_service or not isinstance(alert_service, AlertService):
            raise ValueError("alert_service must be an instance of AlertService")
        self._alert_service = alert_service
        self._handler_id = handler_id

    @property
    def handler_id(self) -> str:
        return self._handler_id

    def can_handle(self, event_type: EventType) -> bool:
        # Evalúa eventos de cambio, oportunidad y observaciones de mercado (por source failure)
        return event_type in (
            EventType.CHANGE_DETECTED,
            EventType.OPPORTUNITY_DETECTED,
            EventType.MARKET_OBSERVATION_CREATED,
        )

    def handle(self, event: EventRecord) -> None:
        """
        Procesa el evento mediante AlertService.
        """
        self._alert_service.process_event(event)
