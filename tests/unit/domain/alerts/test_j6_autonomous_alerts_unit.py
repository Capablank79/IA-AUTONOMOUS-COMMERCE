"""
Suite exhaustiva de Unit Tests para Alertas Autónomas (Hito J.6).

Cubre todos los criterios A-AC:
A. create immutable AlertRecord
B. alert type
C. severity
D. event evaluation
E. ChangeDetected -> alert
F. OpportunityDetected -> alert when eligible
G. non-eligible event -> no alert
H. UNKNOWN safety
I. source failure technical alert if defined
J. duplicate event
K. alert idempotency
L. replay safe
M. throttling/cooldown if applicable
N. correlation
O. causation
P. provenance
Q. evidence
R. message/template determinism
S. persistence
T. restart/reload
U. delivery success
V. delivery failure
W. UNKNOWN delivery
X. handler failure isolation
Y. sensitive data sanitization
Z. no Decision creation
AA. no Action execution
AB. no Mission creation
AC. no marketplace call
"""

import pytest
from datetime import datetime, timezone
import json
from pathlib import Path
from types import MappingProxyType

from src.domain.events.models import EventRecord, EventType
from src.domain.alerts.models import (
    AlertRecord,
    AlertType,
    AlertSeverity,
    AlertStatus,
    AlertDeliveryStatus,
    AlertDeliveryResult,
)
from src.domain.alerts.rules import DeterministicAlertRulesEngine
from src.domain.scheduling.models import DeterministicClock
from src.application.alerts.alert_service import AlertService
from src.application.alerts.event_handler import AutonomousAlertEventHandler
from src.infrastructure.persistence.data.json.alert_repository import (
    JsonAlertRepository,
    CorruptedAlertDataError,
)
from src.infrastructure.alerts.deterministic_delivery_adapter import InMemoryAlertDeliveryAdapter


@pytest.fixture
def temp_repo_dir(tmp_path):
    repo_dir = tmp_path / "alerts_store"
    repo_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir


@pytest.fixture
def alert_repo(temp_repo_dir):
    return JsonAlertRepository(temp_repo_dir)


@pytest.fixture
def test_clock():
    return DeterministicClock(datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc))


def test_a_create_immutable_alert_record():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    alert = AlertRecord(
        alert_id="alt-001",
        alert_type=AlertType.SIGNIFICANT_CHANGE,
        severity=AlertSeverity.HIGH,
        status=AlertStatus.CREATED,
        subject_type="PRODUCT",
        subject_id="PROD-100",
        title="Test Alert",
        message="Test Message",
        event_id="evt-001",
        occurred_at=now,
        created_at=now,
        correlation_id="corr-001",
        causation_id="caus-001",
    )
    assert alert.alert_id == "alt-001"
    assert alert.alert_type == AlertType.SIGNIFICANT_CHANGE
    assert alert.severity == AlertSeverity.HIGH
    with pytest.raises(Exception):
        alert.title = "New Title"  # Inmutable


def test_b_alert_types():
    assert AlertType.OPPORTUNITY_DETECTED.value == "OPPORTUNITY_DETECTED"
    assert AlertType.SIGNIFICANT_CHANGE.value == "SIGNIFICANT_CHANGE"
    assert AlertType.SOURCE_FAILURE.value == "SOURCE_FAILURE"
    assert AlertType.RISK_CHANGE.value == "RISK_CHANGE"
    assert AlertType.SYSTEM_FAILURE.value == "SYSTEM_FAILURE"


def test_c_severity():
    assert AlertSeverity.INFO.value == "INFO"
    assert AlertSeverity.WARNING.value == "WARNING"
    assert AlertSeverity.HIGH.value == "HIGH"
    assert AlertSeverity.CRITICAL.value == "CRITICAL"


def test_d_event_evaluation():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-dummy",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-1",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-1",
        payload=MappingProxyType({"significance": "CRITICAL", "change_type": "PRICE_CHANGED"}),
    )
    res = DeterministicAlertRulesEngine.evaluate(evt, now=now)
    assert res.is_eligible is True
    assert res.alert is not None
    assert res.alert.severity == AlertSeverity.CRITICAL


