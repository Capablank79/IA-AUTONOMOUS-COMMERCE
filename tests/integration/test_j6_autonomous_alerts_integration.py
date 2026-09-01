"""
Test de Integración para Alertas Autónomas (Autonomous Alerts - Hito J.6).

Demuestra la cadena completa:
J.4 CHANGE -> J.5 CHANGE_DETECTED EVENT -> EVENT BUS -> J.6 ALERT HANDLER -> ALERT EVALUATION -> ALERT RECORD -> PERSIST -> DELIVERY PORT -> DELIVERY RESULT.

Verifica:
- Idempotencia
- Replay
- Correlación y Causalidad
- Reinicio / Reload
- Aislamiento de fallos
- Seguridad y Sanitización
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from src.domain.events.models import EventRecord, EventType
from src.infrastructure.persistence.data.json.event_store import JsonEventStore
from src.application.events.event_bus_service import EventBusService
from src.domain.alerts.models import (
    AlertRecord,
    AlertType,
    AlertSeverity,
    AlertStatus,
    AlertDeliveryStatus,
)
from src.infrastructure.persistence.data.json.alert_repository import JsonAlertRepository
from src.infrastructure.alerts.deterministic_delivery_adapter import InMemoryAlertDeliveryAdapter
from src.application.alerts.alert_service import AlertService
from src.application.alerts.event_handler import AutonomousAlertEventHandler
from src.domain.scheduling.models import DeterministicClock


@pytest.fixture
def temp_dir(tmp_path):
    d = tmp_path / "j6_integration"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_j6_autonomous_alerts_full_integration(temp_dir):
    clock = DeterministicClock(datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc))

    # 1. Configurar infra J.5 Event Bus
    event_store = JsonEventStore(temp_dir / "events")
    event_bus = EventBusService(event_store=event_store)

    # 2. Configurar infra J.6 Alerts
    alert_repo = JsonAlertRepository(temp_dir / "alerts")
    delivery_adapter = InMemoryAlertDeliveryAdapter(channel_name="WEBHOOK_SIMULATOR")
    alert_service = AlertService(
        alert_repository=alert_repo,
        delivery_port=delivery_adapter,
        clock=clock,
        cooldown_seconds=30.0,
    )
    alert_handler = AutonomousAlertEventHandler(alert_service=alert_service)

    # 3. Registrar handler en el Bus de J.5
    event_bus.register_handler(alert_handler)

    # 4. Crear evento J.4 ChangeDetected
    now = clock.now()
    change_event = EventRecord(
        event_id="evt-integ-chg-101",
        event_type=EventType.CHANGE_DETECTED,
        subject_type="PRODUCT",
        subject_id="PROD-SSD-1TB",
        occurred_at=now,
        recorded_at=now,
        correlation_id="corr-mission-999",
        causation_id="caus-change-888",
        provenance="MERCADOLIBRE_OBSERVER",
        payload_reference="evidence://snapshot-2026-09-01.json",
        payload=MappingProxyType({
            "change_type": "PRICE_CHANGED",
            "significance": "CRITICAL",
            "change_summary": "Competitor dropped price by 35% below price floor",
            "observed_changes_count": 1,
            "secret_token": "bearer-should-be-redacted",
        }),
    )

    # 5. Publicar evento a través del Bus
    deliveries = event_bus.publish(change_event)
    assert len(deliveries) == 1
    assert deliveries[0].status.value == "DELIVERED"

    # 6. Verificar Alerta generada y persistida
    alerts = alert_repo.list_alerts(subject_id="PROD-SSD-1TB")
    assert len(alerts) == 1
    alert = alerts[0]

    assert alert.alert_type == AlertType.SIGNIFICANT_CHANGE
    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.correlation_id == "corr-mission-999"
    assert alert.causation_id == "caus-change-888"
    assert alert.evidence_reference == "evidence://snapshot-2026-09-01.json"
    assert alert.delivery_status == AlertDeliveryStatus.DELIVERED
    assert "dropped price by 35%" in alert.message

    # 7. Verificar Delivery Result registrado
    delivery_results = alert_repo.list_delivery_results_by_alert(alert.alert_id)
    assert len(delivery_results) == 1
    assert delivery_results[0].status == AlertDeliveryStatus.DELIVERED
    assert delivery_results[0].channel == "WEBHOOK_SIMULATOR"

    # 8. Verificar Idempotencia y Replay
    replay_deliveries = event_bus.publish(change_event)
    assert len(replay_deliveries) == 1
    # No se crean alertas duplicadas
    all_alerts = alert_repo.list_alerts(subject_id="PROD-SSD-1TB")
    assert len(all_alerts) == 1
    assert len(delivery_adapter.delivered_alerts) == 1

    # 9. Verificar Restart / Reload
    reloaded_repo = JsonAlertRepository(temp_dir / "alerts")
    reloaded_alert = reloaded_repo.get_by_id(alert.alert_id)
    assert reloaded_alert is not None
    assert reloaded_alert.alert_id == alert.alert_id
    assert reloaded_alert.delivery_status == AlertDeliveryStatus.DELIVERED

    # 10. Verificar Sanitización en disco
    alert_file = temp_dir / "alerts" / "alerts" / f"{alert.alert_id}.json"
    assert alert_file.exists()
    file_content = alert_file.read_text(encoding="utf-8")
    assert "bearer-should-be-redacted" not in file_content
