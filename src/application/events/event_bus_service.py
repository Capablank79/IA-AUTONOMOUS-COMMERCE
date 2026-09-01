"""
Servicio de Event Bus e infraestructura de procesamiento de eventos (Hito J.5).

Implementa un bus de eventos in-process, determinista y durable:
- Registro de manejadores desacoplados (EventHandlerPort).
- Publicación persistente (append a EventStorePort).
- Despacho y entrega at-least-once a consumidores registrados.
- Idempotencia de consumo por (event_id, handler_id).
- Aislamiento riguroso de fallos entre manejadores (failure isolation).
- Replay determinista sin duplicación de efectos secundarios.
- Preservación de correlation_id, causation_id, provenance y UNKNOWN.
- Sanitización y exclusión de credenciales/secretos.
- Cero creación de decisiones de negocio, acciones de marketplace, alertas (J.6) o misiones continuas (J.7).
"""

import time
import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
from types import MappingProxyType

from src.domain.events.models import (
    EventRecord,
    EventType,
    DeliveryRecord,
    DeliveryStatus,
)
from src.domain.events.ports import (
    EventHandlerPort,
    EventStorePort,
    EventPublisherPort,
)

logger = logging.getLogger(__name__)


class EventBusService(EventPublisherPort):
    """
    Servicio de Event Bus In-Process para Hito J.5.
    """

    def __init__(self, event_store: EventStorePort):
        self.event_store = event_store
        self._handlers: Dict[str, EventHandlerPort] = {}

    def register_handler(self, handler: EventHandlerPort) -> None:
        """
        Registra un manejador de eventos en el bus.
        """
        if not handler or not isinstance(handler, EventHandlerPort):
            raise ValueError("handler must implement EventHandlerPort")
        if not handler.handler_id:
            raise ValueError("handler_id must be a non-empty string")
        self._handlers[handler.handler_id] = handler

    def unregister_handler(self, handler_id: str) -> None:
        """
        Desregistra un manejador de eventos del bus.
        """
        if handler_id in self._handlers:
            del self._handlers[handler_id]

    def list_handlers(self) -> List[str]:
        """
        Retorna la lista de identificadores de manejadores registrados.
        """
        return list(self._handlers.keys())

    def publish(self, event: EventRecord) -> List[DeliveryRecord]:
        """
        Publica un evento:
        1. Persiste el evento en el EventStore de forma inmutable e idempotente.
        2. Despacha el evento a cada handler registrado compatible.
        3. Aísla fallos de ejecución por handler y registra el DeliveryRecord resultante.
        4. Retorna la lista de DeliveryRecords obtenidos.
        """
        if not isinstance(event, EventRecord):
            raise ValueError("event must be an instance of EventRecord")

        # 1. Persistir evento en store
        persisted_event = self.event_store.append(event)

        # 2. Despachar a handlers
        delivery_results: List[DeliveryRecord] = []

        for handler_id, handler in list(self._handlers.items()):
            if not handler.can_handle(persisted_event.event_type):
                continue

            delivery_record = self._deliver_to_handler(persisted_event, handler)
            delivery_results.append(delivery_record)

        return delivery_results

    def _deliver_to_handler(
        self,
        event: EventRecord,
        handler: EventHandlerPort,
        force_replay: bool = False,
    ) -> DeliveryRecord:
        """
        Entrega el evento a un handler específico garantizando idempotencia y aislamiento de fallos.
        """
        handler_id = handler.handler_id
        existing_delivery = self.event_store.get_delivery(event.event_id, handler_id)

        # Si ya fue entregado exitosamente y no es un force_replay que requiera re-ejecución:
        if existing_delivery and existing_delivery.status == DeliveryStatus.DELIVERED and not force_replay:
            # Idempotencia: no re-ejecutar lógica de negocio
            return existing_delivery

        attempt_count = (existing_delivery.attempt_count + 1) if existing_delivery else 1
        now = datetime.now(timezone.utc)
        first_attempt = existing_delivery.first_attempted_at if existing_delivery else now

        start_time = time.perf_counter()
        try:
            handler.handle(event)
            duration_ms = (time.perf_counter() - start_time) * 1000.0

            delivery = DeliveryRecord(
                delivery_id=f"deliv-{event.event_id}-{handler_id}-{attempt_count}",
                event_id=event.event_id,
                handler_id=handler_id,
                status=DeliveryStatus.DELIVERED,
                attempt_count=attempt_count,
                first_attempted_at=first_attempt,
                last_attempted_at=now,
                error_message=None,
                execution_duration_ms=round(duration_ms, 2),
                metadata=MappingProxyType({"handler_name": handler.__class__.__name__}),
            )
            return self.event_store.record_delivery(delivery)

        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            err_msg = str(exc)
            logger.warning(
                "Handler %s falló al procesar evento %s: %s",
                handler_id,
                event.event_id,
                err_msg,
            )

            delivery = DeliveryRecord(
                delivery_id=f"deliv-{event.event_id}-{handler_id}-{attempt_count}",
                event_id=event.event_id,
                handler_id=handler_id,
                status=DeliveryStatus.FAILED,
                attempt_count=attempt_count,
                first_attempted_at=first_attempt,
                last_attempted_at=now,
                error_message=err_msg,
                execution_duration_ms=round(duration_ms, 2),
                metadata=MappingProxyType({
                    "handler_name": handler.__class__.__name__,
                    "exception_type": type(exc).__name__,
                }),
            )
            return self.event_store.record_delivery(delivery)

    def replay_events(
        self,
        event_type: Optional[EventType] = None,
        subject_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        handler_id: Optional[str] = None,
        force: bool = False,
    ) -> List[DeliveryRecord]:
        """
        Reproduce eventos históricos persistidos en orden determinista.
        Si force=False, respeta la idempotencia (los handlers ya ejecutados exitosamente no duplican efectos).
        """
        events = self.event_store.list_events(
            event_type=event_type,
            subject_id=subject_id,
            correlation_id=correlation_id,
        )

        results: List[DeliveryRecord] = []
        for event in events:
            if handler_id:
                handler = self._handlers.get(handler_id)
                if handler and handler.can_handle(event.event_type):
                    deliv = self._deliver_to_handler(event, handler, force_replay=force)
                    results.append(deliv)
            else:
                for h_id, handler in self._handlers.items():
                    if handler.can_handle(event.event_type):
                        deliv = self._deliver_to_handler(event, handler, force_replay=force)
                        results.append(deliv)

        return results
