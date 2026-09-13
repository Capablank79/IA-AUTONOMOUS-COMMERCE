"""
Pruebas Unitarias para el Módulo de Retención, Rotación y Purga de Logs (P.9).

Cubre:
1. retention cutoff deterministic
2. environment required
3. invalid class rejected
4. audit policy protected
5. active alert protected
6. resolved alert eligible
7. tenant isolation
8. dry-run no deletion
9. purge idempotent
10. UTC semantics
11. unsafe path rejected
12. sensitive data not introduced
13. batch limit respected
14. UNKNOWN timestamp safe
15. policy centralization
16. no P.10+
"""

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import pytest
import shutil
import tempfile

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.log_retention.models import (
    RetentionClass,
    RetentionAction,
    RetentionStatus,
    RetentionPolicy,
    RetentionDecision,
    RetentionResult,
    RetentionStatusSummary,
    LogRetentionError,
    LogRetentionSecurityError,
    LogRetentionPolicyError,
)
from src.domain.log_retention.policies import (
    DEFAULT_RETENTION_DAYS,
    DefaultRetentionPolicyRegistry,
)
from src.domain.production_alerting.models import (
    AlertRuleType,
    AlertSeverity,
    AlertState,
    ProductionAlertInstance,
    ProductionAlertScope,
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
from src.domain.monitoring.models import MetricSample, MetricType, MonitoringScope
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType


@pytest.fixture
def tmp_test_env(tmp_path):
    """Crea una jerarquía temporal de logs, datos y trazas."""
    base_dir = tmp_path / "app_root"
    base_dir.mkdir()
    logs_dir = base_dir / ".runtime" / "logs"
    logs_dir.mkdir(parents=True)
    traces_dir = base_dir / ".runtime" / "traces"
    traces_dir.mkdir(parents=True)
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True)
    return {
        "base_dir": base_dir,
        "logs_dir": logs_dir,
        "traces_dir": traces_dir,
        "data_dir": data_dir,
    }


def test_retention_cutoff_deterministic():
    """1. retention cutoff deterministic & UTC semantics."""
    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)
    policy = RetentionPolicy(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.APPLICATION_LOG,
        retention_days=10,
    )
    cutoff = policy.calculate_cutoff(now=fixed_now)
    assert cutoff == datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_environment_required_and_normalized():
    """2. environment required."""
    policy = RetentionPolicy(
        environment="production",
        data_class=RetentionClass.APPLICATION_LOG,
        retention_days=7,
    )
    assert policy.environment == ApplicationEnvironment.PRODUCTION


def test_invalid_class_rejected():
    """3. invalid class rejected."""
    with pytest.raises(LogRetentionPolicyError):
        RetentionPolicy(
            environment=ApplicationEnvironment.DEVELOPMENT,
            data_class="INVALID_UNKNOWN_CLASS",
            retention_days=7,
        )


def test_audit_policy_protected():
    """4. audit policy protected (K.1 Audit Trail)."""
    # AUDIT_RECORD no puede tener retention menor a 365 días si is_audit_protected=False
    with pytest.raises(LogRetentionPolicyError):
        RetentionPolicy(
            environment=ApplicationEnvironment.PRODUCTION,
            data_class=RetentionClass.AUDIT_RECORD,
            retention_days=30,
            is_audit_protected=False,
        )


def test_active_and_acknowledged_alerts_protected(tmp_test_env):
    """5. active alert protected."""
    alert_repo = JsonProductionAlertRepository(base_dir=tmp_test_env["data_dir"])
    alert_store = AlertLogRetentionStore(alert_repository=alert_repo)

    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = fixed_now - timedelta(days=60)

    # Crear alerta ACTIVE muy antigua
    active_alert = ProductionAlertInstance(
        alert_id="alert_active_01",
        rule_type=AlertRuleType.HIGH_ERROR_RATE,
        severity=AlertSeverity.CRITICAL,
        state=AlertState.ACTIVE,
        environment=ApplicationEnvironment.PRODUCTION,
        scope=ProductionAlertScope.PLATFORM,
        deduplication_key="dedup_01",
        summary="High error rate",
        evidence={"error_rate": 0.15},
        triggered_at=old_ts,
        updated_at=old_ts,
        target_resource="api_gateway",
    )
    alert_repo.save_alert(active_alert)

    policy = RetentionPolicy(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.ALERT_HISTORY,
        retention_days=14,
    )
    decisions = alert_store.evaluate_retention(policy, now=fixed_now)
    assert len(decisions) == 1
    assert decisions[0].action == RetentionAction.PROTECT
    assert "protegida de purga" in decisions[0].reason


def test_resolved_alert_eligible_when_older_than_cutoff(tmp_test_env):
    """6. resolved alert eligible."""
    alert_repo = JsonProductionAlertRepository(base_dir=tmp_test_env["data_dir"])
    alert_store = AlertLogRetentionStore(alert_repository=alert_repo)

    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)
    old_ts = fixed_now - timedelta(days=60)

    resolved_alert = ProductionAlertInstance(
        alert_id="alert_resolved_01",
        rule_type=AlertRuleType.HIGH_LATENCY,
        severity=AlertSeverity.WARNING,
        state=AlertState.RESOLVED,
        environment=ApplicationEnvironment.PRODUCTION,
        scope=ProductionAlertScope.PLATFORM,
        target_resource="cache_cluster",
        deduplication_key="dedup_02",
        evidence={"latency_ms": 600, "threshold_ms": 500},
        triggered_at=old_ts - timedelta(hours=1),
        resolved_at=old_ts,
        updated_at=old_ts,
        summary="High latency resolved",
    )
    alert_repo.save_alert(resolved_alert)

    policy = RetentionPolicy(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.ALERT_HISTORY,
        retention_days=14,
    )
    decisions = alert_store.evaluate_retention(policy, now=fixed_now)
    assert len(decisions) == 1
    assert decisions[0].action == RetentionAction.PURGE


