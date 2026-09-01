"""
Módulo de aplicación para gestión y despacho de eventos (Hito J.5).
"""

from src.application.events.event_bus_service import EventBusService
from src.application.events.integration_service import EventIntegrationService
from src.application.events.adapters import (
    build_change_detected_event,
    build_market_observation_event,
    build_opportunity_detected_event,
)

__all__ = [
    "EventBusService",
    "EventIntegrationService",
    "build_change_detected_event",
    "build_market_observation_event",
    "build_opportunity_detected_event",
]