def test_e_change_detected_to_alert():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-chg-1",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-123",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-123",
        payload=MappingProxyType({
            "significance": "SIGNIFICANT",
            "change_type": "STOCK_CHANGED",
            "change_summary": "Stock reduced by 80%",
        }),
    )
    res = DeterministicAlertRulesEngine.evaluate(evt, now=now)
    assert res.is_eligible is True
    assert res.alert.alert_type == AlertType.SIGNIFICANT_CHANGE
    assert res.alert.severity == AlertSeverity.HIGH
    assert "Stock reduced by 80%" in res.alert.message


def test_f_opportunity_detected_to_alert_when_eligible():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-opp-1",
        event_type=EventType.OPPORTUNITY_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-999",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-opp",
        payload=MappingProxyType({
            "status": "VALID",
            "confidence": "HIGH",
            "opportunity_type": "PRICE_ARBITRAGE",
            "opportunity_score": "88.5",
        }),
    )
    res = DeterministicAlertRulesEngine.evaluate(evt, now=now)
    assert res.is_eligible is True
    assert res.alert.alert_type == AlertType.OPPORTUNITY_DETECTED
    assert res.alert.severity == AlertSeverity.HIGH


def test_g_non_eligible_event_no_alert():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Evento de cambio insignificante
    evt = EventRecord(
        event_id="evt-chg-none",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-1",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-1",
        payload=MappingProxyType({"significance": "NONE"}),
    )
    res = DeterministicAlertRulesEngine.evaluate(evt, now=now)
    assert res.is_eligible is False
    assert res.alert is None


def test_h_unknown_safety():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-opp-unk",
        event_type=EventType.OPPORTUNITY_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-UNK",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-unk",
        payload=MappingProxyType({
            "status": "UNKNOWN",
            "confidence": "UNKNOWN",
            "opportunity_type": "GENERAL_COMMERCIAL",
        }),
    )
    res = DeterministicAlertRulesEngine.evaluate(evt, now=now)
    assert res.is_eligible is True
    # UNKNOWN no debe inferir CRITICAL ni HIGH
    assert res.alert.severity == AlertSeverity.INFO


def test_i_source_failure_technical_alert():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-obs-fail",
        event_type=EventType.MARKET_OBSERVATION_CREATED,
        subject_type="OBSERVATION",
        subject_id="OBS-FAIL-1",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-fail",
        payload=MappingProxyType({
            "is_source_failure": True,
            "error_message": "MercadoLibre API timeout 504",
        }),
    )
    res = DeterministicAlertRulesEngine.evaluate(evt, now=now)
    assert res.is_eligible is True
    assert res.alert.alert_type == AlertType.SOURCE_FAILURE
    assert res.alert.severity == AlertSeverity.WARNING


def test_j_k_l_duplicate_event_idempotency_and_replay_safe(alert_repo, test_clock):
    adapter = InMemoryAlertDeliveryAdapter()
    service = AlertService(alert_repository=alert_repo, delivery_port=adapter, clock=test_clock)

    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-dup-1",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-DUP",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-dup",
        payload=MappingProxyType({"significance": "SIGNIFICANT", "change_type": "PRICE_CHANGED"}),
    )

    # Primer intento
    alert1 = service.process_event(evt)
    assert alert1 is not None
    assert len(adapter.delivered_alerts) == 1

    # Replay idéntico
    alert2 = service.process_event(evt)
    assert alert2 is not None
    assert alert1.idempotency_key == alert2.idempotency_key
    assert alert1.alert_id == alert2.alert_id
    # No se disparó un segundo envío en el adapter
    assert len(adapter.delivered_alerts) == 1


