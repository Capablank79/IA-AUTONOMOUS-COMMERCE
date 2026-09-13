"""
Tests Unitarios para Alerting de Producción (Hito P.8 — Production / Operations).

Cubre los 16 requisitos mínimos de diseño y arquitectura:
1. rule trigger (condición sobre umbral genera alerta)
2. below threshold no alert (métrica dentro de umbrales no genera alerta)
3. UNKNOWN no false resolution (datos insuficientes / ausentes no resuelven falsamente como healthy)
4. deduplication (alertas repetidas con misma clave canónica no se duplican)
5. cooldown (período de cooldown suprime spam repetitivo)
6. acknowledge != resolve (reconocer alerta cambia estado pero no indica resolución técnica)
7. automatic resolve with evidence (resolución automática sólo con evidencia normalizada)
8. reopen (reaparición de condición tras resolve genera o reabre alerta)
9. severity escalation (aumento de severidad actualiza in-place sin duplicar alerta)
10. environment isolation (separación estricta DEV / STAGING / PROD)
11. tenant isolation (alertas tenant-scoped no se filtran entre tenants)
12. sensitive evidence redacted (cero passwords, DSNs, tokens o PII en evidencia)
13. notification failure non-fatal (fallo en canal de notificación no rompe el flujo ni oculta la alerta)
14. alert != action (las alertas no modifican cuotas, subscripciones ni disparan backups)
15. manual resolve audited (resolución manual registra trazabilidad y actor)
16. no P.9+ (verificación de no implementación accidental de P.9 Log Retention o superior)
"""

from datetime import datetime, timezone, timedelta
import inspect
from typing import List, Optional
import pytest

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.monitoring.models import (
    MetricType,
    MetricUnit,
    MetricWindow,
    MonitoringScope,
)
from src.infrastructure.persistence.data.json.metric_repository import InMemoryMetricRepository, JsonMetricRepository
from src.application.monitoring.production_monitoring_service import ProductionMonitoringService
from src.domain.production_alerting.models import (
    AlertRule,
    AlertRuleType,
    AlertSeverity,
    AlertState,
    AlertEvaluationStatus,
    ProductionAlertInstance,
    ProductionAlertScope,
    NotificationMessage,
    NotificationResult,
    NotificationDeliveryStatus,
    generate_deduplication_key,
)
from src.domain.production_alerting.ports import (
    NotificationPort,
    ProductionAlertRepositoryPort,
)
from src.infrastructure.persistence.data.json.production_alert_repository import (
    JsonProductionAlertRepository,
)
from src.application.production_alerting.production_alerting_service import (
    ProductionAlertingService,
)


class MockNotificationPort(NotificationPort):
    def __init__(self, channel_name: str = "mock_channel", should_fail: bool = False) -> None:
        self._channel = channel_name
        self.should_fail = should_fail
        self.sent_messages: List[NotificationMessage] = []

    @property
    def channel_name(self) -> str:
        return self._channel

    def send(self, message: NotificationMessage) -> NotificationResult:
        if self.should_fail:
            return NotificationResult(
                delivery_id=f"del_{len(self.sent_messages)+1}",
                notification_id=message.notification_id,
                alert_id=message.alert_id,
                channel=self._channel,
                status=NotificationDeliveryStatus.FAILED,
                attempted_at=datetime.now(timezone.utc),
                error_message="Simulated connection timeout to notification endpoint",
            )
        self.sent_messages.append(message)
        return NotificationResult(
            delivery_id=f"del_{len(self.sent_messages)}",
            notification_id=message.notification_id,
            alert_id=message.alert_id,
            channel=self._channel,
            status=NotificationDeliveryStatus.SENT,
            attempted_at=datetime.now(timezone.utc),
        )


@pytest.fixture
def metric_repo() -> InMemoryMetricRepository:
    return InMemoryMetricRepository()