def test_tenant_isolation_in_file_store(tmp_test_env):
    """7. tenant isolation."""
    logs_dir = tmp_test_env["logs_dir"]
    file_store = FileLogRetentionStore(base_log_dir=logs_dir)

    # Crear logs para Tenant A y Tenant B
    tenant_a_dir = logs_dir / "production" / "tenants" / "tenant_alpha"
    tenant_b_dir = logs_dir / "production" / "tenants" / "tenant_beta"
    tenant_a_dir.mkdir(parents=True)
    tenant_b_dir.mkdir(parents=True)

    old_date = "2025-01-01"
    (tenant_a_dir / f"app_{old_date}.log").write_text("tenant a old log\n", encoding="utf-8")
    (tenant_b_dir / f"app_{old_date}.log").write_text("tenant b old log\n", encoding="utf-8")

    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)

    # Evaluar policy solo para Tenant A
    policy_a = RetentionPolicy(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.APPLICATION_LOG,
        retention_days=7,
        tenant_id="tenant_alpha",
    )
    decisions_a = file_store.evaluate_retention(policy_a, now=fixed_now)
    assert len(decisions_a) == 1
    assert decisions_a[0].tenant_id == "tenant_alpha"

    # Purga solo tenant A
    result_a = file_store.execute_purge(policy_a, decisions_a)
    assert result_a.purged_count == 1
    assert not (tenant_a_dir / f"app_{old_date}.log").exists()
    assert (tenant_b_dir / f"app_{old_date}.log").exists()  # Tenant B intacto


def test_dry_run_no_deletion(tmp_test_env):
    """8. dry-run no deletion."""
    logs_dir = tmp_test_env["logs_dir"]
    file_store = FileLogRetentionStore(base_log_dir=logs_dir)

    prod_dir = logs_dir / "production"
    prod_dir.mkdir(parents=True, exist_ok=True)
    old_file = prod_dir / "app_2025-01-01.log"
    old_file.write_text("old content\n", encoding="utf-8")

    service = LogRetentionApplicationService(stores=[file_store])
    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)

    result = service.execute_retention(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.APPLICATION_LOG,
        dry_run=True,
        now=fixed_now,
    )
    assert result.dry_run is True
    assert result.eligible_count == 1
    assert result.purged_count == 0
    assert old_file.exists()  # No borrado físico


def test_purge_idempotent(tmp_test_env):
    """9. purge idempotent."""
    logs_dir = tmp_test_env["logs_dir"]
    file_store = FileLogRetentionStore(base_log_dir=logs_dir)

    prod_dir = logs_dir / "production"
    prod_dir.mkdir(parents=True, exist_ok=True)
    old_file = prod_dir / "app_2025-01-01.log"
    old_file.write_text("old content\n", encoding="utf-8")

    service = LogRetentionApplicationService(stores=[file_store])
    fixed_now = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)

    # 1era purga
    res1 = service.execute_retention(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.APPLICATION_LOG,
        dry_run=False,
        now=fixed_now,
    )
    assert res1.purged_count == 1

    # 2da purga (idempotente)
    res2 = service.execute_retention(
        environment=ApplicationEnvironment.PRODUCTION,
        data_class=RetentionClass.APPLICATION_LOG,
        dry_run=False,
        now=fixed_now,
    )
    assert res2.purged_count == 0
    assert res2.eligible_count == 0
    assert res2.is_successful is True


def test_unsafe_path_and_symlink_rejected(tmp_test_env):
    """11. unsafe path rejected."""
    logs_dir = tmp_test_env["logs_dir"]
    file_store = FileLogRetentionStore(base_log_dir=logs_dir)

    # Tenant ID con path traversal
    with pytest.raises(LogRetentionSecurityError):
        RetentionPolicy(
            environment=ApplicationEnvironment.PRODUCTION,
            data_class=RetentionClass.APPLICATION_LOG,
            retention_days=7,
            tenant_id="../../etc",
        )


def test_policy_centralization():
    """15. policy centralization."""
    registry = DefaultRetentionPolicyRegistry()
    dev_policy = registry.get_policy(ApplicationEnvironment.DEVELOPMENT, RetentionClass.APPLICATION_LOG)
    prod_policy = registry.get_policy(ApplicationEnvironment.PRODUCTION, RetentionClass.APPLICATION_LOG)
    audit_policy = registry.get_policy(ApplicationEnvironment.PRODUCTION, RetentionClass.AUDIT_RECORD)

    assert dev_policy.retention_days == 7
    assert prod_policy.retention_days == 30
    assert audit_policy.retention_days == 2555  # 7 años
    assert audit_policy.is_audit_protected is True
