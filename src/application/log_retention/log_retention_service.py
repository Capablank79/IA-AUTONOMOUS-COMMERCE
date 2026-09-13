"""
Servicio de Aplicación para el Ciclo de Vida y Retención de Logs (Hito P.9 — Production / Operations).

Responsabilidades:
1. Coordinar la evaluación y ejecución de retención, rotación y purga para todas las clases de logs.
2. Garantizar separación absoluta de entornos (DEV != STAGING != PROD) y multi-tenant.
3. Modo `--dry-run` no destructivo que proyecta el impacto exacto sin mutaciones físicas.
4. Protección estricta de Audit Trail (K.1) y alertas vivas (P.8 ACTIVE/ACKNOWLEDGED).
5. Idempotencia y seguridad contra fallos parciales.
6. Emisión de eventos de auditoría inmutables (K.1).
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

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
    LogRetentionPolicyError,
    LogRetentionSecurityError,
)
from src.domain.log_retention.ports import (
    LogRetentionStorePort,
    LogRetentionPolicyRegistryPort,
    LogRetentionAuditEmitterPort,
)
from src.domain.log_retention.policies import DefaultRetentionPolicyRegistry

logger = logging.getLogger(__name__)


class LogRetentionApplicationService:
    """
    Servicio de orquestación de políticas de retención de logs y evidencias operacionales.
    """

    def __init__(
        self,
        stores: Sequence[LogRetentionStorePort],
        policy_registry: Optional[LogRetentionPolicyRegistryPort] = None,
        audit_emitter: Optional[LogRetentionAuditEmitterPort] = None,
    ) -> None:
        self._stores = tuple(stores)
        self._policy_registry = policy_registry or DefaultRetentionPolicyRegistry()
        self._audit_emitter = audit_emitter

        # Mapeo de store por clase de retención
        self._store_map: Dict[RetentionClass, LogRetentionStorePort] = {}
        for store in self._stores:
            for d_class in store.supported_classes:
                self._store_map[d_class] = store

    def get_status(
        self,
        environment: ApplicationEnvironment,
        data_class: Optional[RetentionClass] = None,
        tenant_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Sequence[RetentionStatusSummary]:
        """
        Obtiene el resumen de estado de retención para una o todas las clases soportadas.
        """
        env = normalize_environment_name(environment)
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        target_classes = [data_class] if data_class else list(RetentionClass)
        summaries: List[RetentionStatusSummary] = []

        for c in target_classes:
            store = self._store_map.get(c)
            if not store:
                continue
            policy = self._policy_registry.get_policy(env, c, tenant_id=tenant_id)
            summary = store.get_status_summary(policy, now=current_time)
            summaries.append(summary)

        return tuple(summaries)

    def evaluate_policy(
        self,
        policy: RetentionPolicy,
        now: Optional[datetime] = None,
    ) -> Sequence[RetentionDecision]:
        """
        Evalúa las decisiones de retención para una política dada sin mutar estado físico.
        """
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        store = self._store_map.get(policy.data_class)
        if not store:
            raise LogRetentionPolicyError(f"No hay store registrado para la clase {policy.data_class}")

        return store.evaluate_retention(policy, now=current_time)

    def execute_retention(
        self,
        environment: ApplicationEnvironment,
        data_class: RetentionClass,
        dry_run: bool = False,
        retention_days_override: Optional[int] = None,
        batch_size_override: Optional[int] = None,
        tenant_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> RetentionResult:
        """
        Ejecuta o simula el proceso de retención y purga para una clase y entorno específicos.
        """
        env = normalize_environment_name(environment)
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        # Obtener política base
        base_policy = self._policy_registry.get_policy(env, data_class, tenant_id=tenant_id)

        # Aplicar overrides si existen
        retention_days = retention_days_override if retention_days_override is not None else base_policy.retention_days
        batch_size = batch_size_override if batch_size_override is not None else base_policy.purge_batch_size

        policy = RetentionPolicy(
            environment=env,
            data_class=data_class,
            retention_days=retention_days,
            enabled=base_policy.enabled,
            purge_batch_size=batch_size,
            protect_latest=base_policy.protect_latest,
            dry_run=dry_run,
            tenant_id=tenant_id,
            is_audit_protected=base_policy.is_audit_protected,
            max_file_size_bytes=base_policy.max_file_size_bytes,
            backup_count=base_policy.backup_count,
        )

        store = self._store_map.get(data_class)
        if not store:
            raise LogRetentionPolicyError(f"No hay store registrado para la clase {data_class}")

        # 1. Evaluar decisiones
        decisions = store.evaluate_retention(policy, now=current_time)

        # 2. Si es dry_run, construir resultado sin llamar a execute_purge en el store
        if dry_run:
            scanned_count = len(decisions)
            eligible_count = sum(1 for d in decisions if d.action == RetentionAction.PURGE)
            rotated_count = sum(1 for d in decisions if d.action == RetentionAction.ROTATE)
            protected_count = sum(1 for d in decisions if d.action == RetentionAction.PROTECT)
            skipped_count = sum(1 for d in decisions if d.action == RetentionAction.SKIP_CORRUPT)

            result = RetentionResult(
                environment=env,
                data_class=data_class,
                dry_run=True,
                scanned_count=scanned_count,
                eligible_count=eligible_count,
                purged_count=0,
                rotated_count=0,
                protected_count=protected_count,
                skipped_count=skipped_count,
                cutoff_utc=policy.calculate_cutoff(current_time),
                started_at=current_time,
                completed_at=current_time,
                status=RetentionStatus.SUCCESS,
                decisions=tuple(decisions),
                errors=(),
                tenant_id=tenant_id,
            )
            return result

        # 3. Purga real
        started_at = datetime.now(timezone.utc)
        try:
            result = store.execute_purge(policy, decisions)
        except Exception as ex:
            logger.error(f"Error inesperado al purgar logs: {str(ex)}")
            completed_at = datetime.now(timezone.utc)
            result = RetentionResult(
                environment=env,
                data_class=data_class,
                dry_run=False,
                scanned_count=len(decisions),
                eligible_count=sum(1 for d in decisions if d.action == RetentionAction.PURGE),
                purged_count=0,
                rotated_count=0,
                protected_count=sum(1 for d in decisions if d.action == RetentionAction.PROTECT),
                skipped_count=sum(1 for d in decisions if d.action == RetentionAction.SKIP_CORRUPT),
                cutoff_utc=policy.calculate_cutoff(current_time),
                started_at=started_at,
                completed_at=completed_at,
                status=RetentionStatus.FAILED,
                decisions=tuple(decisions),
                errors=(str(ex),),
                tenant_id=tenant_id,
            )

        # 4. Emisión de auditoría
        if self._audit_emitter:
            try:
                event_name = "LOG_RETENTION_PURGE_COMPLETED" if result.is_successful else "LOG_RETENTION_PURGE_FAILED"
                self._audit_emitter.emit_retention_event(
                    event_type=event_name,
                    environment=env,
                    data_class=data_class,
                    result=result,
                )
            except Exception as audit_ex:
                logger.warning(f"No se pudo emitir evento de auditoría de retención: {str(audit_ex)}")

        return result
