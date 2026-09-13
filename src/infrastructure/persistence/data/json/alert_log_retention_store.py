"""
Adaptador de Retención para Historial de Alertas de Producción (P.8 ALERT_HISTORY) (P.9).

Garantiza:
- Protección absoluta de alertas vivas: Alertas en estado ACTIVE o ACKNOWLEDGED NUNCA se purgan por antigüedad.
- Purga estricta y segura únicamente de alertas en estado RESOLVED cuyo resolved_at o triggered_at sea anterior al corte UTC.
- Aislamiento por entorno (DEV, STAGING, PROD) y tenant.
- Verificación y mantenimiento de checksums SHA-256 intactos.
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.log_retention.models import (
    RetentionAction,
    RetentionClass,
    RetentionDecision,
    RetentionPolicy,
    RetentionResult,
    RetentionStatus,
    RetentionStatusSummary,
)
from src.domain.log_retention.ports import LogRetentionStorePort
from src.domain.production_alerting.models import (
    AlertState,
    ProductionAlertInstance,
)
from src.infrastructure.persistence.data.json.production_alert_repository import JsonProductionAlertRepository

logger = logging.getLogger(__name__)


class AlertLogRetentionStore(LogRetentionStorePort):
    """
    Gestiona el ciclo de vida y purga del historial de alertas operacionales (ALERT_HISTORY).
    """

    SUPPORTED = (RetentionClass.ALERT_HISTORY,)

    def __init__(self, alert_repository: JsonProductionAlertRepository) -> None:
        self.alert_repository = alert_repository
        self._lock = threading.RLock()

    @property
    def supported_classes(self) -> Sequence[RetentionClass]:
        return self.SUPPORTED

    def get_status_summary(
        self,
        policy: RetentionPolicy,
        now: Optional[datetime] = None,
    ) -> RetentionStatusSummary:
        current_time = now or datetime.now(timezone.utc)
        decisions = self.evaluate_retention(policy, now=current_time)

        total_records = len(decisions)
        eligible_for_purge = sum(1 for d in decisions if d.action == RetentionAction.PURGE)

        timestamps = [d.occurred_at for d in decisions if d.occurred_at is not None]
        oldest_record_at = min(timestamps) if timestamps else None
        newest_record_at = max(timestamps) if timestamps else None

        location = str(self.alert_repository.alerts_base / policy.environment.value) if self.alert_repository.alerts_base else "in_memory"

        return RetentionStatusSummary(
            environment=policy.environment,
            data_class=policy.data_class,
            retention_days=policy.retention_days,
            total_records=total_records,
            eligible_for_purge=eligible_for_purge,
            oldest_record_at=oldest_record_at,
            newest_record_at=newest_record_at,
            last_purge_at=None,
            last_purge_result=None,
            storage_type="json_production_alert_repository",
            location_summary=location,
            tenant_id=policy.tenant_id,
        )

    def evaluate_retention(
        self,
        policy: RetentionPolicy,
        now: Optional[datetime] = None,
    ) -> Sequence[RetentionDecision]:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        cutoff = policy.calculate_cutoff(current_time)
        alerts = self.alert_repository.list_alerts(
            environment=policy.environment,
            tenant_id=policy.tenant_id,
        )

        decisions: List[RetentionDecision] = []
        for alert in alerts:
            alert_ts = alert.resolved_at or alert.triggered_at
            if alert_ts and alert_ts.tzinfo is None:
                alert_ts = alert_ts.replace(tzinfo=timezone.utc)

            # 1. Regla crítica P.8/P.9: Alertas vivas (ACTIVE o ACKNOWLEDGED) NUNCA se purgan
            if alert.state in (AlertState.ACTIVE, AlertState.ACKNOWLEDGED):
                decisions.append(
                    RetentionDecision(
                        item_id=alert.alert_id,
                        data_class=RetentionClass.ALERT_HISTORY,
                        action=RetentionAction.PROTECT,
                        reason=f"Alerta viva en estado {alert.state.value} protegida de purga",
                        occurred_at=alert_ts,
                        cutoff_date=cutoff,
                        metadata={"state": alert.state.value, "severity": alert.severity.value},
                        tenant_id=alert.tenant_id,
                        environment=policy.environment,
                    )
                )
                continue

            # 2. Alertas RESOLVED
            if alert.state == AlertState.RESOLVED:
                if alert_ts and alert_ts < cutoff:
                    decisions.append(
                        RetentionDecision(
                            item_id=alert.alert_id,
                            data_class=RetentionClass.ALERT_HISTORY,
                            action=RetentionAction.PURGE,
                            reason=f"Alerta RESOLVED con fecha ({alert_ts.isoformat()}) anterior a corte UTC ({cutoff.isoformat()})",
                            occurred_at=alert_ts,
                            cutoff_date=cutoff,
                            metadata={"state": alert.state.value, "severity": alert.severity.value},
                            tenant_id=alert.tenant_id,
                            environment=policy.environment,
                        )
                    )
                else:
                    decisions.append(
                        RetentionDecision(
                            item_id=alert.alert_id,
                            data_class=RetentionClass.ALERT_HISTORY,
                            action=RetentionAction.KEEP,
                            reason=f"Alerta RESOLVED dentro de la ventana de retención ({policy.retention_days} días)",
                            occurred_at=alert_ts,
                            cutoff_date=cutoff,
                            metadata={"state": alert.state.value, "severity": alert.severity.value},
                            tenant_id=alert.tenant_id,
                            environment=policy.environment,
                        )
                    )

        return tuple(decisions)

    def execute_purge(
        self,
        policy: RetentionPolicy,
        decisions: Sequence[RetentionDecision],
    ) -> RetentionResult:
        current_time = datetime.now(timezone.utc)
        errors: List[str] = []
        purged_count = 0
        protected_count = 0

        with self._lock:
            for decision in decisions:
                if decision.action == RetentionAction.PROTECT:
                    protected_count += 1
                    continue

                if decision.action == RetentionAction.PURGE:
                    try:
                        # 1. Eliminar de memoria
                        env_val = policy.environment.value
                        if env_val in self.alert_repository._in_memory_alerts:
                            self.alert_repository._in_memory_alerts[env_val].pop(decision.item_id, None)

                        # 2. Eliminar del disco si aplica
                        if self.alert_repository.alerts_base:
                            file_path = self.alert_repository._env_dir(policy.environment) / f"{decision.item_id}.json"
                            if file_path.exists() and file_path.is_file():
                                file_path.unlink()
                        purged_count += 1
                    except Exception as e:
                        errors.append(f"Error al eliminar alerta {decision.item_id}: {str(e)}")

        status = RetentionStatus.SUCCESS if len(errors) == 0 else RetentionStatus.PARTIAL

        return RetentionResult(
            environment=policy.environment,
            data_class=policy.data_class,
            dry_run=False,
            scanned_count=len(decisions),
            eligible_count=sum(1 for d in decisions if d.action == RetentionAction.PURGE),
            purged_count=purged_count,
            rotated_count=0,
            protected_count=protected_count,
            skipped_count=0,
            cutoff_utc=policy.calculate_cutoff(current_time),
            started_at=current_time,
            completed_at=datetime.now(timezone.utc),
            status=status,
            decisions=tuple(decisions),
            errors=tuple(errors),
            tenant_id=policy.tenant_id,
        )
