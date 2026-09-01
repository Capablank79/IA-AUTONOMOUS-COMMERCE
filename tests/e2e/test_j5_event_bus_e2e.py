"""
Tests End-to-End (E2E) para el Event Bus y Procesamiento de Eventos (Hito J.5).

Cubre los 9 escenarios obligatorios (A - I):
- Escenario A — Publish/Deliver: ChangeDetected -> event persisted -> handler invoked.
- Escenario B — Multiple Consumers: same event -> handler A, handler B -> both independently tracked.
- Escenario C — Duplicate Event: same event published twice -> one logical processing per handler.
- Escenario D — Restart: event persisted -> process recreated -> pending/replayable state recovered.
- Escenario E — Replay: historical event replayed -> no duplicated logical side effect.
- Escenario F — Handler Failure: handler raises -> failure recorded -> bus remains operational.
- Escenario G — Correlation/Causation: event preserves complete causal chain.
- Escenario H — Security: secret in metadata -> redacted persistence.
- Escenario I — Scope Boundary: EventBus does NOT send alert, create mission, create decision, execute action.
"""

import shutil
import pytest
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import List

from src.domain.events.models import (
    EventRecord,
    EventType,
    DeliveryRecord,
    DeliveryStatus,
)
from src.domain.events.ports import EventHandlerPort
from src.infrastructure.persistence.data.json.event_store import JsonEventStore
from src.application.events.event_bus_service import EventBusService
from src.application.events.integration_service import EventIntegrationService
from src.domain.change_detection.models import (
    ChangeRecord,
    ChangeSubjectType,
    ChangeType,
    ChangeSignificance,
    ObservedChangeField,
    DerivedChangeDelta,
)
from src.domain.market_intelligence.models import Confidence, Marketplace
from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    ObservationSourceType,
    NormalizedPrice,
)


class CountingHandler(EventHandlerPort):
    def __init__(self, handler_id: str):
        self._handler_id = handler_id
        self.processed_events: List[EventRecord] = []
        self.call_count: int = 0

    @property
    def handler_id(self) -> str:
        return self._handler_id

    def can_handle(self, event_type: EventType) -> bool:
        return True

    def handle(self, event: EventRecord) -> None:
        self.call_count += 1
        self.processed_events.append(event)


class BuggyHandler(EventHandlerPort):
    def __init__(self, handler_id: str):
        self._handler_id = handler_id

    @property
    def handler_id(self) -> str:
        return self._handler_id

    def can_handle(self, event_type: EventType) -> bool:
        return True

    def handle(self, event: EventRecord) -> None:
        raise RuntimeError("Buggy execution crash")


@pytest.fixture
def e2e_store_dir(tmp_path):
    d = tmp_path / "j5_e2e_store"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _build_test_change_record(change_id: str = "chg-e2e-1", corr_id: str = "corr-e2e-1") -> ChangeRecord:
    t0 = datetime(2026, 9, 1, 14, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 14, 10, 0, tzinfo=timezone.utc)
    return ChangeRecord(
        change_id=change_id,
        subject_type=ChangeSubjectType.MARKET_OBSERVATION,
        subject_id="PROD-E2E-100",
        previous_reference="obs-prev",
        current_reference="obs-curr",
        change_type=ChangeType.PRICE_CHANGED,
        detected_at=t1,
        observed_from=t0,
        observed_to=t1,
        changed_fields=("price",),
        observed_changes=(
            ObservedChangeField(field_name="price", previous_value=Decimal("20000"), current_value=Decimal("18000")),
        ),
        derived_deltas=(
            DerivedChangeDelta(field_name="price", numeric_delta=Decimal("-2000"), percentage_delta=Decimal("-10.0")),
        ),
        significance=ChangeSignificance.MODERATE,
        confidence=Confidence.HIGH,
        correlation_id=corr_id,
    )


# Escenario A — Publish/Deliver
def test_e2e_scenario_a_publish_deliver(e2e_store_dir):
    store = JsonEventStore(e2e_store_dir)
    bus = EventBusService(store)
    integ = EventIntegrationService(bus)

    handler = CountingHandler("h-e2e-a")
    bus.register_handler(handler)

    chg = _build_test_change_record("chg-a", "corr-a")
    delivs = integ.publish_change(chg)

    assert len(delivs) == 1
    assert delivs[0].status == DeliveryStatus.DELIVERED
    assert len(handler.processed_events) == 1
    assert handler.processed_events[0].event_type == EventType.CHANGE_DETECTED
    assert store.get_by_id(delivs[0].event_id) is not None


# Escenario B — Multiple Consumers
def test_e2e_scenario_b_multiple_consumers(e2e_store_dir):
    store = JsonEventStore(e2e_store_dir)
    bus = EventBusService(store)
    integ = EventIntegrationService(bus)

    h_a = CountingHandler("h-b-1")
    h_b = CountingHandler("h-b-2")
    bus.register_handler(h_a)
    bus.register_handler(h_b)

    chg = _build_test_change_record("chg-b", "corr-b")
    delivs = integ.publish_change(chg)

    assert len(delivs) == 2
    assert h_a.call_count == 1
    assert h_b.call_count == 1

    d_a = store.get_delivery(delivs[0].event_id, "h-b-1")
    d_b = store.get_delivery(delivs[0].event_id, "h-b-2")
    assert d_a.status == DeliveryStatus.DELIVERED
    assert d_b.status == DeliveryStatus.DELIVERED


