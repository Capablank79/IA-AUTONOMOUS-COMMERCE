"""
Adaptador de Retención para Registros y Eventos en PostgreSQL (P.9).

Alineado con:
- P.3 Database Migrations & P.2 Environment Isolation
- 16 tablas SaaS de PostgreSQL (usage_events, operational_alerts, payment_provider_events, etc.)
- DELETE transaccional parametrizado por lotes (purge_batch_size) con límites y filtros multi-tenant.
- Cero uso de TRUNCATE o sentencias destructivas masivas sin scope.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
try:
    import psycopg
except ImportError:
    psycopg = None

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.log_retention.models import (
    RetentionAction,
    RetentionClass,
    RetentionDecision,
    RetentionPolicy,
    RetentionResult,
    RetentionStatus,
    RetentionStatusSummary,
    LogRetentionError,
    LogRetentionSecurityError,
)
from src.domain.log_retention.ports import LogRetentionStorePort
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


# Mapeo de tablas de PostgreSQL por clase de retención
POSTGRES_TABLE_MAP: Mapping[RetentionClass, Tuple[str, str, str]] = {
    # RetentionClass -> (table_name, primary_key, timestamp_column)
    RetentionClass.APPLICATION_LOG: ("usage_events", "usage_event_id", "occurred_at"),
    RetentionClass.ALERT_HISTORY: ("operational_alerts", "alert_id", "triggered_at"),
}


class PostgresLogRetentionStore(LogRetentionStorePort):
    """
    Gestiona la evaluación y purga transaccional acotada de tablas operacionales en PostgreSQL.
    """

    SUPPORTED = (
        RetentionClass.APPLICATION_LOG,
        RetentionClass.ALERT_HISTORY,
    )

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @property
    def supported_classes(self) -> Sequence[RetentionClass]:
        return self.SUPPORTED

    def get_status_summary(
        self,
        policy: RetentionPolicy,
        now: Optional[datetime] = None,
    ) -> RetentionStatusSummary:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        table_info = POSTGRES_TABLE_MAP.get(policy.data_class)
        if not table_info:
            return RetentionStatusSummary(
                environment=policy.environment,
                data_class=policy.data_class,
                retention_days=policy.retention_days,
                total_records=0,
                eligible_for_purge=0,
                oldest_record_at=None,
                newest_record_at=None,
                last_purge_at=None,
                last_purge_result=None,
                storage_type="postgresql",
                location_summary=f"Table not mapped for {policy.data_class.value}",
                tenant_id=policy.tenant_id,
            )

        table_name, pk_col, ts_col = table_info
        cutoff = policy.calculate_cutoff(current_time)

        where_clauses = [f"{ts_col} IS NOT NULL"]
        params: List[Any] = []

        if policy.tenant_id:
            where_clauses.append("tenant_id = %s")
            params.append(policy.tenant_id)

        # Si es alert_history en postgres, solo considerar RESOLVED
        if policy.data_class == RetentionClass.ALERT_HISTORY:
            where_clauses.append("status = %s")
            params.append("RESOLVED")

        where_sql = " AND ".join(where_clauses)

        try:
            with psycopg.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    # 1. Total records
                    cur.execute(f"SELECT COUNT(*), MIN({ts_col}), MAX({ts_col}) FROM {table_name} WHERE {where_sql}", params)
                    row = cur.fetchone()
                    total_records = row[0] if row else 0
                    oldest_ts = row[1] if row and row[1] else None
                    newest_ts = row[2] if row and row[2] else None

                    # 2. Eligible records
                    eligible_where = f"{where_sql} AND {ts_col} < %s"
                    eligible_params = list(params) + [cutoff]
                    cur.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {eligible_where}", eligible_params)
                    eligible_row = cur.fetchone()
                    eligible_records = eligible_row[0] if eligible_row else 0

            return RetentionStatusSummary(
                environment=policy.environment,
                data_class=policy.data_class,
                retention_days=policy.retention_days,
                total_records=total_records,
                eligible_for_purge=eligible_records,
                oldest_record_at=oldest_ts,
                newest_record_at=newest_ts,
                last_purge_at=None,
                last_purge_result=None,
                storage_type="postgresql",
                location_summary=f"Table: {table_name}",
                tenant_id=policy.tenant_id,
            )
        except Exception as e:
            logger.warning(f"Error consultando status en Postgres: {str(e)}")
            return RetentionStatusSummary(
                environment=policy.environment,
                data_class=policy.data_class,
                retention_days=policy.retention_days,
                total_records=0,
                eligible_for_purge=0,
                oldest_record_at=None,
                newest_record_at=None,
                last_purge_at=None,
                last_purge_result=f"Error: {str(e)}",
                storage_type="postgresql",
                location_summary=f"Table: {table_name}",
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

        table_info = POSTGRES_TABLE_MAP.get(policy.data_class)
        if not table_info:
            return ()

        table_name, pk_col, ts_col = table_info
        cutoff = policy.calculate_cutoff(current_time)

        where_clauses = ["1=1"]
        params: List[Any] = []

        if policy.tenant_id:
            where_clauses.append("tenant_id = %s")
            params.append(policy.tenant_id)

        where_sql = " AND ".join(where_clauses)
        decisions: List[RetentionDecision] = []

        try:
            with psycopg.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    if policy.data_class == RetentionClass.ALERT_HISTORY:
                        cur.execute(
                            f"SELECT {pk_col}, {ts_col}, status, tenant_id FROM {table_name} WHERE {where_sql} LIMIT {policy.purge_batch_size * 2}",
                            params,
                        )
                        for r_id, r_ts, status, t_id in cur.fetchall():
                            ts_utc = r_ts if r_ts and r_ts.tzinfo else (r_ts.replace(tzinfo=timezone.utc) if r_ts else None)
                            if status in ("ACTIVE", "ACKNOWLEDGED"):
                                decisions.append(
                                    RetentionDecision(
                                        item_id=str(r_id),
                                        data_class=RetentionClass.ALERT_HISTORY,
                                        action=RetentionAction.PROTECT,
                                        reason=f"Alerta viva en Postgres con status={status}",
                                        occurred_at=ts_utc,
                                        cutoff_date=cutoff,
                                        metadata={"status": status},
                                        tenant_id=t_id,
                                        environment=policy.environment,
                                    )
                                )
                            elif ts_utc and ts_utc < cutoff:
                                decisions.append(
                                    RetentionDecision(
                                        item_id=str(r_id),
                                        data_class=RetentionClass.ALERT_HISTORY,
                                        action=RetentionAction.PURGE,
                                        reason=f"Alerta RESOLVED anterior al cutoff ({cutoff.isoformat()})",
                                        occurred_at=ts_utc,
                                        cutoff_date=cutoff,
                                        metadata={"status": status},
                                        tenant_id=t_id,
                                        environment=policy.environment,
                                    )
                                )
                            else:
                                decisions.append(
                                    RetentionDecision(
                                        item_id=str(r_id),
                                        data_class=RetentionClass.ALERT_HISTORY,
                                        action=RetentionAction.KEEP,
                                        reason=f"Alerta dentro de retención ({policy.retention_days} días)",
                                        occurred_at=ts_utc,
                                        cutoff_date=cutoff,
                                        metadata={"status": status},
                                        tenant_id=t_id,
                                        environment=policy.environment,
                                    )
                                )
                    else:
                        cur.execute(
                            f"SELECT {pk_col}, {ts_col}, tenant_id FROM {table_name} WHERE {where_sql} LIMIT {policy.purge_batch_size * 2}",
                            params,
                        )
                        for r_id, r_ts, t_id in cur.fetchall():
                            ts_utc = r_ts if r_ts and r_ts.tzinfo else (r_ts.replace(tzinfo=timezone.utc) if r_ts else None)
                            if ts_utc and ts_utc < cutoff:
                                decisions.append(
                                    RetentionDecision(
                                        item_id=str(r_id),
                                        data_class=policy.data_class,
                                        action=RetentionAction.PURGE,
                                        reason=f"Registro anterior al cutoff ({cutoff.isoformat()})",
                                        occurred_at=ts_utc,
                                        cutoff_date=cutoff,
                                        tenant_id=t_id,
                                        environment=policy.environment,
                                    )
                                )
                            else:
                                decisions.append(
                                    RetentionDecision(
                                        item_id=str(r_id),
                                        data_class=policy.data_class,
                                        action=RetentionAction.KEEP,
                                        reason=f"Registro dentro de ventana ({policy.retention_days} días)",
                                        occurred_at=ts_utc,
                                        cutoff_date=cutoff,
                                        tenant_id=t_id,
                                        environment=policy.environment,
                                    )
                                )
        except Exception as e:
            logger.warning(f"Error evaluando retención en Postgres: {str(e)}")

        return tuple(decisions)

    def execute_purge(
        self,
        policy: RetentionPolicy,
        decisions: Sequence[RetentionDecision],
    ) -> RetentionResult:
        current_time = datetime.now(timezone.utc)
        cutoff = policy.calculate_cutoff(current_time)
        table_info = POSTGRES_TABLE_MAP.get(policy.data_class)

        if not table_info:
            return RetentionResult(
                environment=policy.environment,
                data_class=policy.data_class,
                dry_run=False,
                scanned_count=0,
                eligible_count=0,
                purged_count=0,
                rotated_count=0,
                protected_count=0,
                skipped_count=0,
                cutoff_utc=cutoff,
                started_at=current_time,
                completed_at=current_time,
                status=RetentionStatus.SKIPPED,
                decisions=(),
                errors=(f"No hay mapeo de tabla para {policy.data_class.value}",),
                tenant_id=policy.tenant_id,
            )

        table_name, pk_col, ts_col = table_info
        purge_ids = [d.item_id for d in decisions if d.action == RetentionAction.PURGE]
        protected_count = sum(1 for d in decisions if d.action == RetentionAction.PROTECT)

        if not purge_ids:
            return RetentionResult(
                environment=policy.environment,
                data_class=policy.data_class,
                dry_run=False,
                scanned_count=len(decisions),
                eligible_count=0,
                purged_count=0,
                rotated_count=0,
                protected_count=protected_count,
                skipped_count=0,
                cutoff_utc=cutoff,
                started_at=current_time,
                completed_at=current_time,
                status=RetentionStatus.SUCCESS,
                decisions=tuple(decisions),
                errors=(),
                tenant_id=policy.tenant_id,
            )

        errors: List[str] = []
        purged_count = 0

        # Purga por lotes acotados transaccionales (batch bounded)
        batch_size = policy.purge_batch_size
        try:
            with psycopg.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    for i in range(0, len(purge_ids), batch_size):
                        batch = purge_ids[i : i + batch_size]
                        placeholders = ", ".join(["%s"] * len(batch))
                        delete_sql = f"DELETE FROM {table_name} WHERE {pk_col} IN ({placeholders})"
                        cur.execute(delete_sql, batch)
                        purged_count += cur.rowcount
                conn.commit()
        except Exception as e:
            errors.append(f"Error ejecutando DELETE en batch en {table_name}: {str(e)}")

        status = RetentionStatus.SUCCESS if len(errors) == 0 else RetentionStatus.FAILED

        return RetentionResult(
            environment=policy.environment,
            data_class=policy.data_class,
            dry_run=False,
            scanned_count=len(decisions),
            eligible_count=len(purge_ids),
            purged_count=purged_count,
            rotated_count=0,
            protected_count=protected_count,
            skipped_count=0,
            cutoff_utc=cutoff,
            started_at=current_time,
            completed_at=datetime.now(timezone.utc),
            status=status,
            decisions=tuple(decisions),
            errors=tuple(errors),
            tenant_id=policy.tenant_id,
        )
