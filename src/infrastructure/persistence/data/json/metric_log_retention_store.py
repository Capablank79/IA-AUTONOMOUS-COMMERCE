"""
Adaptador de Retención para Métricas Operacionales (P.7 MONITORING_SAMPLE) (P.9).

Garantiza:
- Purga basada en timestamp UTC y ventanas de retención.
- Aislamiento estricto por ApplicationEnvironment (DEV, STAGING, PROD).
- Idempotencia y atomicidad en reescritura de metrics.json.
- Protección de ventanas de tiempo mínimas operativas.
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
from src.domain.monitoring.models import MetricSample, MetricType, MonitoringScope
from src.infrastructure.persistence.data.json.metric_repository import JsonMetricRepository

logger = logging.getLogger(__name__)


class MetricLogRetentionStore(LogRetentionStorePort):
    """
    Gestiona la purga segura de muestras de métricas operacionales (MONITORING_SAMPLE).
    """

    SUPPORTED = (RetentionClass.MONITORING_SAMPLE,)

    def __init__(self, metric_repository: JsonMetricRepository) -> None:
        self.metric_repository = metric_repository
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

        loc = (
            str(self.metric_repository._get_env_file(policy.environment))
            if hasattr(self.metric_repository, "_get_env_file")
            else "memory"
        )
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
            storage_type="json_metric_repository",
            location_summary=loc,
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
        samples = self.metric_repository.get_samples(
            environment=policy.environment,
            tenant_id=policy.tenant_id,
        )

        decisions: List[RetentionDecision] = []
        for idx, sample in enumerate(samples):
            item_id = f"sample_{sample.metric_type.value}_{idx}_{int(sample.timestamp.timestamp())}"
            sample_ts = sample.timestamp
            if sample_ts.tzinfo is None:
                sample_ts = sample_ts.replace(tzinfo=timezone.utc)

            if sample_ts < cutoff:
                decisions.append(
                    RetentionDecision(
                        item_id=item_id,
                        data_class=RetentionClass.MONITORING_SAMPLE,
                        action=RetentionAction.PURGE,
                        reason=f"Muestra anterior al cutoff UTC ({cutoff.isoformat()})",
                        occurred_at=sample_ts,
                        cutoff_date=cutoff,
                        metadata={"metric_type": sample.metric_type.value, "value": str(sample.value)},
                        tenant_id=sample.tenant_id,
                        environment=policy.environment,
                    )
                )
            else:
                decisions.append(
                    RetentionDecision(
                        item_id=item_id,
                        data_class=RetentionClass.MONITORING_SAMPLE,
                        action=RetentionAction.KEEP,
                        reason=f"Muestra dentro de ventana ({policy.retention_days} días)",
                        occurred_at=sample_ts,
                        cutoff_date=cutoff,
                        metadata={"metric_type": sample.metric_type.value, "value": str(sample.value)},
                        tenant_id=sample.tenant_id,
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
        # Si las decisiones tienen un cutoff_date definido, reutilizarlo para mantener coherencia con la evaluación
        cutoff = decisions[0].cutoff_date if decisions and decisions[0].cutoff_date else policy.calculate_cutoff(current_time)
        errors: List[str] = []

        # Crear conjunto de item_ids a purgar según las decisiones
        purge_ids = {d.item_id for d in decisions if d.action == RetentionAction.PURGE}

        with self._lock:
            # Obtener todas las muestras actuales del entorno
            all_samples = self.metric_repository.get_samples(environment=policy.environment)
            remaining_samples: List[MetricSample] = []
            purged_count = 0

            for idx, sample in enumerate(all_samples):
                item_id = f"sample_{sample.metric_type.value}_{idx}_{int(sample.timestamp.timestamp())}"
                if item_id in purge_ids:
                    purged_count += 1
                else:
                    remaining_samples.append(sample)

            # Persistir atómicamente la lista depurada
            try:
                if hasattr(self.metric_repository, "_write_samples"):
                    self.metric_repository._write_samples(policy.environment, remaining_samples)
                elif hasattr(self.metric_repository, "_samples_by_env"):
                    # InMemory fallback
                    self.metric_repository._samples_by_env[policy.environment] = list(remaining_samples)
            except Exception as e:
                errors.append(f"Error al persistir muestras tras purga: {str(e)}")

        status = RetentionStatus.SUCCESS if len(errors) == 0 else RetentionStatus.FAILED

        return RetentionResult(
            environment=policy.environment,
            data_class=policy.data_class,
            dry_run=False,
            scanned_count=len(decisions),
            eligible_count=sum(1 for d in decisions if d.action == RetentionAction.PURGE),
            purged_count=purged_count if status == RetentionStatus.SUCCESS else 0,
            rotated_count=0,
            protected_count=0,
            skipped_count=0,
            cutoff_utc=cutoff,
            started_at=current_time,
            completed_at=datetime.now(timezone.utc),
            status=status,
            decisions=tuple(decisions),
            errors=tuple(errors),
            tenant_id=policy.tenant_id,
        )
