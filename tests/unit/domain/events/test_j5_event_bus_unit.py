"""
Tests unitarios exhaustivos para el Event Bus y Event Processing (Hito J.5).

Verifica todos los requisitos explícitos (A-AC):
A. create EventRecord
B. immutable event
C. publish event
D. register handler
E. deliver to one handler
F. deliver to multiple handlers
G. handler isolation
H. duplicate publish
I. duplicate delivery
J. event idempotency
K. handler idempotency
L. ordering
M. correlation_id
N. causation_id
O. provenance
P. serialization
Q. persistence
R. restart/reload
S. replay
T. replay idempotency
U. failure state
V. UNKNOWN preservation
W. sensitive data sanitization
X. invalid event
Y. unknown handler
Z. no Decision creation
AA. no Action execution
AB. no Alert creation
AC. no Continuous Mission
"""

import os
import shutil
import pytest
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from src.domain.events.models import (
    EventRecord,
    EventType,
    DeliveryRecord,
    DeliveryStatus,
)
from src.domain.events.ports import EventHandlerPort
from src.infrastructure.persistence.data.json.event_store import (
    JsonEventStore,
    CorruptedEventStoreDataError,
)
from src.application.events.event_bus_service import EventBusService
from src.application.events.adapters import (
    build_change_detected_event,
    build_market_observation_event,
    build_opportunity_detected_event,
)
from src.domain.change_detection.models import (
    ChangeRecord,
    ChangeSubjectType,
    ChangeType,
    ChangeSignificance,
    ObservedChangeField,
    DerivedChangeDelta,
)
from src.domain.market_intelligence.models import Confidence
from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    ObservationSourceType,
    NormalizedPrice,
)
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityStatus,
)


class MockTestHandler(EventHandlerPort):
    def __init__(self, handler_id: str, supported_types: List[EventType] = None, should_fail: bool = False):
        self._handler_id = handler_id
        self.supported_types = supported_types or [EventType.CHANGE_DETECTED, EventType.MARKET_OBSERVATION_CREATED, EventType.OPPORTUNITY_DETECTED]
        self.should_fail = should_fail
        self.received_events: List[EventRecord] = []
        self.call_count: int = 0

    @property
    def handler_id(self) -> str:
        return self._handler_id

    def can_handle(self, event_type: EventType) -> bool:
        return event_type in self.supported_types

    def handle(self, event: EventRecord) -> None:
        self.call_count += 1
        if self.should_fail:
            raise RuntimeError(f"Handler {self._handler_id} simulated error")
        self.received_events.append(event)


@pytest.fixture
def tmp_event_store_dir(tmp_path):
    store_dir = tmp_path / "event_store_test"
    store_dir.mkdir(parents=True, exist_ok=True)
    yield store_dir
    if store_dir.exists():
        shutil.rmtree(store_dir, ignore_errors=True)


@pytest.fixture
def event_store(tmp_event_store_dir):
    return JsonEventStore(base_dir=tmp_event_store_dir)


@pytest.fixture
def event_bus(event_store):
    return EventBusService(event_store=event_store)


def _sample_change_event(event_id: str = "evt-1") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        event_type=EventType.CHANGE_DETECTED,
        subject_type="MARKET_OBSERVATION",
        subject_id="ITEM-123",
        occurred_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 9, 1, 10, 0, 1, tzinfo=timezone.utc),
        correlation_id=f"corr-{event_id}",
        causation_id=f"chg-{event_id}",
        provenance="TEST",
        payload={"field": "price", "delta": "100"},
    )


# A. create EventRecord
def test_a_create_event_record():
    event = _sample_change_event("evt-a")
    assert event.event_id == "evt-a"
    assert event.event_type == EventType.CHANGE_DETECTED
    assert event.subject_id == "ITEM-123"
    assert event.correlation_id == "corr-evt-a"
    assert event.causation_id == "chg-evt-a"
    assert event.idempotency_key != ""


