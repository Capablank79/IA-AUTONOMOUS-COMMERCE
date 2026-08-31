import threading
from typing import Dict, List, Optional, Sequence, Set, Tuple

from src.domain.fulfillment.models import Shipment, TrackingEvent
from src.domain.fulfillment.ports import FulfillmentRepositoryPort


class InMemoryFulfillmentRepository(FulfillmentRepositoryPort):
    """
    Repositorio en memoria thread-safe para Shipments, eventos de tracking e idempotencia logística.
    Permite aislamiento completo en tests y desacoplamiento de bases de datos.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._shipments_by_id: Dict[str, Shipment] = {}
        self._shipments_by_external_key: Dict[Tuple[str, str], Shipment] = {}  # (channel_id, external_shipment_id)
        self._shipments_by_order_id: Dict[str, Shipment] = {}                  # order_id -> Shipment
        self._shipments_by_order_key: Dict[Tuple[str, str], Shipment] = {}     # (channel_id, external_order_id)
        self._tracking_events_by_shipment: Dict[str, List[TrackingEvent]] = {}
        self._processed_events: Set[str] = set()
        self._processed_idempotency_keys: Set[str] = set()

    def save_shipment(self, shipment: Shipment) -> None:
        with self._lock:
            self._shipments_by_id[shipment.shipment_id] = shipment
            self._shipments_by_external_key[(shipment.channel.channel_id, shipment.external_shipment_id)] = shipment
            if shipment.order_id:
                self._shipments_by_order_id[shipment.order_id] = shipment
            if shipment.external_order_id:
                self._shipments_by_order_key[(shipment.channel.channel_id, shipment.external_order_id)] = shipment
            if shipment.idempotency_key:
                self._processed_idempotency_keys.add(shipment.idempotency_key)

    def get_shipment_by_id(self, shipment_id: str) -> Optional[Shipment]:
        with self._lock:
            return self._shipments_by_id.get(shipment_id)

    def get_shipment_by_external_id(
        self,
        external_shipment_id: str,
        channel_id: str,
    ) -> Optional[Shipment]:
        with self._lock:
            return self._shipments_by_external_key.get((channel_id, external_shipment_id))

    def get_shipment_by_order_id(
        self,
        order_id: str,
    ) -> Optional[Shipment]:
        with self._lock:
            return self._shipments_by_order_id.get(order_id)

    def get_shipment_by_external_order_id(
        self,
        external_order_id: str,
        channel_id: str,
    ) -> Optional[Shipment]:
        with self._lock:
            return self._shipments_by_order_key.get((channel_id, external_order_id))

    def list_shipments(
        self,
        channel_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Shipment]:
        with self._lock:
            results: List[Shipment] = list(self._shipments_by_id.values())
            if channel_id:
                results = [s for s in results if s.channel.channel_id == channel_id]
            if status:
                results = [s for s in results if s.status.value == status]

            # Ordenar por fecha de creación desc
            results.sort(key=lambda s: s.created_at, reverse=True)
            return tuple(results[offset : offset + limit])

    def save_tracking_event(self, event: TrackingEvent) -> bool:
        with self._lock:
            if event.shipment_id not in self._tracking_events_by_shipment:
                self._tracking_events_by_shipment[event.shipment_id] = []
            
            events = self._tracking_events_by_shipment[event.shipment_id]
            # Deduplicar por event_id
            if any(e.event_id == event.event_id for e in events):
                return False

            events.append(event)

            # Si el shipment existe en memoria, actualizar su lista inmutable de tracking events
            shipment = self._shipments_by_id.get(event.shipment_id)
            if shipment:
                updated_events = tuple(events)
                updated_shipment = Shipment(
                    shipment_id=shipment.shipment_id,
                    external_shipment_id=shipment.external_shipment_id,
                    order_id=shipment.order_id,
                    external_order_id=shipment.external_order_id,
                    channel=shipment.channel,
                    status=event.normalized_status if event.normalized_status else shipment.status,
                    carrier=shipment.carrier,
                    service_level=shipment.service_level,
                    tracking_number=shipment.tracking_number,
                    tracking_url=shipment.tracking_url,
                    label=shipment.label,
                    tracking_events=updated_events,
                    created_at=shipment.created_at,
                    updated_at=event.timestamp,
                    shipped_at=shipment.shipped_at,
                    delivered_at=event.timestamp if event.normalized_status == shipment.status.DELIVERED else shipment.delivered_at,
                    correlation_id=event.correlation_id or shipment.correlation_id,
                    idempotency_key=shipment.idempotency_key,
                    provenance=event.provenance,
                    confidence=event.confidence,
                    raw_reference=shipment.raw_reference,
                )
                self.save_shipment(updated_shipment)
            return True

    def get_tracking_events(self, shipment_id: str) -> Sequence[TrackingEvent]:
        with self._lock:
            events = self._tracking_events_by_shipment.get(shipment_id, [])
            return tuple(sorted(events, key=lambda e: e.timestamp))

    # Alias para compatibilidad
    def get_tracking_history(self, shipment_id: str) -> Sequence[TrackingEvent]:
        return self.get_tracking_events(shipment_id)

    def record_processed_fulfillment_event(
        self,
        event_id: str,
        idempotency_key: str,
        external_shipment_id: str,
    ) -> bool:
        with self._lock:
            key_combo = f"{event_id}:{idempotency_key}:{external_shipment_id}"
            if key_combo in self._processed_events:
                return False
            if idempotency_key and idempotency_key in self._processed_idempotency_keys:
                return False

            self._processed_events.add(key_combo)
            if idempotency_key:
                self._processed_idempotency_keys.add(idempotency_key)
            return True

    # Alias para compatibilidad
    def record_processed_event(
        self,
        event_id: str,
        idempotency_key: str,
        external_shipment_id: str,
    ) -> bool:
        return self.record_processed_fulfillment_event(event_id, idempotency_key, external_shipment_id)

    def is_fulfillment_event_processed(self, event_id: str, idempotency_key: str) -> bool:
        with self._lock:
            if idempotency_key and idempotency_key in self._processed_idempotency_keys:
                return True
            for ev in self._processed_events:
                if ev.startswith(f"{event_id}:"):
                    return True
            return False

    # Alias para compatibilidad
    def is_event_processed(self, event_id: str, idempotency_key: str) -> bool:
        return self.is_fulfillment_event_processed(event_id, idempotency_key)
