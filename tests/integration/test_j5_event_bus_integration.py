"""
Tests de integración para el Event Bus (Hito J.5).

Verifica la cadena de integración completa:
J.4 CHANGE -> ChangeDetectedEvent -> PERSIST EVENT -> EVENT BUS -> HANDLER -> DELIVERY RESULT -> RELOAD -> REPLAY SAFE

Comprueba:
- correlation
- causation
- idempotency
- restart
- ordering
- failure isolation
- security
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
from src.infrastructure.persistence.data.json.change_repository import JsonChangeRecordRepository
from src.infrastructure.persistence.data.json.market_observation_repository import JsonMarketObservationRepository
from src.application.events.event_bus_service import EventBusService
from src.application.events.integration_service import EventIntegrationService
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
from src.domain.market_intelligence.models import Confidence, Marketplace
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
from src.application.change_detection.service import ChangeDetectionService


class AuditLoggerHandler(EventHandlerPort):
    """Handler desacoplado para auditar eventos en memoria durante tests."""
    def __init__(self, handler_id: str = "audit-logger"):
        self._handler_id = handler_id
        self.processed_events: List[EventRecord] = []

    @property
    def handler_id(self) -> str:
        return self._handler_id

    def can_handle(self, event_type: EventType) -> bool:
        return True

    def handle(self, event: EventRecord) -> None:
        self.processed_events.append(event)


class FailingTestHandler(EventHandlerPort):
    def __init__(self, handler_id: str = "failing-handler"):
        self._handler_id = handler_id

    @property
    def handler_id(self) -> str:
        return self._handler_id

    def can_handle(self, event_type: EventType) -> bool:
        return True

    def handle(self, event: EventRecord) -> None:
        raise ValueError("Handler deliberate failure")


@pytest.fixture
def tmp_integration_dir(tmp_path):
    d = tmp_path / "j5_integ_test"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def test_j4_change_to_event_bus_flow(tmp_integration_dir):
    """
    Verifica el flujo completo desde ChangeDetection -> Event -> Bus -> Handler -> Store.
    """
    event_store_dir = tmp_integration_dir / "event_store"
    event_store = JsonEventStore(base_dir=event_store_dir)
    event_bus = EventBusService(event_store=event_store)
    integ_service = EventIntegrationService(event_bus=event_bus)

    audit_handler = AuditLoggerHandler("audit-handler-1")
    event_bus.register_handler(audit_handler)

    # 1. Crear un ChangeRecord de J.4
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 1, 12, 10, 0, tzinfo=timezone.utc)

    change = ChangeRecord(
        change_id="chg-integ-001",
        subject_type=ChangeSubjectType.MARKET_OBSERVATION,
        subject_id="ITEM-MLA-999",
        previous_reference="obs-001",
        current_reference="obs-002",
        change_type=ChangeType.PRICE_CHANGED,
        detected_at=t1,
        observed_from=t0,
        observed_to=t1,
        changed_fields=("price",),
        observed_changes=(
            ObservedChangeField(field_name="price", previous_value=Decimal("10000"), current_value=Decimal("8500")),
        ),
        derived_deltas=(
            DerivedChangeDelta(field_name="price", numeric_delta=Decimal("-1500"), percentage_delta=Decimal("-15.0")),
        ),
        significance=ChangeSignificance.SIGNIFICANT,
        confidence=Confidence.HIGH,
        correlation_id="corr-integ-chain-1",
    )

    # 2. Publicar a través del EventIntegrationService
    deliveries = integ_service.publish_change(change)

    assert len(deliveries) == 1
    assert deliveries[0].status == DeliveryStatus.DELIVERED
    assert deliveries[0].event_id == "evt-chg-chg-integ-001"

    # 3. Validar entrega y recepción
    assert len(audit_handler.processed_events) == 1
    received = audit_handler.processed_events[0]
    assert received.event_type == EventType.CHANGE_DETECTED
    assert received.causation_id == "chg-integ-001"
    assert received.correlation_id == "corr-integ-chain-1"
    assert received.payload["change_type"] == "PRICE_CHANGED"

    # 4. Validar persistencia durable
    persisted_event = event_store.get_by_id("evt-chg-chg-integ-001")
    assert persisted_event is not None
    assert persisted_event.subject_id == "ITEM-MLA-999"

    # 5. Validar reinicio (nueva instancia del EventStore y EventBus)
    reloaded_store = JsonEventStore(base_dir=event_store_dir)
    reloaded_bus = EventBusService(event_store=reloaded_store)
    reloaded_handler = AuditLoggerHandler("audit-handler-1")
    reloaded_bus.register_handler(reloaded_handler)

    # 6. Replay seguro e idempotente
    replayed_delivs = reloaded_bus.replay_events(handler_id="audit-handler-1", force=False)
    # Ya estaba DELIVERED -> no re-ejecuta
    assert len(replayed_delivs) == 1
    assert len(reloaded_handler.processed_events) == 0


def test_j5_failure_isolation_and_multi_consumer(tmp_integration_dir):
    """
    Verifica entrega a múltiples consumidores independientes y aislamiento de fallos.
    """
    event_store_dir = tmp_integration_dir / "event_store_fail"
    event_store = JsonEventStore(base_dir=event_store_dir)
    event_bus = EventBusService(event_store=event_store)
    integ_service = EventIntegrationService(event_bus=event_bus)

    h_success = AuditLoggerHandler("handler-success")
    h_fail = FailingTestHandler("handler-fail")

    event_bus.register_handler(h_success)
    event_bus.register_handler(h_fail)

    obs = MarketObservation(
        observation_id="obs-fail-test",
        source="MERCADO_LIBRE",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-FAIL-1",
        status=ObservationStatus.SUCCESS,
        price=NormalizedPrice(amount=Decimal("5000"), currency="CLP"),
        correlation_id="corr-obs-fail",
    )

    delivs = integ_service.publish_observation(obs)
    assert len(delivs) == 2

    status_by_handler = {d.handler_id: d.status for d in delivs}
    assert status_by_handler["handler-success"] == DeliveryStatus.DELIVERED
    assert status_by_handler["handler-fail"] == DeliveryStatus.FAILED

    # El bus permanece operativo
    assert len(h_success.processed_events) == 1


def test_j5_security_sanitization_integration(tmp_integration_dir):
    """
    Verifica que la integración de eventos sanitiza recursivamente cualquier credencial/secreto.
    """
    event_store_dir = tmp_integration_dir / "event_store_sec"
    event_store = JsonEventStore(base_dir=event_store_dir)
    event_bus = EventBusService(event_store=event_store)

    event = EventRecord(
        event_id="evt-sec-test",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="SECURITY_TEST",
        subject_id="SEC-01",
        occurred_at=datetime.now(timezone.utc),
        recorded_at=datetime.now(timezone.utc),
        correlation_id="corr-sec",
        payload={
            "authorization": "Bearer token123456",
            "refresh_token": "rt-7890",
            "safe_metadata": "valid_value",
        },
    )

    event_bus.publish(event)

    saved_file = event_store._event_file_path("evt-sec-test")
    with open(saved_file, "r", encoding="utf-8") as f:
        file_text = f.read()

    assert "token123456" not in file_text
    assert "rt-7890" not in file_text
    assert "[REDACTED]" in file_text
    assert "valid_value" in file_text