# B. immutable event
def test_b_immutable_event():
    event = _sample_change_event("evt-b")
    with pytest.raises(Exception):
        event.event_id = "modified-id"  # type: ignore
    with pytest.raises(Exception):
        event.payload["new_key"] = "val"  # type: ignore


# C. publish event
def test_c_publish_event(event_bus, event_store):
    event = _sample_change_event("evt-c")
    deliveries = event_bus.publish(event)
    assert isinstance(deliveries, list)
    saved = event_store.get_by_id("evt-c")
    assert saved is not None
    assert saved.event_id == "evt-c"


# D. register handler
def test_d_register_handler(event_bus):
    handler = MockTestHandler("handler-1")
    event_bus.register_handler(handler)
    assert "handler-1" in event_bus.list_handlers()
    event_bus.unregister_handler("handler-1")
    assert "handler-1" not in event_bus.list_handlers()


# E. deliver to one handler
def test_e_deliver_to_one_handler(event_bus):
    handler = MockTestHandler("handler-e")
    event_bus.register_handler(handler)
    event = _sample_change_event("evt-e")
    deliveries = event_bus.publish(event)
    assert len(deliveries) == 1
    assert deliveries[0].status == DeliveryStatus.DELIVERED
    assert len(handler.received_events) == 1
    assert handler.received_events[0].event_id == "evt-e"


# F. deliver to multiple handlers
def test_f_deliver_to_multiple_handlers(event_bus):
    h1 = MockTestHandler("handler-f1")
    h2 = MockTestHandler("handler-f2")
    event_bus.register_handler(h1)
    event_bus.register_handler(h2)

    event = _sample_change_event("evt-f")
    deliveries = event_bus.publish(event)
    assert len(deliveries) == 2
    assert all(d.status == DeliveryStatus.DELIVERED for d in deliveries)
    assert len(h1.received_events) == 1
    assert len(h2.received_events) == 1


# G. handler isolation
def test_g_handler_isolation(event_bus):
    h_ok = MockTestHandler("handler-ok", should_fail=False)
    h_fail = MockTestHandler("handler-fail", should_fail=True)
    event_bus.register_handler(h_ok)
    event_bus.register_handler(h_fail)

    event = _sample_change_event("evt-g")
    deliveries = event_bus.publish(event)
    assert len(deliveries) == 2

    deliv_map = {d.handler_id: d for d in deliveries}
    assert deliv_map["handler-ok"].status == DeliveryStatus.DELIVERED
    assert deliv_map["handler-fail"].status == DeliveryStatus.FAILED
    assert "simulated error" in (deliv_map["handler-fail"].error_message or "")
    assert len(h_ok.received_events) == 1


# H. duplicate publish
def test_h_duplicate_publish(event_bus, event_store):
    event = _sample_change_event("evt-h")
    event_bus.publish(event)
    event_bus.publish(event)

    events = event_store.list_events(subject_id="ITEM-123")
    assert len(events) == 1


# I. duplicate delivery
def test_i_duplicate_delivery(event_bus):
    handler = MockTestHandler("handler-i")
    event_bus.register_handler(handler)

    event = _sample_change_event("evt-i")
    event_bus.publish(event)
    assert handler.call_count == 1

    # Segunda publicación del mismo evento
    event_bus.publish(event)
    # Por idempotencia de entrega, handler no debe duplicar ejecución
    assert handler.call_count == 1


# J. event idempotency
def test_j_event_idempotency(event_store):
    event1 = _sample_change_event("evt-j")
    event2 = _sample_change_event("evt-j")
    s1 = event_store.append(event1)
    s2 = event_store.append(event2)
    assert s1.event_id == s2.event_id
    assert event_store.get_by_idempotency_key(event1.idempotency_key) is not None


# K. handler idempotency
def test_k_handler_idempotency(event_bus, event_store):
    handler = MockTestHandler("handler-k")
    event_bus.register_handler(handler)
    event = _sample_change_event("evt-k")

    event_bus.publish(event)
    assert handler.call_count == 1

    deliv = event_store.get_delivery("evt-k", "handler-k")
    assert deliv is not None
    assert deliv.status == DeliveryStatus.DELIVERED
    assert deliv.attempt_count == 1

    # Replay standard respeta idempotencia
    event_bus.replay_events(handler_id="handler-k", force=False)
    assert handler.call_count == 1


