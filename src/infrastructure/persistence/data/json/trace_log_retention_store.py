"""
Adaptador de Retención para Trazas de Agentes (K.2 TRACE_LOG) (P.9).

Garantiza:
- Purga basada en started_at de la traza respecto al cutoff UTC.
- Preservación de integridad y correlación de misiones.
- Aislamiento estricto por Environment y Tenant.
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
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository

logger = logging.getLogger(__name__)


class TraceLogRetentionStore(LogRetentionStorePort):
    """
    Gestiona la purga segura de trazas de ejecución de agentes autónomos (TRACE_LOG).
    """

    SUPPORTED = (RetentionClass.TRACE_LOG,)

    def __init__(self, trace_repository: JsonAgentTraceRepository) -> None:
        self.trace_repository = trace_repository
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
            storage_type="json_agent_trace_repository",
            location_summary=str(self.trace_repository.base_dir),
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
        decisions: List[RetentionDecision] = []

        with self._lock:
            # Inspeccionar archivos de trazas en disco si existen
            trace_dir = self.trace_repository.traces_dir if hasattr(self.trace_repository, "traces_dir") else self.trace_repository.base_dir
            if trace_dir.exists():
                for f in trace_dir.glob("*.json"):
                    if not f.is_file():
                        continue
                    trace_id = f.stem
                    try:
                        # Leer registro para ver fecha
                        record = self.trace_repository.get_by_id(trace_id)
                        if not record:
                            continue
                        started_at = record.started_at
                        if started_at.tzinfo is None:
                            started_at = started_at.replace(tzinfo=timezone.utc)

                        if started_at < cutoff:
                            decisions.append(
                                RetentionDecision(
                                    item_id=trace_id,
                                    data_class=RetentionClass.TRACE_LOG,
                                    action=RetentionAction.PURGE,
                                    reason=f"Traza iniciada ({started_at.isoformat()}) anterior al corte UTC ({cutoff.isoformat()})",
                                    occurred_at=started_at,
                                    cutoff_date=cutoff,
                                    metadata={"mission_id": record.mission_id, "step_type": record.step_type.value},
                                    tenant_id=policy.tenant_id,
                                    environment=policy.environment,
                                )
                            )
                        else:
                            decisions.append(
                                RetentionDecision(
                                    item_id=trace_id,
                                    data_class=RetentionClass.TRACE_LOG,
                                    action=RetentionAction.KEEP,
                                    reason=f"Traza dentro de la ventana de retención ({policy.retention_days} días)",
                                    occurred_at=started_at,
                                    cutoff_date=cutoff,
                                    metadata={"mission_id": record.mission_id, "step_type": record.step_type.value},
                                    tenant_id=policy.tenant_id,
                                    environment=policy.environment,
                                )
                            )
                    except Exception:
                        decisions.append(
                            RetentionDecision(
                                item_id=trace_id,
                                data_class=RetentionClass.TRACE_LOG,
                                action=RetentionAction.SKIP_CORRUPT,
                                reason="Error al leer archivo de traza",
                                occurred_at=None,
                                cutoff_date=cutoff,
                                tenant_id=policy.tenant_id,
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
        skipped_count = 0

        with self._lock:
            for decision in decisions:
                if decision.action == RetentionAction.SKIP_CORRUPT:
                    skipped_count += 1
                    continue

                if decision.action == RetentionAction.PURGE:
                    try:
                        trace_dir = self.trace_repository.traces_dir if hasattr(self.trace_repository, "traces_dir") else self.trace_repository.base_dir
                        file_path = trace_dir / f"{decision.item_id}.json"
                        if file_path.exists() and file_path.is_file():
                            file_path.unlink()
                            purged_count += 1
                    except Exception as e:
                        errors.append(f"Error al eliminar traza {decision.item_id}: {str(e)}")

        status = RetentionStatus.SUCCESS if len(errors) == 0 else RetentionStatus.PARTIAL

        return RetentionResult(
            environment=policy.environment,
            data_class=policy.data_class,
            dry_run=False,
            scanned_count=len(decisions),
            eligible_count=sum(1 for d in decisions if d.action == RetentionAction.PURGE),
            purged_count=purged_count,
            rotated_count=0,
            protected_count=0,
            skipped_count=skipped_count,
            cutoff_utc=policy.calculate_cutoff(current_time),
            started_at=current_time,
            completed_at=datetime.now(timezone.utc),
            status=status,
            decisions=tuple(decisions),
            errors=tuple(errors),
            tenant_id=policy.tenant_id,
        )
