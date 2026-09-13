"""Modelos de dominio para Disaster Recovery (P.5 — Recovery Procedures, RPO/RTO & Service Restoration).

Principios P.5:
1. Inmutabilidad y tipado fuerte (frozen=True, dataclasses, enums).
2. Seguridad estricta de conexión: source DB != recovery target.
3. RPO (Recovery Point Objective) y RTO (Recovery Time Objective) medidos con precisión.
4. Preservación estricta de credenciales en conninfo y redactado total en salidas públicas.
5. Cero manipulación frágil de cadenas DSN.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.backup.models import BackupMetadata
from src.domain.deployment.models import (
    ApplicationEnvironment,
    normalize_environment_name,
)
from src.domain.security.models import validate_safe_identifier


class DisasterScenarioType(str, Enum):
    """Escenarios de desastre contemplados en la política de continuidad de negocio."""
    DATABASE_LOSS = "database_loss"                  # Pérdida total del nodo de base de datos
    DATABASE_CORRUPTION = "database_corruption"      # Corrupción de datos o tablas SaaS
    APPLICATION_RUNTIME_LOSS = "app_runtime_loss"    # Caída irrecuperable del proceso o entorno
    PERSISTENT_DATA_LOSS = "persistent_data_loss"    # Pérdida de storage / snapshots
    FAILED_DEPLOYMENT = "failed_deployment"          # Migración o despliegue fallido irreversible
    CONFIGURATION_CORRUPTION = "config_corruption"   # Corrupción crítica de variables/configuración


class RecoveryPhase(str, Enum):
    """Fases ordenadas del ciclo de vida de recuperación ante desastres."""
    INCIDENT_DETECTION = "incident_detection"
    TARGET_VERIFICATION = "target_verification"
    BACKUP_SELECTION = "backup_selection"
    SCHEMA_RESTORE = "schema_restore"
    MIGRATION_CHECK = "migration_check"
    DATA_INTEGRITY_VERIFICATION = "data_integrity_verification"
    SERVICE_RESTORE = "service_restore"
    CLEANUP = "cleanup"


class RecoveryStatus(str, Enum):
    """Estado final o parcial de la ejecución de Disaster Recovery."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class DisasterRecoveryError(RuntimeError):
    """Error base de operaciones de Disaster Recovery."""
    pass


class TargetConnectionVerificationError(DisasterRecoveryError):
    """Fallo en la validación de conexión o identidad del recovery target."""
    pass


class RPOTargetExceededError(DisasterRecoveryError):
    """El punto de recuperación excede el RPO máximo permitido por la política."""
    pass


class RTOTargetExceededError(DisasterRecoveryError):
    """El tiempo de recuperación excede el RTO máximo permitido por la política."""
    pass


@dataclass(frozen=True)
class DisasterRecoveryPolicy:
    """Política de Disaster Recovery que define los SLAs de RPO/RTO y guardas de seguridad."""
    rpo_max_seconds: int = 3600       # 1 hora máximo RPO permitido
    rto_max_seconds: int = 900        # 15 minutos máximo RTO permitido
    require_target_verification: bool = True
    require_migration_check: bool = True
    require_multi_tenant_check: bool = True
    allow_same_database: bool = False

    def __post_init__(self) -> None:
        if self.rpo_max_seconds <= 0:
            raise ValueError("rpo_max_seconds must be positive.")
        if self.rto_max_seconds <= 0:
            raise ValueError("rto_max_seconds must be positive.")


@dataclass(frozen=True)
class DisasterRecoveryPlan:
    """Plan determinista de recuperación ante desastres para un entorno y escenario dados."""
    plan_id: str
    scenario: DisasterScenarioType
    environment: ApplicationEnvironment
    source_database: str
    recovery_target: str
    backup_metadata: Optional[BackupMetadata]
    estimated_rpo_seconds: Optional[float]
    estimated_rto_seconds: float
    steps: Tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        validate_safe_identifier(self.plan_id, "plan_id")
        if not self.source_database or not self.source_database.strip():
            raise ValueError("source_database cannot be empty.")
        if not self.recovery_target or not self.recovery_target.strip():
            raise ValueError("recovery_target cannot be empty.")
        if self.source_database == self.recovery_target:
            raise ValueError(
                f"Dangerous recovery configuration: source_database '{self.source_database}' "
                f"cannot be equal to recovery_target '{self.recovery_target}'."
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "scenario": self.scenario.value,
            "environment": self.environment.value,
            "source_database": self.source_database,
            "recovery_target": self.recovery_target,
            "backup_id": self.backup_metadata.backup_id if self.backup_metadata else None,
            "estimated_rpo_seconds": self.estimated_rpo_seconds,
            "estimated_rto_seconds": self.estimated_rto_seconds,
            "steps": list(self.steps),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class DisasterRecoveryExecutionResult:
    """Resultado auditable y completo de una ejecución/simulación de Disaster Recovery."""
    execution_id: str
    plan: DisasterRecoveryPlan
    status: RecoveryStatus
    target_connection_verified: bool
    backup_verified: bool
    restore_verified: bool
    migration_revision_verified: bool
    restored_revision: Optional[str]
    expected_revision: Optional[str]
    tables_restored_count: int
    expected_tables_count: int
    tenants_restored_count: int
    actual_rpo_seconds: Optional[float]
    actual_rto_seconds: float
    rpo_compliant: bool
    rto_compliant: bool
    cleanup_successful: bool
    completed_at: datetime
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "plan": self.plan.to_dict(),
            "status": self.status.value,
            "target_connection_verified": self.target_connection_verified,
            "backup_verified": self.backup_verified,
            "restore_verified": self.restore_verified,
            "migration_revision_verified": self.migration_revision_verified,
            "restored_revision": self.restored_revision,
            "expected_revision": self.expected_revision,
            "tables_restored_count": self.tables_restored_count,
            "expected_tables_count": self.expected_tables_count,
            "tenants_restored_count": self.tenants_restored_count,
            "actual_rpo_seconds": self.actual_rpo_seconds,
            "actual_rto_seconds": self.actual_rto_seconds,
            "rpo_compliant": self.rpo_compliant,
            "rto_compliant": self.rto_compliant,
            "cleanup_successful": self.cleanup_successful,
            "completed_at": self.completed_at.isoformat(),
            "error_message": self.error_message,
        }
