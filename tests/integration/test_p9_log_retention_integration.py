"""
Pruebas de Integración y Validación de Ciclo de Vida para Retención de Logs (P.9).

Escenarios:
A. old app logs eligible.
B. recent logs preserved.
C. dry-run reports but deletes nothing.
D. purge removes only eligible records.
E. Tenant A purge leaves B intact.
F. DEV purge leaves PROD intact.
G. active alert evidence preserved.
H. audit records protected.
I. second purge idempotent.
J. purge failure safely reported.
"""

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import pytest
import shutil

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.log_retention.models import (
    RetentionClass,
    RetentionAction,
    RetentionStatus,
    RetentionPolicy,
    RetentionDecision,
    RetentionResult,
    RetentionStatusSummary,
)
from src.domain.production_alerting.models import (
    AlertRuleType,
    AlertSeverity,
    AlertState,
    ProductionAlertInstance,
    ProductionAlertScope,
)
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.monitoring.models import (
    MetricSample,
    MetricType,
    MonitoringScope,
)
from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
)
from src.infrastructure.persistence.data.json.file_log_retention_store import FileLogRetentionStore
from src.infrastructure.persistence.data.json.metric_log_retention_store import MetricLogRetentionStore
from src.infrastructure.persistence.data.json.alert_log_retention_store import AlertLogRetentionStore
from src.infrastructure.persistence.data.json.trace_log_retention_store import TraceLogRetentionStore
from src.infrastructure.persistence.data.json.audit_log_retention_store import AuditLogRetentionStore
from src.infrastructure.persistence.data.json.metric_repository import JsonMetricRepository
from src.infrastructure.persistence.data.json.production_alert_repository import JsonProductionAlertRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.log_retention.log_retention_service import LogRetentionApplicationService


@pytest.fixture
def integrated_env(tmp_path):
    root = tmp_path / "system_root"
    root.mkdir()
    logs_dir = root / ".runtime" / "logs"
    logs_dir.mkdir(parents=True)
    traces_dir = root / ".runtime" / "traces"
    traces_dir.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir(parents=True)

    metric_repo = JsonMetricRepository(base_dir=data_dir)
    alert_repo = JsonProductionAlertRepository(base_dir=data_dir)
    trace_repo = JsonAgentTraceRepository(base_dir=traces_dir)
    audit_repo = JsonAuditRepository(storage_dir=data_dir / "audit")

    file_store = FileLogRetentionStore(base_log_dir=logs_dir)
    metric_store = MetricLogRetentionStore(metric_repository=metric_repo)
    alert_store = AlertLogRetentionStore(alert_repository=alert_repo)
    trace_store = TraceLogRetentionStore(trace_repository=trace_repo)
    audit_store = AuditLogRetentionStore(audit_repository=audit_repo)

    service = LogRetentionApplicationService(
        stores=[file_store, metric_store, alert_store, trace_store, audit_store]
    )

    return {
        "service": service,
        "logs_dir": logs_dir,
        "traces_dir": traces_dir,
        "data_dir": data_dir,
        "metric_repo": metric_repo,
        "alert_repo": alert_repo,
        "trace_repo": trace_repo,
        "audit_repo": audit_repo,
    }


def test_scenario_a_and_b_old_logs_eligible_recent_preserved(integrated_env):
    """Escenarios A y B: logs antiguos son elegibles y logs recientes se preservan."""
    service = integrated_env["service"]
    logs_dir = integrated_env["logs_dir"]
    dev_dir = logs_dir / "development"
    dev_dir.mkdir(parents=True, exist_ok=True)

    # 1 log antiguo (hace 20 días) y 1 log reciente (hace 2 días)
    old_log = dev_dir / "app_2026-03-05.log"
    recent_log = dev_dir / "app_2026-03-23.log"
    old_log.write_text("old log line\n", encoding="utf-8")
    recent_log.write_text("recent log line\n", encoding="utf-8")

    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)

    # DEV default retention es 7 días -> cutoff = 2026-03-18
    result = service.execute_retention(
        environment=ApplicationEnvironment.DEVELOPMENT,
        data_class=RetentionClass.APPLICATION_LOG,
        dry_run=False,
        now=fixed_now,
    )

    assert result.scanned_count == 2
    assert result.eligible_count == 1
    assert result.purged_count == 1
    assert not old_log.exists()
    assert recent_log.exists()


