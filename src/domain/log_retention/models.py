"""
Modelos de Dominio para Retención, Rotación y Purga Segura de Logs y Evidencia Operativa (P.9).

Principios P.9:
1. Operational Log != Audit Evidence: K.1 Audit Trail es inmutable y está protegido contra purgas genéricas.
2. Tipado fuerte e inmutabilidad (frozen=True, Enums, Tuples).
3. Aislamiento absoluto por ApplicationEnvironment (P.2) y Tenant (O.13).
4. Determinismo temporal: Fechas estrictamente en UTC consciente de zona horaria.
5. Protección de alertas no resueltas: Alertas ACTIVE/ACKNOWLEDGED de P.8 NUNCA se purgan por antigüedad.
6. Safe rotation y safe filesystem paths (anti path-traversal, no-symlink fuera de log root).
7. Transaccionalidad, batches acotados e idempotencia estricta en purgas.
8. Sanitización de seguridad: Cero volcado de secretos o credenciales en decisiones o resultados.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from src.domain.deployment.models import (
    ApplicationEnvironment,
    normalize_environment_name,
)
from src.domain.security.models import validate_safe_identifier


class RetentionClass(str, Enum):
    """Clases canónicas de datos operacionales y evidencias bajo el ciclo de vida de retención."""
    APPLICATION_LOG = "APPLICATION_LOG"
    ACCESS_LOG = "ACCESS_LOG"
    ERROR_LOG = "ERROR_LOG"
    TRACE_LOG = "TRACE_LOG"
    MONITORING_SAMPLE = "MONITORING_SAMPLE"
    ALERT_HISTORY = "ALERT_HISTORY"
    AUDIT_RECORD = "AUDIT_RECORD"


class RetentionAction(str, Enum):
    """Acciones resultantes de la evaluación de políticas de retención."""
    KEEP = "KEEP"
    ROTATE = "ROTATE"
    PURGE = "PURGE"
    PROTECT = "PROTECT"
    SKIP_CORRUPT = "SKIP_CORRUPT"


class RetentionStatus(str, Enum):
    """Estado general de la ejecución de una política de retención."""
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class LogRetentionError(Exception):
    """Excepción base para fallos en el ciclo de vida de retención de logs."""
    pass


class LogRetentionSecurityError(LogRetentionError):
    """Lanzada ante violaciones de aislamiento de entorno, tenant o path traversal."""
    pass


class LogRetentionPolicyError(LogRetentionError):
    """Lanzada ante configuraciones de política de retención inválidas o contradictorias."""
    pass


@dataclass(frozen=True)
class RetentionPolicy:
    """
    Especificación inmutable y declarativa de la política de retención para una clase de log.
    """
    environment: ApplicationEnvironment
    data_class: RetentionClass
    retention_days: int
    enabled: bool = True
    purge_batch_size: int = 500
    protect_latest: int = 0
    dry_run: bool = False
    tenant_id: Optional[str] = None
    is_audit_protected: bool = True
    max_file_size_bytes: int = 10 * 1024 * 1024  # 10 MB default para rotación
    backup_count: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.environment, ApplicationEnvironment):
            normalized = normalize_environment_name(self.environment)
            object.__setattr__(self, "environment", normalized)

        if not isinstance(self.data_class, RetentionClass):
            if isinstance(self.data_class, str):
                try:
                    object.__setattr__(self, "data_class", RetentionClass(self.data_class.upper()))
                except ValueError:
                    raise LogRetentionPolicyError(f"Clase de retención desconocida: {self.data_class}")
            else:
                raise LogRetentionPolicyError("data_class debe ser una instancia de RetentionClass")

        if self.retention_days < 0:
            raise LogRetentionPolicyError(f"retention_days debe ser >= 0, recibido: {self.retention_days}")

        if self.purge_batch_size <= 0:
            raise LogRetentionPolicyError(f"purge_batch_size debe ser > 0, recibido: {self.purge_batch_size}")

        if self.protect_latest < 0:
            raise LogRetentionPolicyError(f"protect_latest debe ser >= 0, recibido: {self.protect_latest}")

        if self.tenant_id is not None:
            try:
                validate_safe_identifier(self.tenant_id, "tenant_id")
            except ValueError as ve:
                raise LogRetentionSecurityError(str(ve))

        # Salvaguarda estricta de Audit Trail (K.1): Nunca permitir retención genérica menor a 365 días sin protección
        if self.data_class == RetentionClass.AUDIT_RECORD:
            if not self.is_audit_protected and self.retention_days < 365:
                raise LogRetentionPolicyError(
                    "AUDIT_RECORD no puede tener una retención inferior a 365 días ni deshabilitar la protección sin política de cumplimiento legal explícita."
                )

    def calculate_cutoff(self, now: Optional[datetime] = None) -> datetime:
        """Calcula el timestamp UTC de corte para la purga."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        return current_time - timedelta(days=self.retention_days)


