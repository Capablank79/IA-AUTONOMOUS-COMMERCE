"""
Pruebas unitarias de O.12 — SaaS Observability (TenantObservabilityService y contratos).

Cobertura:
- Tenant isolation estricto (alertas y snapshots por tenant).
- UNKNOWN != ZERO: ausencia de fuentes no produce 0 ni HEALTHY artificiales.
- Agregación de hechos O.6 (REQUEST_COUNT, ERROR_RATE, CACHE_HIT_RATE, MODEL_REQUEST_COUNT).
- Contratos reales: Billing get_active_subscription(tenant_id, current_time=now) y
  Emergency Stop list_active_records(now).
- Ciclo de vida de alertas y deduplicación por deduplication_key.
- Alert != Automatic Action (evaluar alertas no muta billing ni emergency stop).
- Rechazo de identificadores inseguros (path traversal).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from src.application.saas_observability.tenant_observability_service import (
    TenantObservabilityService,
)
from src.domain.saas_observability.models import (
    MetricType,
    TenantHealthStatus,
    AlertSeverity,
    AlertStatus,
    OperationalAlertType,
    OperationalAlert,
    ObservabilityIntegrityError,
)
from src.domain.saas_observability.ports import OperationalAlertRepositoryPort
from src.domain.usage_metering.models import (
    UsageAggregate,
    DimensionUsageSummary,
    UsageQuery,
)
from src.domain.emergency_stop.models import (
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
    EmergencyStopReasonCode,
)
from src.domain.billing.models import SubscriptionStatus, BillingCycle
from src.domain.reliability.ports import ClockPort


class FixedClock(ClockPort):
    def __init__(self):
        self.current = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


class FakeAlertRepository(OperationalAlertRepositoryPort):
    """Repositorio de alertas en memoria particionado por tenant."""

    def __init__(self):
        self._alerts = {}  # (tenant_id, alert_id) -> OperationalAlert

    def save_alert(self, alert: OperationalAlert) -> OperationalAlert:
        self._alerts[(alert.tenant_id, alert.alert_id)] = alert
        return alert

    def get_alert_by_id(self, tenant_id: str, alert_id: str):
        return self._alerts.get((tenant_id, alert_id))

    def list_alerts(self, tenant_id, status=None, organization_id=None, limit=100):
        results = [
            a
            for (t, _), a in self._alerts.items()
            if t == tenant_id
            and (status is None or a.status == status)
            and (organization_id is None or a.organization_id == organization_id)
        ]
        return results[:limit]

    def get_active_alert_by_deduplication_key(self, tenant_id, deduplication_key):
        for (t, _), a in self._alerts.items():
            if t == tenant_id and a.status == AlertStatus.ACTIVE and a.deduplication_key == deduplication_key:
                return a
        return None


class FlexibleUsageMeteringService:
    """Reemplazo ligero del contrato aggregate_usage(context, query) del port O.6."""

    def __init__(self, aggregate):
        self.aggregate = aggregate

    def aggregate_usage(self, context, query: UsageQuery) -> UsageAggregate:
        return self.aggregate


class FakeSubscriptionRepository:
    """Implementa solo el contrato real utilizado: get_active_subscription(tenant_id, current_time)."""

    def __init__(self, subscription=None):
        self.subscription = subscription
        self.last_current_time = None
        self.last_tenant_id = None
        self.save_calls = 0

    def get_active_subscription(self, tenant_id, current_time=None):
        self.last_tenant_id = tenant_id
        self.last_current_time = current_time
        return self.subscription

    def save_subscription(self, subscription):
        self.save_calls += 1


class FakeEmergencyStopRepository:
    """Implementa el contrato real list_active_records(current_time) del port N.11."""

    def __init__(self, records=()):
        self.records = list(records)
        self.last_current_time = None

    def list_active_records(self, current_time):
        self.last_current_time = current_time
        return list(self.records)


@pytest.fixture
def clock():
    return FixedClock()


@pytest.fixture
def alert_repo():
    return FakeAlertRepository()


def _make_aggregate(
    tenant_id="tenant_a",
    total=100,
    success=95,
    failed=5,
    cached=20,
    tokens=1000,
    estimated="2.50",
    actual="2.50",
    breakdown_by_model=None,
):
    return UsageAggregate(
        tenant_id=tenant_id,
        period=None,
        total_requests=total,
        successful_requests=success,
        failed_requests=failed,
        cached_requests=cached,
        total_input_tokens=600,
        total_output_tokens=400,
        total_tokens=tokens,
        unknown_token_events_count=0,
        total_estimated_cost=Decimal(estimated),
        total_actual_cost=Decimal(actual),
        unknown_actual_cost_events_count=0,
        breakdown_by_model=breakdown_by_model or {},
    )


def _make_emergency_record(stop_id="stop_1", scope=EmergencyStopScope.GLOBAL, target_id=None, state=EmergencyStopState.ACTIVE):
    return EmergencyStopRecord(
        stop_id=stop_id,
        scope=scope,
        state=state,
        reason_code=EmergencyStopReasonCode.MANUAL_OPERATOR_HALT,
        reason_details="test",
        activated_by_identity_id="admin_1",
        activated_at=datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc),
        target_id=target_id,
        expires_at=None,
    )


def _make_alert(tenant_id="tenant_a", alert_id="alt_1", dedup_key=None, status=AlertStatus.ACTIVE, severity=AlertSeverity.HIGH):
    return OperationalAlert(
        alert_id=alert_id,
        tenant_id=tenant_id,
        alert_type=OperationalAlertType.HIGH_ERROR_RATE,
        severity=severity,
        status=status,
        summary="test alert",
        details={"total_requests": 10, "failed_requests": 8},
        triggered_at=datetime(2026, 9, 9, 11, 30, tzinfo=timezone.utc),
        deduplication_key=dedup_key or f"high_error_rate:{tenant_id}",
    )


def _service(alert_repo, clock, usage=None, subscription=None, emergency=None):
    return TenantObservabilityService(
        alert_repository=alert_repo,
        clock=clock,
        usage_metering_service=usage,
        subscription_repository=subscription,
        emergency_stop_repository=emergency,
    )


# ---------------------------------------------------------------------------
# UNKNOWN != ZERO: sin fuentes estructuradas no hay métricas fabricadas
# ---------------------------------------------------------------------------


def test_snapshot_sin_fuentes_todo_unknown_no_cero(alert_repo, clock):
    service = _service(alert_repo, clock)
    snapshot = service.get_tenant_snapshot(tenant_id="tenant_a", window_seconds=3600)

    assert snapshot.request_count is None
    assert snapshot.error_rate is None
    assert snapshot.avg_latency_ms is None
    assert snapshot.p95_latency_ms is None
    assert snapshot.total_tokens is None
    assert snapshot.total_cost_usd is None
    assert snapshot.billing_status == "UNKNOWN"
    assert snapshot.quota_status == "UNKNOWN"
    assert snapshot.health_status == TenantHealthStatus.UNKNOWN
    assert snapshot.metrics == {}
    # No se inventan conteos de quota/provider/auth/emergency
    for metric_type in (
        MetricType.QUOTA_REJECTION_COUNT,
        MetricType.PROVIDER_ERROR_COUNT,
        MetricType.AUTHORIZATION_DENIAL_COUNT,
        MetricType.EMERGENCY_STOP_BLOCK_COUNT,
        MetricType.CACHE_HIT_RATE,
        MetricType.MODEL_REQUEST_COUNT,
    ):
        assert metric_type.value not in snapshot.metrics


def test_snapshot_canary_y_determinismo(alert_repo, clock):
    snapshot_a = _service(alert_repo, clock).get_tenant_snapshot("tenant_a")
    snapshot_b = _service(alert_repo, clock).get_tenant_snapshot("tenant_a")
    assert snapshot_a.checksum == snapshot_b.checksum
    assert snapshot_a.to_dict() == snapshot_b.to_dict()


# ---------------------------------------------------------------------------
# Agregación desde hechos O.6
# ---------------------------------------------------------------------------


def test_snapshot_agrega_hechos_usage_o6(alert_repo, clock):
    usage = FlexibleUsageMeteringService(
        _make_aggregate(
            breakdown_by_model={
                "gpt-4o": DimensionUsageSummary(dimension_key="model", dimension_value="gpt-4o", total_requests=60),
                "claude-3": DimensionUsageSummary(dimension_key="model", dimension_value="claude-3", total_requests=40),
            }
        )
    )
    snapshot = _service(alert_repo, clock, usage=usage).get_tenant_snapshot("tenant_a")

    assert snapshot.request_count == 100
    assert snapshot.error_rate == 0.05
    assert snapshot.total_tokens == 1000
    assert snapshot.total_cost_usd == Decimal("2.50")
    assert snapshot.metrics[MetricType.REQUEST_COUNT.value].value == 100
    assert snapshot.metrics[MetricType.ERROR_RATE.value].value == 0.05
    assert snapshot.metrics[MetricType.CACHE_HIT_RATE.value].value == 0.2
    assert snapshot.metrics[MetricType.CACHE_HIT_RATE.value].unit.value == "RATIO"
    assert snapshot.metrics[MetricType.MODEL_REQUEST_COUNT.value].value == 100
    assert snapshot.metrics[MetricType.SUCCESS_COUNT.value].value == 95
    assert snapshot.metrics[MetricType.FAILURE_COUNT.value].value == 5
    assert snapshot.health_status == TenantHealthStatus.HEALTHY


def test_snapshot_trafico_cero_rates_unknown(alert_repo, clock):
    usage = FlexibleUsageMeteringService(_make_aggregate(total=0, success=0, failed=0, cached=0))
    snapshot = _service(alert_repo, clock, usage=usage).get_tenant_snapshot("tenant_a")

    assert snapshot.request_count == 0  # hecho O.6 (cero eventos) no es inventado
    assert snapshot.error_rate is None
    assert snapshot.health_status == TenantHealthStatus.UNKNOWN
    assert MetricType.CACHE_HIT_RATE.value not in snapshot.metrics
    assert MetricType.MODEL_REQUEST_COUNT.value not in snapshot.metrics


# ---------------------------------------------------------------------------
# Contrato real de Billing: get_active_subscription(tenant_id, current_time=now)
# ---------------------------------------------------------------------------


def test_billing_usa_contrato_get_active_subscription(alert_repo, clock):
    sub_status = SubscriptionStatus.ACTIVE.value
    sub = _fake_subscription(status=sub_status, tenant_id="tenant_a")
    subscription = FakeSubscriptionRepository(sub)
    service = _service(alert_repo, clock, subscription=subscription)

    snapshot = service.get_tenant_snapshot("tenant_a")
    assert subscription.last_tenant_id == "tenant_a"
    assert subscription.last_current_time == clock.now()
    assert snapshot.billing_status == SubscriptionStatus.ACTIVE.value
    # Sin tráfico, suscripción activa y sin anomalías -> HEALTHY
    assert snapshot.health_status == TenantHealthStatus.HEALTHY


def test_billing_sin_suscripcion_no_subscription(alert_repo, clock):
    subscription = FakeSubscriptionRepository(None)
    snapshot = _service(alert_repo, clock, subscription=subscription).get_tenant_snapshot("tenant_a")
    assert snapshot.billing_status == "NO_SUBSCRIPTION"
    assert snapshot.health_status == TenantHealthStatus.UNKNOWN


def test_billing_past_due_genera_alerta_y_dedup(alert_repo, clock):
    subscription = FakeSubscriptionRepository(
        _fake_subscription(status=SubscriptionStatus.PAST_DUE.value, tenant_id="tenant_a")
    )
    service = _service(alert_repo, clock, subscription=subscription)

    alerts_1 = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    billing_alerts = [a for a in alerts_1 if a.alert_type == OperationalAlertType.BILLING_PAST_DUE]
    assert len(billing_alerts) == 1
    assert billing_alerts[0].severity == AlertSeverity.HIGH
    first_id = billing_alerts[0].alert_id
    assert subscription.last_current_time == clock.now()

    # Deduplicación: segunda evaluación con la misma ventana no crea otra alerta
    alerts_2 = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    billing_alerts_2 = [a for a in alerts_2 if a.alert_type == OperationalAlertType.BILLING_PAST_DUE]
    assert len(billing_alerts_2) == 1
    assert billing_alerts_2[0].alert_id == first_id
    assert alert_repo.get_active_alert_by_deduplication_key("tenant_a", f"billing_past_due:tenant_a") is not None

    # Al resolver la condición, la alerta se resuelve automáticamente
    subscription.subscription = _fake_subscription(
        status=SubscriptionStatus.ACTIVE.value, tenant_id="tenant_a"
    )
    alerts_3 = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    assert all(a.status != AlertStatus.ACTIVE for a in alerts_3 if a.alert_type == OperationalAlertType.BILLING_PAST_DUE)


def _fake_subscription(status, tenant_id):
    import types as _types
    sub = _types.SimpleNamespace(
        tenant_id=tenant_id,
        status=type("Status", (), {"value": status})(),
        plan_id="plan_pro",
    )
    return sub


# ---------------------------------------------------------------------------
# Contrato real de Emergency Stop: list_active_records(now)
# ---------------------------------------------------------------------------


def test_emergency_stop_usa_contrato_list_active_records_global(alert_repo, clock):
    emergency = FakeEmergencyStopRepository([_make_emergency_record()])
    service = _service(alert_repo, clock, emergency=emergency)

    alerts = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    assert emergency.last_current_time == clock.now()
    stops = [a for a in alerts if a.alert_type == OperationalAlertType.EMERGENCY_STOP_ACTIVE]
    assert len(stops) == 1
    assert stops[0].severity == AlertSeverity.CRITICAL

    snapshot = service.get_tenant_snapshot("tenant_a")
    assert snapshot.health_status == TenantHealthStatus.UNHEALTHY

    # Dedup: segunda evaluación no duplica la alerta
    alerts_2 = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    stops_2 = [a for a in alerts_2 if a.alert_type == OperationalAlertType.EMERGENCY_STOP_ACTIVE]
    assert len(stops_2) == 1
    assert stops_2[0].alert_id == stops[0].alert_id


def test_emergency_stop_scope_acount_aislado_por_target(alert_repo, clock):
    record = _make_emergency_record(stop_id="stop_acct", scope=EmergencyStopScope.ACCOUNT, target_id="tenant_a")
    emergency = FakeEmergencyStopRepository([record])
    service = _service(alert_repo, clock, emergency=emergency)

    alerts_a = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    assert any(a.alert_type == OperationalAlertType.EMERGENCY_STOP_ACTIVE for a in alerts_a)

    alerts_b = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_b")
    assert not any(a.alert_type == OperationalAlertType.EMERGENCY_STOP_ACTIVE for a in alerts_b)


def test_emergency_stop_desactivado_resuelve_alerta(alert_repo, clock):
    emergency = FakeEmergencyStopRepository([_make_emergency_record()])
    service = _service(alert_repo, clock, emergency=emergency)
    service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")

    emergency.records = []
    alerts = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    resolved = [a for a in alert_repo.list_alerts("tenant_a") if a.status == AlertStatus.RESOLVED]
    assert len(resolved) == 1
    assert all(a.status != AlertStatus.ACTIVE for a in alerts if a.alert_type == OperationalAlertType.EMERGENCY_STOP_ACTIVE)


# ---------------------------------------------------------------------------
# Ciclo de vida de alertas (acknowledge / resolve) y aislamiento por tenant
# ---------------------------------------------------------------------------


def test_acknowledge_y_resolve_ciclo_de_vida(alert_repo, clock):
    alert = _make_alert()
    alert_repo.save_alert(alert)
    service = _service(alert_repo, clock)

    acked = service.acknowledge_alert(tenant_id="tenant_a", alert_id="alt_1", actor_id="operator_1")
    assert acked.status == AlertStatus.ACKNOWLEDGED
    assert acked.acknowledged_at is not None

    resolved = service.resolve_alert(tenant_id="tenant_a", alert_id="alt_1", actor_id="operator_1", reason="resuelto por operador")
    assert resolved.status == AlertStatus.RESOLVED
    assert resolved.resolved_at is not None
    assert resolved.details["resolution_reason"] == "resuelto por operador"

    # Re-resolver es idempotente
    resolved_again = service.resolve_alert(tenant_id="tenant_a", alert_id="alt_1", actor_id="operator_1")
    assert resolved_again.resolved_at == resolved.resolved_at


def test_aislamiento_tenant_alertas(alert_repo, clock):
    alert_repo.save_alert(_make_alert(tenant_id="tenant_a", alert_id="alt_a"))
    alert_repo.save_alert(_make_alert(tenant_id="tenant_b", alert_id="alt_b"))
    service = _service(alert_repo, clock)

    list_a = service.list_alerts(tenant_id="tenant_a")
    list_b = service.list_alerts(tenant_id="tenant_b")
    assert [a.alert_id for a in list_a] == ["alt_a"]
    assert [a.alert_id for a in list_b] == ["alt_b"]

    assert service.alert_repository.get_alert_by_id("tenant_a", "alt_b") is None
    with pytest.raises(Exception):
        service.acknowledge_alert(tenant_id="tenant_a", alert_id="alt_b", actor_id="x")


def test_alert_no_es_accion_automatica(alert_repo, clock):
    """Las alertas informan; no ejecutan cancelaciones ni mutaciones de billing/stop."""
    usage = FlexibleUsageMeteringService(_make_aggregate(total=10, success=2, failed=8))
    subscription = FakeSubscriptionRepository(_fake_subscription(status=SubscriptionStatus.ACTIVE.value, tenant_id="tenant_a"))
    service = _service(alert_repo, clock, usage=usage, subscription=subscription)

    alerts = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    assert any(a.alert_type == OperationalAlertType.HIGH_ERROR_RATE for a in alerts)
    # La suscripción sigue intacta y sin escrituras
    assert subscription.save_calls == 0
    assert subscription.subscription.status.value == SubscriptionStatus.ACTIVE.value
    assert alert_repo.get_active_alert_by_deduplication_key("tenant_a", "high_error_rate:tenant_a") is not None


def test_alerta_high_error_rate(alert_repo, clock):
    usage = FlexibleUsageMeteringService(_make_aggregate(total=10, success=2, failed=8))
    service = _service(alert_repo, clock, usage=usage)
    alerts = service.evaluate_tenant_health_and_alerts(tenant_id="tenant_a")
    err = [a for a in alerts if a.alert_type == OperationalAlertType.HIGH_ERROR_RATE]
    assert len(err) == 1
    assert err[0].severity == AlertSeverity.HIGH


# ---------------------------------------------------------------------------
# Sanitización de identificadores / path traversal
# ---------------------------------------------------------------------------


def test_identificadores_inseguros_rechazados(alert_repo, clock):
    service = _service(alert_repo, clock)
    with pytest.raises(ValueError):
        service.get_tenant_snapshot(tenant_id="../etc/passwd")
    with pytest.raises(ValueError):
        service.evaluate_tenant_health_and_alerts(tenant_id="tenant/../x")
    with pytest.raises(ValueError):
        service.list_alerts(tenant_id="tenant_a", organization_id="../evil")
    with pytest.raises(ValueError):
        service.acknowledge_alert(tenant_id="tenant_a", alert_id="../x", actor_id="op")