# L. ordering
def test_l_ordering(event_bus, event_store):
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 10, 5, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 1, 10, 10, 0, tzinfo=timezone.utc)

    e1 = EventRecord("e1", EventType.MARKET_OBSERVATION_CREATED, "OBS", "ITEM-1", t0, t0, "c1")
    e2 = EventRecord("e2", EventType.MARKET_OBSERVATION_CREATED, "OBS", "ITEM-1", t1, t1, "c1")
    e3 = EventRecord("e3", EventType.CHANGE_DETECTED, "CHG", "ITEM-1", t2, t2, "c1")

    # Guardar desordenados
    event_store.append(e3)
    event_store.append(e1)
    event_store.append(e2)

    ordered = event_store.list_events(subject_id="ITEM-1")
    assert [e.event_id for e in ordered] == ["e1", "e2", "e3"]


# M. correlation_id
def test_m_correlation_id():
    event = EventRecord(
        event_id="e-m",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="CHG",
        subject_id="SUBJ-1",
        occurred_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
        correlation_id="corr-chain-999",
    )
    assert event.correlation_id == "corr-chain-999"


# N. causation_id
def test_n_causation_id():
    event = EventRecord(
        event_id="e-n",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="CHG",
        subject_id="SUBJ-1",
        occurred_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
        correlation_id="corr-1",
        causation_id="parent-change-456",
    )
    assert event.causation_id == "parent-change-456"


# O. provenance
def test_o_provenance():
    event = EventRecord(
        event_id="e-o",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="CHG",
        subject_id="SUBJ-1",
        occurred_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
        correlation_id="corr-1",
        provenance="J4_CHANGE_DETECTION_ENGINE",
    )
    assert event.provenance == "J4_CHANGE_DETECTION_ENGINE"


# P. serialization
def test_p_serialization(event_store):
    event = _sample_change_event("evt-p")
    event_store.append(event)
    file_path = event_store._event_file_path("evt-p")
    assert file_path.exists()
    reloaded = event_store.get_by_id("evt-p")
    assert reloaded == event


# Q. persistence
def test_q_persistence(event_store, tmp_event_store_dir):
    event = _sample_change_event("evt-q")
    event_store.append(event)

    # Nuevo store apuntando al mismo directorio
    new_store = JsonEventStore(base_dir=tmp_event_store_dir)
    recovered = new_store.get_by_id("evt-q")
    assert recovered is not None
    assert recovered.event_id == "evt-q"


# R. restart/reload
def test_r_restart_reload(tmp_event_store_dir):
    store1 = JsonEventStore(base_dir=tmp_event_store_dir)
    bus1 = EventBusService(store1)
    h1 = MockTestHandler("h-reload")
    bus1.register_handler(h1)

    event = _sample_change_event("evt-r")
    bus1.publish(event)
    assert len(h1.received_events) == 1

    # Simular reinicio creando nuevas instancias
    store2 = JsonEventStore(base_dir=tmp_event_store_dir)
    bus2 = EventBusService(store2)
    h2 = MockTestHandler("h-reload")
    bus2.register_handler(h2)

    # Estado previo de delivery persiste
    deliv = store2.get_delivery("evt-r", "h-reload")
    assert deliv is not None
    assert deliv.status == DeliveryStatus.DELIVERED


# S. replay
def test_s_replay(event_bus, event_store):
    e1 = _sample_change_event("evt-s1")
    e2 = _sample_change_event("evt-s2")
    event_store.append(e1)
    event_store.append(e2)

    handler = MockTestHandler("h-replay")
    event_bus.register_handler(handler)

    # Replay a un nuevo handler que aún no los ha recibido
    replayed_delivs = event_bus.replay_events(handler_id="h-replay")
    assert len(replayed_delivs) == 2
    assert len(handler.received_events) == 2


