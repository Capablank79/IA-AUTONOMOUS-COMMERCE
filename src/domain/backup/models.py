"""Modelos de dominio para Backups de Base de Datos y Verificación de Restauración (P.4).

Principios P.4:
1. Inmutabilidad y tipado fuerte (frozen=True, tuples, enums).
2. Determinismo en nombrado y cálculo de integridad (SHA-256).
3. Aislamiento estricto por Environment (P.2 / O.13).
4. Cero almacenamiento o exposición de secretos, contraseñas o DSNs en texto plano.
5. Preservación del principio de seguridad: source DB != restore test target DB.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.deployment.models import (
    ApplicationEnvironment,
    DeploymentEnvironment,
    normalize_environment_name,
)
from src.domain.security.models import validate_safe_identifier


class BackupFormat(str, Enum):
    """Formatos soportados de backup nativo de PostgreSQL."""
    CUSTOM = "custom"  # pg_dump -Fc (recomendado: comprimido, flexible para restore selectivo)
    DIRECTORY = "directory"
    TAR = "tar"
    PLAIN = "plain"


class BackupStatus(str, Enum):
    """Estados canónicos del ciclo de vida de un backup."""
    COMPLETED = "completed"
    CORRUPTED = "corrupted"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RestoreValidationStatus(str, Enum):
    """Estados de validación de restore en base de datos temporal de prueba."""
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class BackupError(RuntimeError):
    """Error base de operaciones de backup o restore."""
    pass


class BackupIntegrityError(BackupError):
    """Fallo en la verificación criptográfica (checksum mismatch o archivo corrupto)."""
    pass


class BackupEnvironmentMismatchError(BackupError):
    """Violación de frontera de aislamiento de environment (e.g. intentar restaurar prod en dev sin autorización)."""
    pass


class DangerousRestoreTargetError(BackupError):
    """Intento de restauración sobre la base de datos fuente u objetivo no seguro."""
    pass


@dataclass(frozen=True)
class BackupMetadata:
    """Metadata inmutable y sanitizada asociada a un archivo de backup."""
    backup_id: str
    environment: ApplicationEnvironment
    database_name: str
    created_at: datetime
    postgres_version: str
    migration_revision: str
    backup_format: BackupFormat
    file_size_bytes: int
    checksum_sha256: str
    file_name: str
    app_version: str = "0.1.0"

    def __post_init__(self) -> None:
        validate_safe_identifier(self.backup_id, "backup_id")
        if not self.database_name or not self.database_name.strip():
            raise ValueError("database_name cannot be empty.")
        if not self.checksum_sha256 or len(self.checksum_sha256) != 64:
            raise ValueError("checksum_sha256 must be a valid 64-character hex SHA-256 hash.")
        if self.file_size_bytes < 0:
            raise ValueError("file_size_bytes cannot be negative.")

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la metadata a diccionario seguro sin secretos."""
        return {
            "backup_id": self.backup_id,
            "environment": self.environment.value,
            "database_name": self.database_name,
            "created_at": self.created_at.isoformat(),
            "postgres_version": self.postgres_version,
            "migration_revision": self.migration_revision,
            "backup_format": self.backup_format.value,
            "file_size_bytes": self.file_size_bytes,
            "checksum_sha256": self.checksum_sha256,
            "file_name": self.file_name,
            "app_version": self.app_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BackupMetadata":
        """Deserializa un diccionario de metadata sanitizada."""
        env_raw = data.get("environment", "development")
        env = normalize_environment_name(str(env_raw))
        created_at_raw = data.get("created_at")
        if isinstance(created_at_raw, str):
            created_at = datetime.fromisoformat(created_at_raw)
        elif isinstance(created_at_raw, datetime):
            created_at = created_at_raw
        else:
            created_at = datetime.now(timezone.utc)

        fmt_raw = str(data.get("backup_format", "custom")).lower()
        try:
            fmt = BackupFormat(fmt_raw)
        except ValueError:
            fmt = BackupFormat.CUSTOM

        return cls(
            backup_id=str(data["backup_id"]),
            environment=env,
            database_name=str(data["database_name"]),
            created_at=created_at,
            postgres_version=str(data.get("postgres_version", "PostgreSQL")),
            migration_revision=str(data.get("migration_revision", "unknown")),
            backup_format=fmt,
            file_size_bytes=int(data.get("file_size_bytes", 0)),
            checksum_sha256=str(data.get("checksum_sha256", "")),
            file_name=str(data.get("file_name", "")),
            app_version=str(data.get("app_version", "0.1.0")),
        )


@dataclass(frozen=True)
class BackupExecutionResult:
    """Resultado determinista de la creación de un backup."""
    status: BackupStatus
    backup_path: Path
    metadata_path: Path
    metadata: BackupMetadata
    duration_seconds: float
    error_message: Optional[str] = None


@dataclass(frozen=True)
class RestoreValidationResult:
    """Resultado detallado de la prueba de restore en base de datos o esquema temporal."""
    status: RestoreValidationStatus
    target_database_or_schema: str
    migration_revision_verified: bool
    restored_revision: Optional[str]
    expected_revision: str
    tables_verified_count: int
    expected_tables_count: int
    missing_tables: Tuple[str, ...]
    tenants_verified_count: int
    data_records_verified: int
    tenant_isolation_preserved: bool
    cleanup_successful: bool
    duration_seconds: float
    error_message: Optional[str] = None


def generate_backup_filename(
    database_name: str,
    environment: ApplicationEnvironment,
    timestamp: Optional[datetime] = None,
    suffix: str = ".dump",
) -> str:
    """Genera un nombre de archivo determinista y seguro contra path traversal."""
    ts = timestamp or datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y%m%dT%H%M%SZ")
    clean_db = re.sub(r"[^a-zA-Z0-9_-]", "_", database_name)
    clean_env = environment.value
    filename = f"{clean_db}_{clean_env}_{ts_str}{suffix}"
    validate_safe_identifier(filename, "generated_filename")
    return filename


def calculate_file_sha256(file_path: Path, chunk_size: int = 65536) -> str:
    """Calcula el checksum SHA-256 de un archivo en streaming."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()
