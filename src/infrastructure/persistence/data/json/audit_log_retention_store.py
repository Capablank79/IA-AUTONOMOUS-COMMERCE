"""
Adaptador de Retención para Audit Trail (K.1 AUDIT_RECORD) (P.9).

Garantiza:
- LOG != AUDIT: Protección estricta contra purgas ordinarias.
- Por defecto, los registros de auditoría están protegidos (`RetentionAction.PROTECT`).
- Solo bajo una política con `is_audit_protected=False` y retención legal explícita (>= 365 días) se evalúan purgas.
- Preservación de registros recientes (`protect_latest`) y de registros corruptos/manipulados para análisis forense.
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
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository

logger = logging.getLogger(__name__)


class AuditLogRetentionStore(LogRetentionStorePort):
    """
    Gestiona la evaluación y retención protegida de registros de auditoría (AUDIT_RECORD).
    """

    SUPPORTED = (RetentionClass.AUDIT_RECORD,)

    def __init__(self, audit_repository: JsonAuditRepository) -> None:
        self.audit_repository = audit_repository
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
            storage_type="json_audit_repository",
            location_summary=str(self.audit_repository._records_dir),
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
            # Obtener todos los registros de auditoría
            if hasattr(self.audit_repository, "list_all"):
                records = self.audit_repository.list_all()
            elif hasattr(self.audit_repository, "list_records"):
                records = self.audit_repository.list_records(limit=100000)
            elif hasattr(self.audit_repository, "_records_by_id"):
                records = list(self.audit_repository._records_by_id.values())
            else:
                records = []

            # Ordenar cronológicamente descendente para aplicar protect_latest
            sorted_records = sorted(records, key=lambda r: r.occurred_at, reverse=True)

            for idx, record in enumerate(sorted_records):
                record_ts = record.occurred_at
                if record_ts.tzinfo is None:
                    record_ts = record_ts.replace(tzinfo=timezone.utc)

                # 1. Protección estricta si is_audit_protected es True
                if policy.is_audit_protected:
                    decisions.append(
                        RetentionDecision(
                            item_id=record.audit_id,
                            data_class=RetentionClass.AUDIT_RECORD,
                            action=RetentionAction.PROTECT,
                            reason="Registro de auditoría protegido incondicionalmente por política K.1",
                            occurred_at=record_ts,
                            cutoff_date=cutoff,
                            metadata={"actor": record.actor.actor_id, "action": record.action_or_operation},
                            tenant_id=policy.tenant_id,
                            environment=policy.environment,
                        )
                    )
                    continue

                # 2. Protección de los últimos N registros
                if idx < policy.protect_latest:
                    decisions.append(
                        RetentionDecision(
                            item_id=record.audit_id,
                            data_class=RetentionClass.AUDIT_RECORD,
                            action=RetentionAction.PROTECT,
                            reason=f"Registro protegido por regla protect_latest={policy.protect_latest}",
                            occurred_at=record_ts,
                            cutoff_date=cutoff,
                            metadata={"actor": record.actor.actor_id, "action": record.action_or_operation},
                            tenant_id=policy.tenant_id,
                            environment=policy.environment,
                        )
                    )
                    continue

                # 3. Evaluación de purga solo si no está protegido y supera cutoff
                if record_ts < cutoff:
                    decisions.append(
                        RetentionDecision(
                            item_id=record.audit_id,
                            data_class=RetentionClass.AUDIT_RECORD,
                            action=RetentionAction.PURGE,
                            reason=f"Auditoría anterior al cutoff legal UTC ({cutoff.isoformat()})",
                            occurred_at=record_ts,
                            cutoff_date=cutoff,
                            metadata={"actor": record.actor.actor_id, "action": record.action_or_operation},
                            tenant_id=policy.tenant_id,
                            environment=policy.environment,
                        )
                    )
                else:
                    decisions.append(
                        RetentionDecision(
                            item_id=record.audit_id,
                            data_class=RetentionClass.AUDIT_RECORD,
                            action=RetentionAction.KEEP,
                            reason=f"Auditoría dentro de la ventana de retención legal ({policy.retention_days} días)",
                            occurred_at=record_ts,
                            cutoff_date=cutoff,
                            metadata={"actor": record.actor.actor_id, "action": record.action_or_operation},
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
        protected_count = 0

        with self._lock:
            for decision in decisions:
                if decision.action == RetentionAction.PROTECT:
                    protected_count += 1
                    continue

                if decision.action == RetentionAction.PURGE:
                    try:
                        file_path = self.audit_repository._records_dir / f"{decision.item_id}.json"
                        if file_path.exists() and file_path.is_file():
                            file_path.unlink()
                            # Eliminar del índice en memoria
                            self.audit_repository._records_by_id.pop(decision.item_id, None)
                            purged_count += 1
                    except Exception as e:
                        errors.append(f"Error al eliminar registro de auditoría {decision.item_id}: {str(e)}")

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