def test_scenario_c_and_d_dry_run_and_selective_purge(integrated_env):
    """Escenarios C y D: dry-run simula sin mutación y purge remueve solo lo elegible."""
    service = integrated_env["service"]
    metric_repo = integrated_env["metric_repo"]

    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)
    old_sample_ts = fixed_now - timedelta(days=10)
    recent_sample_ts = fixed_now - timedelta(hours=2)

    # Grabar 1 sample antiguo y 1 sample reciente en DEV (retention default 3 días)
    metric_repo.record_samples([
        MetricSample(
            metric_type=MetricType.REQUEST_COUNT,
            value=100,
            timestamp=old_sample_ts,
            environment=ApplicationEnvironment.DEVELOPMENT,
        ),
        MetricSample(
            metric_type=MetricType.REQUEST_COUNT,
            value=200,
            timestamp=recent_sample_ts,
            environment=ApplicationEnvironment.DEVELOPMENT,
        ),
    ])

    # C. Dry-run
    dry_res = service.execute_retention(
        environment=ApplicationEnvironment.DEVELOPMENT,
        data_class=RetentionClass.MONITORING_SAMPLE,
        dry_run=True,
        now=fixed_now,
    )
    assert dry_res.dry_run is True
    assert dry_res.eligible_count == 1
    assert dry_res.purged_count == 0
    # Comprobar que en disco siguen las 2 muestras
    assert len(metric_repo.get_samples(ApplicationEnvironment.DEVELOPMENT)) == 2

    # D. Purge real
    purge_res = service.execute_retention(
        environment=ApplicationEnvironment.DEVELOPMENT,
        data_class=RetentionClass.MONITORING_SAMPLE,
        dry_run=False,
        now=fixed_now,
    )
    assert purge_res.purged_count == 1
    remaining = metric_repo.get_samples(ApplicationEnvironment.DEVELOPMENT)
    assert len(remaining) == 1
    assert remaining[0].value == 200


def test_scenario_e_and_f_isolation_dev_prod_and_tenants(integrated_env):
    """Escenarios E y F: DEV purge no toca PROD y Tenant A no toca Tenant B."""
    service = integrated_env["service"]
    logs_dir = integrated_env["logs_dir"]

    dev_log = logs_dir / "development" / "app_2025-01-01.log"
    prod_log = logs_dir / "production" / "app_2025-01-01.log"
    dev_log.parent.mkdir(parents=True, exist_ok=True)
    prod_log.parent.mkdir(parents=True, exist_ok=True)
    dev_log.write_text("dev old", encoding="utf-8")
    prod_log.write_text("prod old", encoding="utf-8")

    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)

    # Purga solo en DEV
    service.execute_retention(
        environment=ApplicationEnvironment.DEVELOPMENT,
        data_class=RetentionClass.APPLICATION_LOG,
        dry_run=False,
        now=fixed_now,
    )

    assert not dev_log.exists()
    assert prod_log.exists()  # PROD completamente intacto