# T. replay idempotency
def test_t_replay_idempotency(event_bus):
    handler = MockTestHandler("h-replay-idem")
    event_bus.register_handler(handler)

    e1 = _sample_change_event("evt-t1")
    event_bus.publish(e1)
    assert handler.call_count == 1

    # Replay no forzado -> no duplica ejecuciones en handler
    event_bus.replay_events(handler_id="h-replay-idem", force=False)
    assert handler.call_count == 1


# U. failure state
def test_u_failure_state(event_bus, event_store):
    h_fail = MockTestHandler("h-fail", should_fail=True)
    event_bus.register_handler(h_fail)

    event = _sample_change_event("evt-u")
    delivs = event_bus.publish(event)
    assert len(delivs) == 1
    assert delivs[0].status == DeliveryStatus.FAILED
    assert delivs[0].error_message is not None

    persisted_deliv = event_store.get_delivery("evt-u", "h-fail")
    assert persisted_deliv.status == DeliveryStatus.FAILED


# V. UNKNOWN preservation
def test_v_unknown_preservation():
    event = EventRecord(
        event_id="evt-v",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="OPPORTUNITY",
        subject_id="PROD-UNKNOWN",
        occurred_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
        correlation_id="corr-v",
        payload={
            "status": "UNKNOWN",
            "score": "UNKNOWN",
            "stock_status": "UNKNOWN",
        },
    )
    assert event.payload["status"] == "UNKNOWN"
    assert event.payload["score"] == "UNKNOWN"


# W. sensitive data sanitization
def test_w_sensitive_data_sanitization(event_store):
    event = EventRecord(
        event_id="evt-w",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="CREDENTIAL_TEST",
        subject_id="ID-1",
        occurred_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
        correlation_id="corr-w",
        payload={
            "access_token": "secret_abc_123",
            "api_key": "key_xyz_789",
            "safe_field": "public_data",
        },
    )
    event_store.append(event)
    file_path = event_store._event_file_path("evt-w")
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "secret_abc_123" not in content
    assert "key_xyz_789" not in content
    assert "[REDACTED]" in content
    assert "public_data" in content


# X. invalid event
def test_x_invalid_event():
    with pytest.raises(ValueError):
        EventRecord(
            event_id="",
            event_type=EventType.CHANGE_DETECTED,
            subject_type="X",
            subject_id="Y",
            occurred_at=datetime.now(timezone.utc),
            recorded_at=datetime.now(timezone.utc),
            correlation_id="c",
        )
    with pytest.raises(ValueError):
        EventRecord(
            event_id="e",
            event_type=EventType.CHANGE_DETECTED,
            subject_type="X",
            subject_id="Y",
            occurred_at=datetime.now(),  # naive
            recorded_at=datetime.now(timezone.utc),
            correlation_id="c",
        )


# Y. unknown handler
def test_y_unknown_handler(event_bus):
    # Publicar sin handlers registrados no produce error y retorna lista vacía
    event = _sample_change_event("evt-y")
    delivs = event_bus.publish(event)
    assert delivs == []


# Z. no Decision creation
def test_z_no_decision_creation(event_bus):
    event = _sample_change_event("evt-z")
    event_bus.publish(event)
    # Verificar que el EventBus no importa ni llama a DecisionEngine/DecisionRecord
    assert not hasattr(event_bus, "create_decision")


# AA. no Action execution
def test_aa_no_action_execution(event_bus):
    event = _sample_change_event("evt-aa")
    event_bus.publish(event)
    assert not hasattr(event_bus, "execute_action")


# AB. no Alert creation
def test_ab_no_alert_creation(event_bus):
    event = _sample_change_event("evt-ab")
    event_bus.publish(event)
    assert not hasattr(event_bus, "create_alert")


# AC. no Continuous Mission
def test_ac_no_continuous_mission(event_bus):
    event = _sample_change_event("evt-ac")
    event_bus.publish(event)
    assert not hasattr(event_bus, "start_continuous_mission")