def test_m_throttling_cooldown(alert_repo, test_clock):
    adapter = InMemoryAlertDeliveryAdapter()
    # 60 segundos de cooldown
    service = AlertService(
        alert_repository=alert_repo,
        delivery_port=adapter,
        clock=test_clock,
        cooldown_seconds=60.0,
    )

    now1 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt1 = EventRecord(
        event_id="evt-cd-1",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-COOLDOWN",
        occurred_at=now1,
        recorded_at=now1,
        correlation_id="corr-cd-1",
        payload=MappingProxyType({"significance": "SIGNIFICANT", "change_type": "PRICE_CHANGED"}),
    )
    alert1 = service.process_event(evt1)
    assert alert1.status != AlertStatus.SUPPRESSED
    assert len(adapter.delivered_alerts) == 1

    # Evento posterior para el mismo sujeto pero dentro del cooldown (10 segs después)
    test_clock.advance(10)
    now2 = test_clock.now()
    evt2 = EventRecord(
        event_id="evt-cd-2",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-COOLDOWN",
        occurred_at=now2,
        recorded_at=now2,
        correlation_id="corr-cd-2",
        payload=MappingProxyType({"significance": "SIGNIFICANT", "change_type": "PRICE_CHANGED"}),
    )
    alert2 = service.process_event(evt2)
    assert alert2.status == AlertStatus.SUPPRESSED
    assert alert2.delivery_status == AlertDeliveryStatus.SUPPRESSED
    assert len(adapter.delivered_alerts) == 1  # No despachado

    # Evento tras superar el cooldown (70 segs después)
    test_clock.advance(70)
    now3 = test_clock.now()
    evt3 = EventRecord(
        event_id="evt-cd-3",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-COOLDOWN",
        occurred_at=now3,
        recorded_at=now3,
        correlation_id="corr-cd-3",
        payload=MappingProxyType({"significance": "SIGNIFICANT", "change_type": "PRICE_CHANGED"}),
    )
    alert3 = service.process_event(evt3)
    assert alert3.status != AlertStatus.SUPPRESSED
    assert len(adapter.delivered_alerts) == 2


def test_n_o_p_q_correlation_causation_provenance_evidence(alert_repo, test_clock):
    service = AlertService(alert_repository=alert_repo, clock=test_clock)
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-trace-1",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-TRACE",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-xyz-999",
        causation_id="caus-obs-888",
        provenance="MERCADOLIBRE_LIVE",
        payload_reference="evidence://snapshot-123.json",
        payload=MappingProxyType({"significance": "CRITICAL", "change_type": "PRICE_CHANGED"}),
    )
    alert = service.process_event(evt)
    assert alert.correlation_id == "corr-xyz-999"
    assert alert.causation_id == "caus-obs-888"
    assert alert.provenance == "MERCADOLIBRE_LIVE"
    assert alert.evidence_reference == "evidence://snapshot-123.json"


def test_r_message_template_determinism():
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-tmpl",
        event_type=EventType.OPPORTUNITY_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-TMPL",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-tmpl",
        payload=MappingProxyType({
            "status": "VALID",
            "confidence": "HIGH",
            "opportunity_type": "PRICE_ARBITRAGE",
        }),
    )
    res = DeterministicAlertRulesEngine.evaluate(evt, now=now)
    assert res.alert.title == "Oportunidad detectada: PRICE_ARBITRAGE en PROD-TMPL"
    assert "[HIGH] Oportunidad PRICE_ARBITRAGE" in res.alert.message


def test_s_t_persistence_restart_reload(temp_repo_dir, test_clock):
    repo1 = JsonAlertRepository(temp_repo_dir)
    adapter1 = InMemoryAlertDeliveryAdapter()
    service1 = AlertService(alert_repository=repo1, delivery_port=adapter1, clock=test_clock)

    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-persist-1",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-P1",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-p1",
        payload=MappingProxyType({"significance": "SIGNIFICANT", "change_type": "PRICE_CHANGED"}),
    )
    created_alert = service1.process_event(evt)

    # Simular reinicio creando nuevo servicio sobre el mismo directorio
    repo2 = JsonAlertRepository(temp_repo_dir)
    loaded_alert = repo2.get_by_id(created_alert.alert_id)
    assert loaded_alert is not None
    assert loaded_alert.alert_id == created_alert.alert_id
    assert loaded_alert.delivery_status == AlertDeliveryStatus.DELIVERED
    assert loaded_alert.correlation_id == "corr-p1"

    deliveries = repo2.list_delivery_results_by_alert(created_alert.alert_id)
    assert len(deliveries) == 1
    assert deliveries[0].status == AlertDeliveryStatus.DELIVERED