# Escenario C — Duplicate Event
def test_e2e_scenario_c_duplicate_event(e2e_store_dir):
    store = JsonEventStore(e2e_store_dir)
    bus = EventBusService(store)
    integ = EventIntegrationService(bus)

    handler = CountingHandler("h-c")
    bus.register_handler(handler)

    chg = _build_test_change_record("chg-c", "corr-c")
    # Publicar primera vez
    integ.publish_change(chg)
    assert handler.call_count == 1

    # Publicar segunda vez el mismo cambio
    integ.publish_change(chg)
    # Por idempotencia, handler no se ejecuta dos veces
    assert handler.call_count == 1


# Escenario D — Restart
def test_e2e_scenario_d_restart(e2e_store_dir):
    store1 = JsonEventStore(e2e_store_dir)
    bus1 = EventBusService(store1)
    integ1 = EventIntegrationService(bus1)

    h1 = CountingHandler("h-d")
    bus1.register_handler(h1)

    chg = _build_test_change_record("chg-d", "corr-d")
    integ1.publish_change(chg)

    # Reinicio completo de proceso
    store2 = JsonEventStore(e2e_store_dir)
    bus2 = EventBusService(store2)
    h2 = CountingHandler("h-d")
    bus2.register_handler(h2)

    events = store2.list_events(correlation_id="corr-d")
    assert len(events) == 1
    deliv = store2.get_delivery(events[0].event_id, "h-d")
    assert deliv.status == DeliveryStatus.DELIVERED


# Escenario E — Replay
def test_e2e_scenario_e_replay(e2e_store_dir):
    store = JsonEventStore(e2e_store_dir)
    bus = EventBusService(store)
    integ = EventIntegrationService(bus)

    h_original = CountingHandler("h-orig")
    bus.register_handler(h_original)

    chg = _build_test_change_record("chg-e", "corr-e")
    integ.publish_change(chg)
    assert h_original.call_count == 1

    # Replay standard sin forzar
    bus.replay_events(handler_id="h-orig", force=False)
    assert h_original.call_count == 1  # no duplicado

    # Nuevo suscriptor se une después
    h_new = CountingHandler("h-new")
    bus.register_handler(h_new)
    bus.replay_events(handler_id="h-new", force=False)
    assert h_new.call_count == 1  # ejecutado exactamente una vez


# Escenario F — Handler Failure
def test_e2e_scenario_f_handler_failure(e2e_store_dir):
    store = JsonEventStore(e2e_store_dir)
    bus = EventBusService(store)
    integ = EventIntegrationService(bus)

    h_good = CountingHandler("h-good")
    h_bad = BuggyHandler("h-bad")
    bus.register_handler(h_good)
    bus.register_handler(h_bad)

    chg = _build_test_change_record("chg-f", "corr-f")
    delivs = integ.publish_change(chg)

    assert len(delivs) == 2
    deliv_map = {d.handler_id: d for d in delivs}
    assert deliv_map["h-good"].status == DeliveryStatus.DELIVERED
    assert deliv_map["h-bad"].status == DeliveryStatus.FAILED
    assert "Buggy execution" in (deliv_map["h-bad"].error_message or "")
    assert h_good.call_count == 1


# Escenario G — Correlation/Causation
def test_e2e_scenario_g_correlation_causation(e2e_store_dir):
    store = JsonEventStore(e2e_store_dir)
    bus = EventBusService(store)
    integ = EventIntegrationService(bus)

    h = CountingHandler("h-g")
    bus.register_handler(h)

    chg = _build_test_change_record("chg-causal-99", "corr-chain-alpha")
    integ.publish_change(chg)

    received = h.processed_events[0]
    assert received.correlation_id == "corr-chain-alpha"
    assert received.causation_id == "chg-causal-99"
    assert received.payload_reference == "chg-causal-99"


# Escenario H — Security
def test_e2e_scenario_h_security(e2e_store_dir):
    store = JsonEventStore(e2e_store_dir)
    bus = EventBusService(store)

    event = EventRecord(
        event_id="evt-e2e-sec",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="TEST",
        subject_id="T1",
        occurred_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
        correlation_id="corr-sec",
        metadata={
            "api_key": "live_secret_key_12345",
            "password": "secretPassword!",
            "public_key": "pub-key-ok",
        },
    )
    bus.publish(event)

    saved_file = store._event_file_path("evt-e2e-sec")
    with open(saved_file, "r", encoding="utf-8") as f:
        data = f.read()

    assert "live_secret_key_12345" not in data
    assert "secretPassword!" not in data
    assert "[REDACTED]" in data
    assert "pub-key-ok" in data


# Escenario I — Scope Boundary
def test_e2e_scenario_i_scope_boundary(e2e_store_dir):
    store = JsonEventStore(e2e_store_dir)
    bus = EventBusService(store)
    integ = EventIntegrationService(bus)

    # Verificar que el Event Bus y sus adaptadores NO tienen métodos ni referencias a J.6/J.7/Policy/Decision/Marketplace execution
    assert not hasattr(bus, "generate_alert")
    assert not hasattr(bus, "start_continuous_mission")
    assert not hasattr(bus, "execute_trade")
    assert not hasattr(bus, "modify_policy")
    assert not hasattr(integ, "generate_alert")
    assert not hasattr(integ, "start_continuous_mission")