@pytest.fixture
def monitoring_service(metric_repo: InMemoryMetricRepository) -> ProductionMonitoringService:
    return ProductionMonitoringService(
        repository=metric_repo,
        environment=ApplicationEnvironment.PRODUCTION,
    )


@pytest.fixture
def alert_repo() -> JsonProductionAlertRepository:
    return JsonProductionAlertRepository()


@pytest.fixture
def mock_notifier() -> MockNotificationPort:
    return MockNotificationPort()


@pytest.fixture
def alerting_service(
    alert_repo: JsonProductionAlertRepository,
    monitoring_service: ProductionMonitoringService,
    mock_notifier: MockNotificationPort,
) -> ProductionAlertingService:
    return ProductionAlertingService(
        repository=alert_repo,
        monitoring_service=monitoring_service,
        notification_ports=[mock_notifier],
        environment=ApplicationEnvironment.PRODUCTION,
    )


def test_01_rule_trigger(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
) -> None:
    """1. rule trigger: Condición sobre umbral genera alerta estructurada y envía notificación."""
    # 5 requests con 4 fallos 500 -> 80% error rate
    monitoring_service.record_request_metric("GET", "/items", 200, 10.0)
    for _ in range(4):
        monitoring_service.record_request_metric("GET", "/items", 500, 20.0)

    alerts = alerting_service.evaluate_all_rules()
    assert len(alerts) >= 1
    error_alert = next((a for a in alerts if a.rule_type == AlertRuleType.HIGH_ERROR_RATE), None)
    assert error_alert is not None
    assert error_alert.state == AlertState.ACTIVE
    assert error_alert.severity == AlertSeverity.HIGH
    assert error_alert.evidence["value"] == 80.0


def test_02_below_threshold_no_alert(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
) -> None:
    """2. below threshold no alert: Métricas dentro de límites normales no generan alerta."""
    for _ in range(10):
        monitoring_service.record_request_metric("GET", "/items", 200, 15.0)

    alerts = alerting_service.evaluate_all_rules()
    assert len(alerts) == 0


def test_03_unknown_no_false_resolution(
    alerting_service: ProductionAlertingService,
) -> None:
    """3. UNKNOWN no false resolution: Sin muestras suficientes el resultado es INSUFFICIENT_DATA."""
    rule = AlertRule(
        rule_type=AlertRuleType.HIGH_ERROR_RATE,
        metric_type=MetricType.ERROR_RATE,
        window=MetricWindow.WINDOW_5M,
        severity=AlertSeverity.HIGH,
        threshold_value=5.0,
        description="High error rate",
        min_sample_count=5,
    )
    res = alerting_service.evaluate_rule(rule)
    assert res.status == AlertEvaluationStatus.INSUFFICIENT_DATA
    assert res.is_unknown is True
    assert res.current_value is None


def test_04_deduplication(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
    alert_repo: JsonProductionAlertRepository,
) -> None:
    """4. deduplication: Evaluaciones repetidas de la misma condición no generan duplicados."""
    for _ in range(5):
        monitoring_service.record_request_metric("GET", "/items", 500, 20.0)

    # Primera evaluación
    alerts1 = alerting_service.evaluate_all_rules()
    assert len(alerts1) == 1
    original_id = alerts1[0].alert_id

    # Segunda evaluación inmediata
    alerts2 = alerting_service.evaluate_all_rules()
    assert len(alerts2) == 1
    assert alerts2[0].alert_id == original_id

    all_alerts = alert_repo.list_alerts(environment=ApplicationEnvironment.PRODUCTION)
    assert len(all_alerts) == 1


def test_05_cooldown(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
    mock_notifier: MockNotificationPort,
) -> None:
    """5. cooldown: Durante el período de cooldown no se genera spam repetitivo de notificaciones."""
    for _ in range(5):
        monitoring_service.record_request_metric("GET", "/items", 500, 20.0)

    # 1era evaluación -> envía notificación
    alerting_service.evaluate_all_rules()
    assert len(mock_notifier.sent_messages) == 1

    # 2da evaluación en cooldown -> no reenvía notificación
    alerting_service.evaluate_all_rules()
    assert len(mock_notifier.sent_messages) == 1