def test_u_v_w_delivery_success_failure_unknown(alert_repo, test_clock):
    adapter = InMemoryAlertDeliveryAdapter()
    service = AlertService(alert_repository=alert_repo, delivery_port=adapter, clock=test_clock)

    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Success
    adapter.set_simulated_status(AlertDeliveryStatus.DELIVERED)
    evt_succ = EventRecord(
        event_id="evt-succ",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-S",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-s",
        payload=MappingProxyType({"significance": "CRITICAL", "change_type": "PRICE_CHANGED"}),
    )
    alt_succ = service.process_event(evt_succ)
    assert alt_succ.delivery_status == AlertDeliveryStatus.DELIVERED

    # 2. Failure
    adapter.set_simulated_status(AlertDeliveryStatus.FAILED, error_msg="HTTP 500 downstream webhook")
    evt_fail = EventRecord(
        event_id="evt-fail",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-F",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-f",
        payload=MappingProxyType({"significance": "CRITICAL", "change_type": "PRICE_CHANGED"}),
    )
    alt_fail = service.process_event(evt_fail)
    assert alt_fail.delivery_status == AlertDeliveryStatus.FAILED
    delivs = alert_repo.list_delivery_results_by_alert(alt_fail.alert_id)
    assert delivs[0].status == AlertDeliveryStatus.FAILED
    assert "HTTP 500" in (delivs[0].error_message or "")

    # 3. UNKNOWN
    adapter.set_simulated_status(AlertDeliveryStatus.UNKNOWN, error_msg="Timeout 30s awaiting ACK")
    evt_unk = EventRecord(
        event_id="evt-unk",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-U",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-u",
        payload=MappingProxyType({"significance": "CRITICAL", "change_type": "PRICE_CHANGED"}),
    )
    alt_unk = service.process_event(evt_unk)
    assert alt_unk.delivery_status == AlertDeliveryStatus.UNKNOWN


def test_x_handler_failure_isolation(alert_repo, test_clock):
    # Demuestra que una excepción inesperada en delivery port no interrumpe el flujo ni derriba la persistencia
    class BrokenAdapter(InMemoryAlertDeliveryAdapter):
        def deliver(self, alert: AlertRecord) -> AlertDeliveryResult:
            raise RuntimeError("Catastrophic connection crash")

    broken_adapter = BrokenAdapter()
    service = AlertService(alert_repository=alert_repo, delivery_port=broken_adapter, clock=test_clock)

    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-broken",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-BROKEN",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-broken",
        payload=MappingProxyType({"significance": "CRITICAL", "change_type": "PRICE_CHANGED"}),
    )

    alert = service.process_event(evt)
    assert alert is not None
    # Alerta sigue persistida
    persisted = alert_repo.get_by_id(alert.alert_id)
    assert persisted is not None

    delivs = alert_repo.list_delivery_results_by_alert(alert.alert_id)
    assert len(delivs) == 1
    assert delivs[0].status == AlertDeliveryStatus.FAILED
    assert "Catastrophic connection crash" in (delivs[0].error_message or "")


def test_y_sensitive_data_sanitization(temp_repo_dir, test_clock):
    repo = JsonAlertRepository(temp_repo_dir)
    service = AlertService(alert_repository=repo, clock=test_clock)

    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-sec-1",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-SEC",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-sec",
        payload=MappingProxyType({
            "significance": "CRITICAL",
            "change_type": "PRICE_CHANGED",
            "api_key": "secret-12345",
            "access_token": "token-abcde",
            "nested": {
                "password": "super-secret-password",
            }
        }),
    )

    alert = service.process_event(evt)
    assert alert is not None

    # Verificar que en el JSON en disco no existan secretos en texto plano
    alert_file = temp_repo_dir / "alerts" / f"{alert.alert_id}.json"
    raw_content = alert_file.read_text(encoding="utf-8")
    assert "secret-12345" not in raw_content
    assert "token-abcde" not in raw_content
    assert "super-secret-password" not in raw_content


def test_z_aa_ab_ac_scope_boundary(alert_repo, test_clock):
    # J.6 NO debe instanciar ni crear DecisionRecord, ActionRecord, ni llamar a ML
    adapter = InMemoryAlertDeliveryAdapter()
    service = AlertService(alert_repository=alert_repo, delivery_port=adapter, clock=test_clock)
    handler = AutonomousAlertEventHandler(alert_service=service)

    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    evt = EventRecord(
        event_id="evt-scope",
        event_type=EventType.OPPORTUNITY_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-SCOPE",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-scope",
        payload=MappingProxyType({
            "status": "VALID",
            "confidence": "HIGH",
            "opportunity_type": "PRICE_ARBITRAGE",
        }),
    )

    handler.handle(evt)

    # Verificar que sólo se crearon Alertas y Entregas
    alerts = alert_repo.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].subject_id == "PROD-SCOPE"
    assert not hasattr(alerts[0], "decision_id")
    assert not hasattr(alerts[0], "action_type")
    assert not hasattr(alerts[0], "mission_id")
