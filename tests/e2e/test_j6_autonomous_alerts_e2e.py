"""
End-to-End (E2E) Test Suite for J.6 Autonomous Alerts
Covers Scenarios A to J defined in Task J.6 specification.
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from src.domain.events.models import EventRecord, EventType
from src.domain.alerts.models import (
    AlertRecord,
    AlertType,
    AlertSeverity,
    AlertStatus,
    AlertDeliveryStatus,
)
from src.domain.alerts.ports import AlertDeliveryPort
from src.infrastructure.persistence.data.json.event_store import JsonEventStore
from src.infrastructure.persistence.data.json.alert_repository import JsonAlertRepository
from src.infrastructure.alerts.deterministic_delivery_adapter import InMemoryAlertDeliveryAdapter
from src.application.events.event_bus_service import EventBusService
from src.application.alerts.alert_service import AlertService
from src.application.alerts.event_handler import AutonomousAlertEventHandler
from src.domain.scheduling.models import DeterministicClock


@pytest.fixture
def e2e_env(tmp_path):
    d = tmp_path / "j6_e2e"
    d.mkdir(parents=True, exist_ok=True)
    clock = DeterministicClock(datetime(2026, 9, 1, 15, 0, 0, tzinfo=timezone.utc))
    event_store = JsonEventStore(d / "events")
    event_bus = EventBusService(event_store=event_store)
    alert_repo = JsonAlertRepository(d / "alerts")
    delivery_adapter = InMemoryAlertDeliveryAdapter(channel_name="DETERMINISTIC_OPERATIONAL_CHANNEL")
    alert_service = AlertService(
        alert_repository=alert_repo,
        delivery_port=delivery_adapter,
        clock=clock,
        cooldown_seconds=60.0,
    )
    handler = AutonomousAlertEventHandler(alert_service=alert_service)
    event_bus.register_handler(handler)

    return {
        "dir": d,
        "clock": clock,
        "event_store": event_store,
        "event_bus": event_bus,
        "alert_repo": alert_repo,
        "delivery_adapter": delivery_adapter,
        "alert_service": alert_service,
        "handler": handler,
    }


def test_scenario_a_eligible_change_alert(e2e_env):
    """Escenario A — Eligible Change Alert: ChangeDetectedEvent eligible -> alert created -> delivered."""
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    alert_repo = e2e_env["alert_repo"]
    adapter = e2e_env["delivery_adapter"]

    event = EventRecord(
        event_id="evt-e2e-a-001",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-TECH-001",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-a",
        causation_id="caus-e2e-a",
        provenance="MARKET_MONITOR",
        payload=MappingProxyType({
            "change_type": "PRICE_CHANGED",
            "significance": "SIGNIFICANT",
            "change_summary": "Competitor dropped price by 25%",
            "observed_changes_count": 1,
        }),
    )

    event_bus.publish(event)

    alerts = alert_repo.list_alerts(subject_id="PROD-TECH-001")
    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.SIGNIFICANT_CHANGE
    assert alerts[0].severity == AlertSeverity.HIGH
    assert alerts[0].delivery_status == AlertDeliveryStatus.DELIVERED
    assert len(adapter.delivered_alerts) == 1


def test_scenario_b_non_eligible_event(e2e_env):
    """Escenario B — Non-eligible Event: Event below rule/irrelevant -> no alert."""
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    alert_repo = e2e_env["alert_repo"]
    adapter = e2e_env["delivery_adapter"]

    # Minor change below threshold (significance NEGLIGIBLE)
    event = EventRecord(
        event_id="evt-e2e-b-001",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-TECH-MINOR",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-b",
        causation_id="caus-e2e-b",
        provenance="MARKET_MONITOR",
        payload=MappingProxyType({
            "change_type": "NO_CHANGE",
            "significance": "NEGLIGIBLE",
            "change_summary": "Minor punctuation change in title",
            "observed_changes_count": 0,
        }),
    )

    event_bus.publish(event)

    alerts = alert_repo.list_alerts(subject_id="PROD-TECH-MINOR")
    assert len(alerts) == 0
    assert len(adapter.delivered_alerts) == 0


def test_scenario_c_opportunity_alert(e2e_env):
    """Escenario C — Opportunity Alert: eligible OpportunityDetectedEvent -> structured alert."""
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    alert_repo = e2e_env["alert_repo"]
    adapter = e2e_env["delivery_adapter"]

    event = EventRecord(
        event_id="evt-e2e-c-001",
        event_type=EventType.OPPORTUNITY_DETECTED,
        subject_type="OPPORTUNITY",
        subject_id="OPP-ARBITRAGE-01",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-c",
        causation_id="caus-e2e-c",
        provenance="OPPORTUNITY_DETECTOR",
        payload_reference="evidence://opp-snapshot-01",
        payload=MappingProxyType({
            "status": "VALID",
            "confidence": "HIGH",
            "opportunity_type": "PRICE_ARBITRAGE",
            "estimated_spread": 0.35,
        }),
    )

    event_bus.publish(event)

    alerts = alert_repo.list_alerts(subject_id="OPP-ARBITRAGE-01")
    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.OPPORTUNITY_DETECTED
    assert alerts[0].severity == AlertSeverity.HIGH
    assert alerts[0].evidence_reference == "evidence://opp-snapshot-01"
    assert alerts[0].delivery_status == AlertDeliveryStatus.DELIVERED


def test_scenario_d_duplicate_replay(e2e_env):
    """Escenario D — Duplicate Replay: same event replayed -> one logical alert."""
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    alert_repo = e2e_env["alert_repo"]
    adapter = e2e_env["delivery_adapter"]

    event = EventRecord(
        event_id="evt-e2e-d-001",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-REPLAY",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-d",
        causation_id="caus-e2e-d",
        provenance="MARKET_MONITOR",
        payload=MappingProxyType({
            "significance": "HIGH",
            "change_summary": "Stock level dropped to critical",
        }),
    )

    # First publish
    event_bus.publish(event)
    assert len(alert_repo.list_alerts(subject_id="PROD-REPLAY")) == 1
    assert len(adapter.delivered_alerts) == 1

    # Replay identical event
    event_bus.publish(event)
    assert len(alert_repo.list_alerts(subject_id="PROD-REPLAY")) == 1
    assert len(adapter.delivered_alerts) == 1


def test_scenario_e_restart_persistence(e2e_env):
    """Escenario E — Restart: alert persisted -> service recreated -> state retained."""
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    d = e2e_env["dir"]

    event = EventRecord(
        event_id="evt-e2e-e-001",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-RESTART",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-e",
        causation_id="caus-e2e-e",
        provenance="MARKET_MONITOR",
        payload=MappingProxyType({
            "significance": "CRITICAL",
            "change_summary": "Buybox lost to unauthorized seller",
        }),
    )

    event_bus.publish(event)

    # Simulate restart by instantiating new repository and service from same dir
    restarted_repo = JsonAlertRepository(d / "alerts")
    restarted_adapter = InMemoryAlertDeliveryAdapter(channel_name="RESTARTED_CHANNEL")
    restarted_service = AlertService(
        alert_repository=restarted_repo,
        delivery_port=restarted_adapter,
        clock=clock,
    )

    alerts = restarted_repo.list_alerts(subject_id="PROD-RESTART")
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.delivery_status == AlertDeliveryStatus.DELIVERED
    assert "Buybox lost" in alert.message


def test_scenario_f_delivery_failure_isolation(e2e_env):
    """Escenario F — Delivery Failure: delivery adapter fails -> alert remains -> delivery FAILED -> bus stays operational."""
    class FailingDeliveryAdapter(AlertDeliveryPort):
        @property
        def channel_name(self) -> str:
            return "FAILING_CHANNEL"

        def deliver(self, alert: AlertRecord):
            raise ConnectionError("Simulated network failure on remote webhook")

    clock = e2e_env["clock"]
    d = e2e_env["dir"]
    alert_repo = JsonAlertRepository(d / "alerts_failing")
    failing_adapter = FailingDeliveryAdapter()
    service = AlertService(
        alert_repository=alert_repo,
        delivery_port=failing_adapter,
        clock=clock,
    )
    handler = AutonomousAlertEventHandler(alert_service=service)
    event_store = JsonEventStore(d / "events_failing")
    event_bus = EventBusService(event_store=event_store)
    event_bus.register_handler(handler)

    event = EventRecord(
        event_id="evt-e2e-f-001",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-FAILING-DELIVERY",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-f",
        causation_id="caus-e2e-f",
        provenance="MARKET_MONITOR",
        payload=MappingProxyType({
            "significance": "HIGH",
            "change_summary": "Price spike",
        }),
    )

    # Publishing must succeed on event bus level despite delivery failure
    deliveries = event_bus.publish(event)
    assert len(deliveries) == 1

    alerts = alert_repo.list_alerts(subject_id="PROD-FAILING-DELIVERY")
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.delivery_status == AlertDeliveryStatus.FAILED

    results = alert_repo.list_delivery_results_by_alert(alert.alert_id)
    assert len(results) == 1
    assert results[0].status == AlertDeliveryStatus.FAILED
    assert "Simulated network failure" in results[0].error_message


def test_scenario_g_unknown_safety(e2e_env):
    """Escenario G — UNKNOWN: uncertain market data -> no fabricated high-confidence commercial alert."""
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    alert_repo = e2e_env["alert_repo"]

    event = EventRecord(
        event_id="evt-e2e-g-001",
        event_type=EventType.OPPORTUNITY_DETECTED,
        subject_type="OPPORTUNITY",
        subject_id="OPP-UNKNOWN-STATE",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-g",
        causation_id="caus-e2e-g",
        provenance="OPPORTUNITY_DETECTOR",
        payload=MappingProxyType({
            "status": "UNKNOWN",
            "confidence": 0.10,
            "opportunity_type": "UNKNOWN",
        }),
    )

    event_bus.publish(event)

    alerts = alert_repo.list_alerts(subject_id="OPP-UNKNOWN-STATE")
    assert len(alerts) == 1
    alert = alerts[0]
    # Must preserve uncertainty and not elevate to CRITICAL/HIGH
    assert alert.severity == AlertSeverity.INFO
    assert "estado UNKNOWN" in alert.message


def test_scenario_h_security_sanitization(e2e_env):
    """Escenario H — Security: secret-bearing metadata -> redacted persistence/delivery."""
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    alert_repo = e2e_env["alert_repo"]
    d = e2e_env["dir"]

    event = EventRecord(
        event_id="evt-e2e-h-001",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-SECRET-TEST",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-h",
        causation_id="caus-e2e-h",
        provenance="MARKET_MONITOR",
        payload=MappingProxyType({
            "change_type": "PRICE_CHANGED",
            "significance": "CRITICAL",
            "change_summary": "Seller changed credentials",
            "api_key": "secret-api-key-12345",
            "authorization": "Bearer secret-oauth-jwt",
        }),
        metadata=MappingProxyType({
            "api_key": "secret-api-key-12345",
            "authorization": "Bearer secret-oauth-jwt",
        }),
    )

    event_bus.publish(event)

    alerts = alert_repo.list_alerts(subject_id="PROD-SECRET-TEST")
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.metadata["api_key"] == "[REDACTED]"
    assert alert.metadata["authorization"] == "[REDACTED]"

    alert_file = d / "alerts" / "alerts" / f"{alert.alert_id}.json"
    content = alert_file.read_text(encoding="utf-8")
    assert "secret-api-key-12345" not in content
    assert "secret-oauth-jwt" not in content


def test_scenario_i_causal_chain(e2e_env):
    """Escenario I — Causal Chain: alert traces event -> change/opportunity -> observation/source."""
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    alert_repo = e2e_env["alert_repo"]

    event = EventRecord(
        event_id="evt-e2e-i-999",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-CHAIN-TEST",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="mission-chain-corr-001",
        causation_id="chg-chain-caus-002",
        provenance="CHAIN_OBSERVER",
        payload_reference="obs://snapshot-store/snap-003.json",
        payload=MappingProxyType({
            "significance": "CRITICAL",
            "change_summary": "Price collapsed below hard stop threshold",
        }),
    )

    event_bus.publish(event)

    alerts = alert_repo.list_alerts(subject_id="PROD-CHAIN-TEST")
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.event_id == "evt-e2e-i-999"
    assert alert.correlation_id == "mission-chain-corr-001"
    assert alert.causation_id == "chg-chain-caus-002"
    assert alert.provenance == "CHAIN_OBSERVER"
    assert alert.evidence_reference == "obs://snapshot-store/snap-003.json"


def test_scenario_j_scope_boundary(e2e_env):
    """
    Escenario J — Scope Boundary:
    J.6 does NOT:
    - create DecisionRecord
    - execute Action
    - modify Policy
    - create Continuous Mission
    - call external Marketplace
    """
    clock = e2e_env["clock"]
    event_bus = e2e_env["event_bus"]
    alert_repo = e2e_env["alert_repo"]

    event = EventRecord(
        event_id="evt-e2e-j-001",
        event_type=EventType.OPPORTUNITY_DETECTED,
        subject_type="OPPORTUNITY",
        subject_id="OPP-SCOPE-TEST",
        occurred_at=clock.now(),
        recorded_at=clock.now(),
        correlation_id="corr-e2e-j",
        causation_id="caus-e2e-j",
        provenance="OPPORTUNITY_DETECTOR",
        payload=MappingProxyType({
            "status": "ACTIONABLE",
            "confidence": 0.99,
            "opportunity_type": "HIGH_PROFIT_SPREAD",
        }),
    )

    event_bus.publish(event)

    alerts = alert_repo.list_alerts(subject_id="OPP-SCOPE-TEST")
    assert len(alerts) == 1
    alert = alerts[0]

    # Verify that the created object is an AlertRecord and nothing more
    assert isinstance(alert, AlertRecord)
    assert not hasattr(alert, "action_type")
    assert not hasattr(alert, "policy_version")
    assert not hasattr(alert, "mission_id")
    assert not hasattr(alert, "order_id")
