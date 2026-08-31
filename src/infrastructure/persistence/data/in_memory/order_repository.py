import threading
from typing import Dict, List, Optional, Sequence, Set, Tuple

from src.domain.order.models import Order
from src.domain.order.ports import OrderRepositoryPort


class InMemoryOrderRepository(OrderRepositoryPort):
    """
    Repositorio en memoria thread-safe para órdenes y control de idempotencia/eventos.
    Permite testing aislado y ejecución sin dependencias de base de datos pesadas.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._orders_by_id: Dict[str, Order] = {}
        self._orders_by_external_key: Dict[Tuple[str, str], Order] = {}
        self._processed_events: Set[str] = set()
        self._processed_idempotency_keys: Set[str] = set()

    def save_order(self, order: Order) -> None:
        with self._lock:
            self._orders_by_id[order.order_id] = order
            self._orders_by_external_key[(order.channel.channel_id, order.external_order_id)] = order
            if order.idempotency_key:
                self._processed_idempotency_keys.add(order.idempotency_key)

    def get_order_by_id(self, order_id: str) -> Optional[Order]:
        with self._lock:
            return self._orders_by_id.get(order_id)

    def get_order_by_external_id(
        self,
        external_order_id: str,
        channel_id: str,
    ) -> Optional[Order]:
        with self._lock:
            return self._orders_by_external_key.get((channel_id, external_order_id))

    def list_orders(
        self,
        channel_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Order]:
        with self._lock:
            results: List[Order] = list(self._orders_by_id.values())
            if channel_id:
                results = [o for o in results if o.channel.channel_id == channel_id]
            if status:
                results = [o for o in results if o.status.value == status]

            # Ordenar por fecha de creación desc
            results.sort(key=lambda o: o.created_at, reverse=True)
            return tuple(results[offset : offset + limit])

    def record_processed_event(
        self,
        event_id: str,
        idempotency_key: str,
        external_order_id: str,
    ) -> bool:
        with self._lock:
            key_combo = f"{event_id}:{idempotency_key}:{external_order_id}"
            if key_combo in self._processed_events:
                return False
            if idempotency_key and idempotency_key in self._processed_idempotency_keys:
                return False

            self._processed_events.add(key_combo)
            if idempotency_key:
                self._processed_idempotency_keys.add(idempotency_key)
            return True

    def is_event_processed(self, event_id: str, idempotency_key: str) -> bool:
        with self._lock:
            if idempotency_key and idempotency_key in self._processed_idempotency_keys:
                return True
            for ev in self._processed_events:
                if ev.startswith(f"{event_id}:"):
                    return True
            return False
