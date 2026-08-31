import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

from src.domain.returns.models import Claim, Return, ReturnEvent, ReturnStatus
from src.domain.returns.ports import ReturnsRepositoryPort


class InMemoryReturnsRepository(ReturnsRepositoryPort):
    """
    Repositorio en memoria thread-safe para Devoluciones, Reclamos, eventos de auditoría e idempotencia (G.8).
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._returns_by_id: Dict[str, Return] = {}
        self._returns_by_external_key: Dict[Tuple[str, str], Return] = {}   # (channel_id, external_return_id)
        self._returns_by_order_id: Dict[str, Return] = {}                   # order_id -> Return
        self._returns_by_order_key: Dict[Tuple[str, str], Return] = {}      # (channel_id, external_order_id)
        
        self._claims_by_id: Dict[str, Claim] = {}
        self._claims_by_external_key: Dict[Tuple[str, str], Claim] = {}     # (channel_id, external_claim_id)
        
        self._events_by_return: Dict[str, List[ReturnEvent]] = {}
        self._processed_events: Set[str] = set()
        self._executed_idempotency_keys: Set[str] = set()

    def save_return(self, ret: Return) -> None:
        with self._lock:
            self._returns_by_id[ret.return_id] = ret
            self._returns_by_external_key[(ret.channel.channel_id, ret.external_return_id)] = ret
            if ret.order_id:
                self._returns_by_order_id[ret.order_id] = ret
            if ret.external_order_id:
                self._returns_by_order_key[(ret.channel.channel_id, ret.external_order_id)] = ret
            if ret.idempotency_key:
                self._executed_idempotency_keys.add(ret.idempotency_key)

    def get_return_by_id(self, return_id: str) -> Optional[Return]:
        with self._lock:
            return self._returns_by_id.get(return_id)

    def get_return_by_external_id(
        self,
        external_return_id: str,
        channel_id: str,
    ) -> Optional[Return]:
        with self._lock:
            return self._returns_by_external_key.get((channel_id, external_return_id))

    def get_return_by_order_id(self, order_id: str) -> Optional[Return]:
        with self._lock:
            return self._returns_by_order_id.get(order_id)

    def get_return_by_external_order_id(
        self,
        external_order_id: str,
        channel_id: str,
    ) -> Optional[Return]:
        with self._lock:
            return self._returns_by_order_key.get((channel_id, external_order_id))

    def list_returns(
        self,
        channel_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Return]:
        with self._lock:
            results: List[Return] = list(self._returns_by_id.values())
            if channel_id:
                results = [r for r in results if r.channel.channel_id == channel_id]
            if status:
                results = [r for r in results if r.status.value == status]

            results.sort(key=lambda r: r.created_at, reverse=True)
            return tuple(results[offset : offset + limit])

    def save_claim(self, claim: Claim) -> None:
        with self._lock:
            self._claims_by_id[claim.claim_id] = claim
            self._claims_by_external_key[(claim.channel.channel_id, claim.external_claim_id)] = claim
            if claim.idempotency_key:
                self._executed_idempotency_keys.add(claim.idempotency_key)

    def get_claim_by_id(self, claim_id: str) -> Optional[Claim]:
        with self._lock:
            return self._claims_by_id.get(claim_id)

    def get_claim_by_external_id(
        self,
        external_claim_id: str,
        channel_id: str,
    ) -> Optional[Claim]:
        with self._lock:
            return self._claims_by_external_key.get((channel_id, external_claim_id))

    def save_return_event(self, event: ReturnEvent) -> bool:
        with self._lock:
            if event.return_id not in self._events_by_return:
                self._events_by_return[event.return_id] = []

            events = self._events_by_return[event.return_id]
            if any(e.event_id == event.event_id for e in events):
                return False

            events.append(event)

            # Actualizar eventos y estado en la entidad Return si existe
            ret = self._returns_by_id.get(event.return_id)
            if ret:
                updated_events = tuple(events)
                updated_return = Return(
                    return_id=ret.return_id,
                    external_return_id=ret.external_return_id,
                    order_id=ret.order_id,
                    external_order_id=ret.external_order_id,
                    channel=ret.channel,
                    status=event.to_status if event.to_status != ReturnStatus.UNKNOWN else ret.status,
                    reason=ret.reason,
                    resolution=ret.resolution,
                    shipment_id=ret.shipment_id,
                    external_shipment_id=ret.external_shipment_id,
                    claim_id=ret.claim_id,
                    refund=ret.refund,
                    events=updated_events,
                    created_at=ret.created_at,
                    updated_at=datetime.now(timezone.utc),
                    closed_at=ret.closed_at,
                    correlation_id=ret.correlation_id,
                    idempotency_key=ret.idempotency_key,
                    provenance=ret.provenance,
                    confidence=ret.confidence,
                    raw_reference=ret.raw_reference,
                )
                self._returns_by_id[ret.return_id] = updated_return
                self._returns_by_external_key[(ret.channel.channel_id, ret.external_return_id)] = updated_return
                if ret.external_order_id:
                    self._returns_by_order_key[(ret.channel.channel_id, ret.external_order_id)] = updated_return

            return True

    def get_events_for_return(self, return_id: str) -> Sequence[ReturnEvent]:
        with self._lock:
            return tuple(self._events_by_return.get(return_id, []))

    def is_event_processed(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._processed_events

    def record_processed_event(self, event_id: str) -> None:
        with self._lock:
            self._processed_events.add(event_id)

    def is_idempotency_key_executed(self, idempotency_key: str) -> bool:
        with self._lock:
            return idempotency_key in self._executed_idempotency_keys

    def record_executed_idempotency_key(self, idempotency_key: str) -> None:
        with self._lock:
            self._executed_idempotency_keys.add(idempotency_key)
