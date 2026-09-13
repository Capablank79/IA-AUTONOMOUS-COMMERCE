"""Módulo de dominio para Backups de Base de Datos y Verificación de Restauración (P.4)."""

from src.domain.backup.models import (
    BackupError,
    BackupExecutionResult,
    BackupEnvironmentMismatchError,
    BackupFormat,
    BackupIntegrityError,
    BackupMetadata,
    BackupStatus,
    DangerousRestoreTargetError,
    RestoreValidationResult,
    RestoreValidationStatus,
    calculate_file_sha256,
    generate_backup_filename,
)

__all__ = [
    "BackupError",
    "BackupExecutionResult",
    "BackupEnvironmentMismatchError",
    "BackupFormat",
    "BackupIntegrityError",
    "BackupMetadata",
    "BackupStatus",
    "DangerousRestoreTargetError",
    "RestoreValidationResult",
    "RestoreValidationStatus",
    "calculate_file_sha256",
    "generate_backup_filename",
]