def test_06_acknowledge_not_resolve(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
) -> None:
    """6. acknowledge != resolve: Reconocer alerta no indica que el problema esté resuelto."""
    for _ in range(5):
        monitoring_service.record_request_metric("GET", "/items", 500, 20.0)

    alerts = alerting_service.evaluate_all_rules()
    alert_id = alerts[0].alert_id

    ack_alert = alerting_service.acknowledge_alert(alert_id=alert_id, actor_id="operator_alice")
    assert ack_alert.state == AlertState.ACKNOWLEDGED
    assert ack_alert.acknowledged_by == "operator_alice"
    assert ack_alert.resolved_at is None

    # Re-evaluación con problema activo mantiene ACKNOWLEDGED sin crear otra alerta
    alerts_again = alerting_service.evaluate_all_rules()
    assert len(alerts_again) == 1
    assert alerts_again[0].state == AlertState.ACKNOWLEDGED


def test_07_automatic_resolve_with_evidence(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
    metric_repo: JsonMetricRepository,
) -> None:
    """7. automatic resolve with evidence: Resolución automática ocurre únicamente con evidencia sana."""
    # Disparar alerta con fallos en DB
    monitoring_service.record_database_check(is_available=False)
    alerts = alerting_service.evaluate_all_rules()
    db_alert = next(a for a in alerts if a.rule_type == AlertRuleType.DATABASE_UNAVAILABLE)
    assert db_alert.state == AlertState.ACTIVE

    # Recuperación: Registrar check de DB exitoso
    metric_repo.clear()
    monitoring_service.record_database_check(is_available=True, latency_ms=2.5)

    alerts_after = alerting_service.evaluate_all_rules()
    resolved_db = next(a for a in alerts_after if a.rule_type == AlertRuleType.DATABASE_UNAVAILABLE)
    assert resolved_db.state == AlertState.RESOLVED
    assert resolved_db.resolved_by == "system_auto_resolve"


def test_08_reopen(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
    metric_repo: JsonMetricRepository,
) -> None:
    """8. reopen: Si la condición reaparece tras estar RESOLVED, se crea/reabre alerta."""
    # 1. Fallo inicial -> Alert
    monitoring_service.record_database_check(is_available=False)
    alerting_service.evaluate_all_rules()

    # 2. Recuperación -> Resolve
    metric_repo.clear()
    monitoring_service.record_database_check(is_available=True)
    alerting_service.evaluate_all_rules()

    # 3. Nuevo fallo -> Nueva Alerta activa
    metric_repo.clear()
    monitoring_service.record_database_check(is_available=False)
    alerts_reopened = alerting_service.evaluate_all_rules()
    active_reopened = next(a for a in alerts_reopened if a.rule_type == AlertRuleType.DATABASE_UNAVAILABLE)
    assert active_reopened.state == AlertState.ACTIVE