def test_scenario_g_and_h_active_alerts_and_audit_protected(integrated_env):
    """Escenarios G y H: Alertas activas y Audit records protegidos incondicionalmente."""
    service = integrated_env["service"]
    alert_repo = integrated_env["alert_repo"]
    audit_repo = integrated_env["audit_repo"]

    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)
    very_old = fixed_now - timedelta(days=500)

    # Alerta activa muy antigua
    alert_repo.save_alert(
        ProductionAlertInstance(
            alert_id="active_old_alert",
            rule_type=AlertRuleType.HIGH_ERROR_RATE,
            severity=AlertSeverity.CRITICAL,
            state=AlertState.ACTIVE,
            environment=ApplicationEnvironment.PRODUCTION,
            scope=ProductionAlertScope.PLATFORM,
            target_resource="payment_gateway",
            deduplication_key="dedup_crit_01",
            evidence={"error_rate": 0.45},
            triggered_at=very_old,
            resolved_at=None,
            updated_at=very_old,
            summary="Payment breaker tripped",
        )
    )

    # Audit Record antiguo
    audit_repo.append(
        AuditRecord(
            audit_id="audit_crit_01",
            record_type=AuditRecordType.ACTION_EXECUTED,
            occurred_at=very_old,
            actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="admin"),
            subject_type="TENANT",
            subject_id="tenant_01",
            action_or_operation="TENANT_PROVISION",
            status="SUCCESS",
            correlation_id="corr_01",
        )
    )

    # 1. Purga de alertas en PROD
    alert_res = service.execute_retention(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.ALERT_HISTORY,
        dry_run=False,
        now=fixed_now,
    )
    assert alert_res.protected_count == 1
    assert alert_res.purged_count == 0
    assert alert_repo.get_alert_by_id(ApplicationEnvironment.PRODUCTION, "active_old_alert") is not None

    # 2. Purga de auditoría en PROD
    audit_res = service.execute_retention(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.AUDIT_RECORD,
        dry_run=False,
        now=fixed_now,
    )
    assert audit_res.protected_count == 1
    assert audit_res.purged_count == 0
    assert audit_repo.get_by_id("audit_crit_01") is not None


def test_scenario_i_and_j_idempotency_and_trace_retention(integrated_env):
    """Escenarios I y J: Segunda purga idempotente y retención segura de TRACE_LOG."""
    service = integrated_env["service"]
    trace_repo = integrated_env["trace_repo"]

    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = fixed_now - timedelta(days=20)
    recent_ts = fixed_now - timedelta(days=1)

    trace_old = AgentTraceRecord(
        trace_id="trace_old_01",
        component_name="MARKET_ANALYST",
        execution_id="exec_01",
        step_number=1,
        step_type=StepType.OBSERVE,
        operation="fetch_market_prices",
        started_at=old_ts,
        completed_at=old_ts + timedelta(seconds=2),
        status=TraceStatus.SUCCESS,
        mission_id="mission_01",
        correlation_id="corr_01",
    )

    trace_recent = AgentTraceRecord(
        trace_id="trace_recent_01",
        component_name="MARKET_ANALYST",
        execution_id="exec_02",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="query_tools",
        started_at=recent_ts,
        completed_at=recent_ts + timedelta(seconds=1),
        status=TraceStatus.SUCCESS,
        mission_id="mission_02",
        correlation_id="corr_02",
    )

    trace_repo.append(trace_old)
    trace_repo.append(trace_recent)

    # 1. Primera purga en DEV (default 7 días para TRACE_LOG)
    res1 = service.execute_retention(
        environment=ApplicationEnvironment.DEVELOPMENT,
        data_class=RetentionClass.TRACE_LOG,
        dry_run=False,
        now=fixed_now,
    )
    assert res1.purged_count == 1
    assert trace_repo.get_by_id("trace_old_01") is None
    assert trace_repo.get_by_id("trace_recent_01") is not None

    # 2. Segunda purga (Idempotente: 0 purged)
    res2 = service.execute_retention(
        environment=ApplicationEnvironment.DEVELOPMENT,
        data_class=RetentionClass.TRACE_LOG,
        dry_run=False,
        now=fixed_now,
    )
    assert res2.purged_count == 0
    assert res2.eligible_count == 0
    assert res2.is_successful is True