@dataclass(frozen=True)
class RetentionDecision:
    """
    Decisión atómica sobre un registro o archivo específico durante la evaluación de retención.
    """
    item_id: str
    data_class: RetentionClass
    action: RetentionAction
    reason: str
    occurred_at: Optional[datetime]
    cutoff_date: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tenant_id: Optional[str] = None
    environment: Optional[ApplicationEnvironment] = None

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id no puede estar vacío")
        if not isinstance(self.action, RetentionAction):
            raise ValueError("action debe ser RetentionAction")


@dataclass(frozen=True)
class RetentionResult:
    """
    Resultado global consolidado de la ejecución o simulación (dry-run) de retención.
    """
    environment: ApplicationEnvironment
    data_class: RetentionClass
    dry_run: bool
    scanned_count: int
    eligible_count: int
    purged_count: int
    rotated_count: int
    protected_count: int
    skipped_count: int
    cutoff_utc: datetime
    started_at: datetime
    completed_at: datetime
    status: RetentionStatus
    decisions: Tuple[RetentionDecision, ...] = field(default_factory=tuple)
    errors: Tuple[str, ...] = field(default_factory=tuple)
    tenant_id: Optional[str] = None

    @property
    def is_successful(self) -> bool:
        return self.status == RetentionStatus.SUCCESS and len(self.errors) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "environment": self.environment.value,
            "data_class": self.data_class.value,
            "dry_run": self.dry_run,
            "scanned_count": self.scanned_count,
            "eligible_count": self.eligible_count,
            "purged_count": self.purged_count,
            "rotated_count": self.rotated_count,
            "protected_count": self.protected_count,
            "skipped_count": self.skipped_count,
            "cutoff_utc": self.cutoff_utc.isoformat(),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "status": self.status.value,
            "errors": list(self.errors),
            "tenant_id": self.tenant_id,
            "decisions_summary": [
                {
                    "item_id": d.item_id,
                    "action": d.action.value,
                    "reason": d.reason,
                    "occurred_at": d.occurred_at.isoformat() if d.occurred_at else None,
                }
                for d in self.decisions[:50]  # Limitar para evitar payloads gigantescos
            ],
        }


@dataclass(frozen=True)
class RetentionStatusSummary:
    """
    Resumen del estado actual de almacenamiento y registros elegibles para una clase de log.
    """
    environment: ApplicationEnvironment
    data_class: RetentionClass
    retention_days: int
    total_records: int
    eligible_for_purge: int
    oldest_record_at: Optional[datetime]
    newest_record_at: Optional[datetime]
    last_purge_at: Optional[datetime]
    last_purge_result: Optional[str]
    storage_type: str
    location_summary: str
    tenant_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "environment": self.environment.value,
            "data_class": self.data_class.value,
            "retention_days": self.retention_days,
            "total_records": self.total_records,
            "eligible_for_purge": self.eligible_for_purge,
            "oldest_record_at": self.oldest_record_at.isoformat() if self.oldest_record_at else None,
            "newest_record_at": self.newest_record_at.isoformat() if self.newest_record_at else None,
            "last_purge_at": self.last_purge_at.isoformat() if self.last_purge_at else None,
            "last_purge_result": self.last_purge_result,
            "storage_type": self.storage_type,
            "location_summary": self.location_summary,
            "tenant_id": self.tenant_id,
        }