def test_09_severity_escalation(
    monitoring_service: ProductionMonitoringService,
    alert_repo: JsonProductionAlertRepository,
    mock_notifier: MockNotificationPort,
) -> None:
    """9. severity escalation: Aumento de severidad escala in-place sin duplicar la alerta."""
    custom_rules = [
        AlertRule(
            rule_type=AlertRuleType.HIGH_ERROR_RATE,
            metric_type=MetricType.ERROR_RATE,
            window=MetricWindow.WINDOW_5M,
            severity=AlertSeverity.WARNING,
            threshold_value=5.0,
            description="Warning error rate",
            min_sample_count=2,
        ),
        AlertRule(
            rule_type=AlertRuleType.HIGH_ERROR_RATE,
            metric_type=MetricType.ERROR_RATE,
            window=MetricWindow.WINDOW_5M,
            severity=AlertSeverity.CRITICAL,
            threshold_value=50.0,
            description="Critical error rate",
            min_sample_count=2,
        ),
    ]
    service = ProductionAlertingService(
        repository=alert_repo,
        monitoring_service=monitoring_service,
        rules=custom_rules,
        notification_ports=[mock_notifier],
        environment=ApplicationEnvironment.PRODUCTION,
    )

    # 10% error rate -> dispara WARNING
    monitoring_service.record_request_metric("GET", "/test", 200, 10.0)
    monitoring_service.record_request_metric("GET", "/test", 500, 10.0)
    service.evaluate_all_rules()
    alerts = alert_repo.list_alerts(environment=ApplicationEnvironment.PRODUCTION)
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING

    # 100% error rate -> escala a CRITICAL sobre la misma alerta
    for _ in range(5):
        monitoring_service.record_request_metric("GET", "/test", 500, 10.0)
    service.evaluate_all_rules()
    alerts_escalated = alert_repo.list_alerts(environment=ApplicationEnvironment.PRODUCTION)
    assert len(alerts_escalated) == 1
    assert alerts_escalated[0].alert_id == alerts[0].alert_id
    assert alerts_escalated[0].severity == AlertSeverity.CRITICAL


def test_10_environment_isolation(
    metric_repo: JsonMetricRepository,
    alert_repo: JsonProductionAlertRepository,
) -> None:
    """10. environment isolation: Las alertas de DEV, STAGING y PROD están estrictamente separadas."""
    dev_mon = ProductionMonitoringService(metric_repo, environment=ApplicationEnvironment.DEVELOPMENT)
    prod_mon = ProductionMonitoringService(metric_repo, environment=ApplicationEnvironment.PRODUCTION)

    dev_alert = ProductionAlertingService(alert_repo, dev_mon, environment=ApplicationEnvironment.DEVELOPMENT)
    prod_alert = ProductionAlertingService(alert_repo, prod_mon, environment=ApplicationEnvironment.PRODUCTION)

    # Generar fallo sólo en DEV
    dev_mon.record_database_check(is_available=False)
    dev_alert.evaluate_all_rules()

    # Verificar que PROD no ve la alerta de DEV
    prod_alerts = prod_alert.list_alerts(environment=ApplicationEnvironment.PRODUCTION)
    dev_alerts = dev_alert.list_alerts(environment=ApplicationEnvironment.DEVELOPMENT)

    assert len(prod_alerts) == 0
    assert len(dev_alerts) == 1
    assert dev_alerts[0].environment == ApplicationEnvironment.DEVELOPMENT


def test_11_tenant_isolation(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
) -> None:
    """11. tenant isolation: Alertas tenant-scoped de Tenant A no son accesibles por Tenant B."""
    rule = AlertRule(
        rule_type=AlertRuleType.QUOTA_EXHAUSTION,
        metric_type=MetricType.QUOTA_DENIAL_COUNT,
        window=MetricWindow.WINDOW_1H,
        severity=AlertSeverity.WARNING,
        threshold_value=1.0,
        comparison_operator=">=",
        description="Quota exhaustion",
        scope=ProductionAlertScope.TENANT,
        min_sample_count=1,
    )
    service = ProductionAlertingService(
        repository=alerting_service._repository,
        monitoring_service=monitoring_service,
        rules=[rule],
        environment=ApplicationEnvironment.PRODUCTION,
    )

    # Métrica para tenant-123
    monitoring_service.record_quota_denial("token_limit", tenant_id="tenant-123")
    service.evaluate_all_rules(tenant_id="tenant-123")

    t1_alerts = service.list_alerts(tenant_id="tenant-123")
    t2_alerts = service.list_alerts(tenant_id="tenant-456")

    assert len(t1_alerts) == 1
    assert len(t2_alerts) == 0


def test_12_sensitive_evidence_redacted(
    alert_repo: JsonProductionAlertRepository,
) -> None:
    """12. sensitive evidence redacted: Contraseñas, DSNs, tokens y PII son sanitizados de la evidencia."""
    dirty_evidence = {
        "password": "super_secret_pwd",
        "api_token": "bearer eyJhbGciOi...",
        "access_token": "secret123",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----",
        "safe_metric_value": 42.0,
    }
    alert = ProductionAlertInstance(
        alert_id="alt_clean123",
        rule_type=AlertRuleType.DATABASE_UNAVAILABLE,
        severity=AlertSeverity.CRITICAL,
        state=AlertState.ACTIVE,
        environment=ApplicationEnvironment.PRODUCTION,
        scope=ProductionAlertScope.PLATFORM,
        deduplication_key="prod:platform:db:default",
        summary="DB check failure",
        evidence=dirty_evidence,
        triggered_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    saved = alert_repo.save_alert(alert)
    loaded = alert_repo.get_alert_by_id(ApplicationEnvironment.PRODUCTION, "alt_clean123")
    assert loaded is not None

    evidence_dict = dict(loaded.evidence)
    assert evidence_dict["password"] == "[REDACTED]"
    assert evidence_dict["api_token"] == "[REDACTED]"
    assert evidence_dict["access_token"] == "[REDACTED]"
    assert evidence_dict["private_key"] == "[REDACTED]"
    assert evidence_dict["safe_metric_value"] == 42.0


def test_13_notification_failure_non_fatal(
    monitoring_service: ProductionMonitoringService,
    alert_repo: JsonProductionAlertRepository,
) -> None:
    """13. notification failure non-fatal: Fallo en canal de notificación no afecta la creación ni validez de la alerta."""
    failing_notifier = MockNotificationPort(should_fail=True)
    service = ProductionAlertingService(
        repository=alert_repo,
        monitoring_service=monitoring_service,
        notification_ports=[failing_notifier],
        environment=ApplicationEnvironment.PRODUCTION,
    )

    monitoring_service.record_database_check(is_available=False)
    alerts = service.evaluate_all_rules()
    assert len(alerts) == 1
    assert alerts[0].state == AlertState.ACTIVE

    # Alerta persiste activa en el repositorio a pesar del fallo en el puerto de notificación
    stored = alert_repo.get_alert_by_id(ApplicationEnvironment.PRODUCTION, alerts[0].alert_id)
    assert stored is not None


def test_14_alert_not_action(
    alerting_service: ProductionAlertingService,
) -> None:
    """14. alert != action: La alerta no ejecuta acciones automáticas (no muta cuotas ni servicios)."""
    # Verificar que el servicio de alerting solo posee métodos de ciclo de vida y consulta
    forbidden_methods = ["cancel_subscription", "stop_service", "apply_emergency_stop", "modify_quota", "restore_backup"]
    for m in forbidden_methods:
        assert not hasattr(alerting_service, m)


def test_15_manual_resolve_audited(
    monitoring_service: ProductionMonitoringService,
    alerting_service: ProductionAlertingService,
) -> None:
    """15. manual resolve audited: Resolución manual por un operador queda registrada con trazabilidad."""
    monitoring_service.record_database_check(is_available=False)
    alerts = alerting_service.evaluate_all_rules()
    alert_id = alerts[0].alert_id

    resolved = alerting_service.resolve_alert(
        alert_id=alert_id,
        actor_id="operator_bob",
        reason="Manual failover completed and verified",
    )
    assert resolved.state == AlertState.RESOLVED
    assert resolved.resolved_by == "operator_bob"
    assert resolved.resolution_reason == "Manual failover completed and verified"
    assert resolved.resolved_at is not None


def test_16_no_p9_plus_dependencies() -> None:
    """16. P.8 opera sin importar ni requerir módulos de P.9+ (Log Retention / Capacity Planning)."""
    import src.domain.production_alerting as a_domain
    import src.application.production_alerting as a_app
    
    src_domain = inspect.getsource(a_domain)
    src_app = inspect.getsource(a_app)

    forbidden = ["log_retention", "capacity_planning", "rate_limit_management", "auto_remediation"]
    for word in forbidden:
        assert word not in src_domain.lower()
        assert word not in src_app.lower()
