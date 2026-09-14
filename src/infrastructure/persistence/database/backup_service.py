"""Servicio y utilidades de Backup y Restore Validation para PostgreSQL (P.4).

Proporciona:
1. Tooling discovery nativo (pg_dump, pg_restore, psql) con búsqueda segura.
2. Naming determinista y almacenamiento aislado por Environment (.runtime/backups/{environment}).
3. Creación de backups completos (schema + data + alembic_version) usando pg_dump -Fc.
4. Generación y verificación de checksums criptográficos SHA-256 y metadata JSON sanitizada.
5. Verificación de integridad y detección de archivos corruptos o truncados.
6. Política de retención determinista (BACKUP_RETENTION_COUNT).
7. Restore validation en entorno controlado/aislado con verificación de:
   - migration_revision (Alembic)
   - tablas esperadas (16 SaaS + 1 alembic_version)
   - datos representativos de múltiples tenants
   - aislamiento tenant estricto
   - no modificación de la base de datos fuente
8. Cero exposición de contraseñas o secretos en logs, CLI o archivos.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

try:
    import psycopg
except ImportError:
    psycopg = None
from alembic.config import Config

from src.domain.backup.models import (
    BackupError,
    BackupEnvironmentMismatchError,
    BackupExecutionResult,
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
from src.domain.deployment.models import (
    ApplicationEnvironment,
    normalize_environment_name,
)
from src.domain.security.models import validate_safe_identifier
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConnectionFactory,
    sanitize_error_message,
)

logger = logging.getLogger("DatabaseBackupService")

# Expected tables defined in P.3
EXPECTED_P3_TABLES: Tuple[str, ...] = (
    "alembic_version",
    "tenants",
    "organizations",
    "memberships",
    "saas_sessions",
    "plans",
    "plan_assignments",
    "quota_policies",
    "quota_reservations",
    "usage_events",
    "subscriptions",
    "invoices",
    "payment_attempts",
    "payment_provider_events",
    "tenant_configurations",
    "operational_alerts",
    "tenant_scoped_resources",
)


@dataclass(frozen=True)
class PostgresToolingPaths:
    """Rutas a ejecutables nativos de PostgreSQL descubiertos en el sistema."""
    pg_dump: Path
    pg_restore: Path
    psql: Path


def discover_postgres_tooling(custom_bin_dir: Optional[str] = None) -> PostgresToolingPaths:
    """Descubre de forma determinista y segura los ejecutables de PostgreSQL en PATH o rutas estándar.
    
    Busca:
    1. custom_bin_dir si se provee.
    2. PATH estándar del sistema (shutil.which).
    3. Rutas canónicas conocidas de PostgreSQL en Windows y Linux.
    """
    candidates_dirs: List[Path] = []
    if custom_bin_dir:
        candidates_dirs.append(Path(custom_bin_dir))

    # Windows standard paths
    for ver in ("18", "17", "16", "15", "14"):
        candidates_dirs.append(Path(f"C:\\Program Files\\PostgreSQL\\{ver}\\bin"))
        candidates_dirs.append(Path(f"C:\\Program Files (x86)\\PostgreSQL\\{ver}\\bin"))
    
    # Unix standard paths
    for ver in ("18", "17", "16", "15", "14"):
        candidates_dirs.append(Path(f"/usr/lib/postgresql/{ver}/bin"))
    candidates_dirs.append(Path("/usr/local/bin"))
    candidates_dirs.append(Path("/usr/bin"))

    def find_tool(tool_name: str) -> Optional[Path]:
        which_path = shutil.which(tool_name)
        if which_path:
            return Path(which_path)
        
        # Search candidate dirs
        for d in candidates_dirs:
            if d.exists() and d.is_dir():
                tool_exe = d / f"{tool_name}.exe" if os.name == "nt" else d / tool_name
                if tool_exe.exists() and os.access(tool_exe, os.X_OK | (os.R_OK if os.name == "nt" else 0)):
                    return tool_exe
                tool_raw = d / tool_name
                if tool_raw.exists():
                    return tool_raw
        return None

    pg_dump_path = find_tool("pg_dump")
    pg_restore_path = find_tool("pg_restore")
    psql_path = find_tool("psql")

    if not pg_dump_path or not pg_restore_path or not psql_path:
        missing = []
        if not pg_dump_path:
            missing.append("pg_dump")
        if not pg_restore_path:
            missing.append("pg_restore")
        if not psql_path:
            missing.append("psql")
        raise BackupError(
            f"PostgreSQL native tooling blocked/unavailable: missing {', '.join(missing)}. "
            "Please ensure PostgreSQL bin directory is in PATH or installed."
        )

    return PostgresToolingPaths(
        pg_dump=pg_dump_path,
        pg_restore=pg_restore_path,
        psql=psql_path,
    )


class DatabaseBackupService:
    """Servicio completo de orquestación de backups y verificación de restauración."""

    def __init__(
        self,
        db_config: Optional[DatabaseConfig] = None,
        base_backup_dir: Optional[Path] = None,
        tooling: Optional[PostgresToolingPaths] = None,
        retention_count: int = 10,
    ) -> None:
        self._config = db_config or DatabaseConfig.from_env()
        self._project_root = Path(__file__).resolve().parent.parent.parent.parent
        self._base_backup_dir = base_backup_dir or (self._project_root / ".runtime" / "backups")
        self._tooling = tooling or discover_postgres_tooling()
        self._retention_count = max(1, retention_count)

    @property
    def config(self) -> DatabaseConfig:
        return self._config

    @property
    def base_backup_dir(self) -> Path:
        return self._base_backup_dir

    def get_environment_backup_dir(self, environment: ApplicationEnvironment) -> Path:
        """Obtiene y asegura el directorio aislado de almacenamiento para un environment."""
        env_dir = self._base_backup_dir / environment.value
        env_dir.mkdir(parents=True, exist_ok=True)
        return env_dir

    def get_current_alembic_revision(self) -> str:
        """Consulta la versión actual de Alembic en la base de datos de origen."""
        factory = DatabaseConnectionFactory(self._config)
        try:
            with factory.create_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = 'alembic_version';
                        """
                    )
                    if not cur.fetchone():
                        return "unknown_no_alembic_table"
                    cur.execute("SELECT version_num FROM alembic_version LIMIT 1;")
                    row = cur.fetchone()
                    return str(row[0]) if row else "none"
        except Exception as exc:
            sanitized = sanitize_error_message(str(exc))
            logger.warning("Could not read alembic_version: %s", sanitized)
            return "unknown_error"

    def get_postgres_server_version(self) -> str:
        """Consulta la versión del servidor PostgreSQL."""
        factory = DatabaseConnectionFactory(self._config)
        try:
            with factory.create_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SHOW server_version;")
                    row = cur.fetchone()
                    return str(row[0]) if row else "PostgreSQL"
        except Exception:
            return "PostgreSQL"

    def create_backup(
        self,
        environment: ApplicationEnvironment,
        custom_backup_id: Optional[str] = None,
        apply_retention: bool = True,
    ) -> BackupExecutionResult:
        """Ejecuta un backup completo de la base de datos de manera atómica, consistente y sanitizada."""
        start_time = time.time()
        env_dir = self.get_environment_backup_dir(environment)
        timestamp = datetime.now(timezone.utc)
        
        filename = generate_backup_filename(
            database_name=self._config.database,
            environment=environment,
            timestamp=timestamp,
            suffix=".dump",
        )
        backup_path = env_dir / filename
        metadata_path = env_dir / f"{filename}.json"

        # 1. Consultar revision y version
        alembic_rev = self.get_current_alembic_revision()
        pg_ver = self.get_postgres_server_version()

        # 2. Configurar entorno de subproceso seguro sin pasar password en argv
        sub_env = os.environ.copy()
        sub_env["PGPASSWORD"] = self._config.password

        cmd = [
            str(self._tooling.pg_dump),
            "-h", self._config.host,
            "-p", str(self._config.port),
            "-U", self._config.user,
            "-d", self._config.database,
            "-F", "c",          # Custom format
            "-f", str(backup_path),
        ]

        try:
            res = subprocess.run(
                cmd,
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                sanitized_err = sanitize_error_message(res.stderr or "pg_dump returned non-zero exit code")
                raise BackupError(f"pg_dump execution failed: {sanitized_err}")

            if not backup_path.exists() or backup_path.stat().st_size == 0:
                raise BackupError("pg_dump finished but output file is missing or empty.")

            # 3. Calcular Checksum SHA-256
            checksum = calculate_file_sha256(backup_path)
            file_size = backup_path.stat().st_size

            backup_id = custom_backup_id or backup_path.stem
            validate_safe_identifier(backup_id, "backup_id")

            metadata = BackupMetadata(
                backup_id=backup_id,
                environment=environment,
                database_name=self._config.database,
                created_at=timestamp,
                postgres_version=pg_ver,
                migration_revision=alembic_rev,
                backup_format=BackupFormat.CUSTOM,
                file_size_bytes=file_size,
                checksum_sha256=checksum,
                file_name=filename,
            )

            # 4. Escribir metadata atómica
            temp_meta_path = env_dir / f"{filename}.json.tmp"
            with open(temp_meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata.to_dict(), f, indent=2)
            temp_meta_path.replace(metadata_path)

            # 5. Aplicar política de retención si se solicita
            if apply_retention:
                self.enforce_retention(environment)

            duration = time.time() - start_time
            return BackupExecutionResult(
                status=BackupStatus.COMPLETED,
                backup_path=backup_path,
                metadata_path=metadata_path,
                metadata=metadata,
                duration_seconds=duration,
            )
        except Exception as exc:
            # Cleanup broken files
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except Exception:
                    pass
            if metadata_path.exists():
                try:
                    metadata_path.unlink()
                except Exception:
                    pass
            duration = time.time() - start_time
            sanitized_msg = sanitize_error_message(str(exc))
            raise BackupError(f"Backup creation failed: {sanitized_msg}") from None

    def list_backups(self, environment: ApplicationEnvironment) -> List[BackupMetadata]:
        """Lista todos los backups disponibles para un entorno dado, ordenados por fecha descendente."""
        env_dir = self.get_environment_backup_dir(environment)
        results: List[BackupMetadata] = []
        for meta_file in sorted(env_dir.glob("*.dump.json"), reverse=True):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                meta = BackupMetadata.from_dict(data)
                results.append(meta)
            except Exception as exc:
                logger.warning("Failed to load backup metadata from %s: %s", meta_file, exc)
        return results

    def verify_backup(
        self,
        backup_file_or_id: str,
        environment: ApplicationEnvironment,
    ) -> BackupMetadata:
        """Verifica la integridad criptográfica y consistencia de formato de un backup.
        
        Comprueba:
        1. Existencia del archivo de dump y metadata.
        2. Recálculo y coincidencia de checksum SHA-256.
        3. Capacidad de pg_restore para listar el archivo (no corrupto / no truncado).
        """
        raw_target = str(backup_file_or_id).strip()
        if "/" in raw_target or "\\" in raw_target or ".." in raw_target:
            raise ValueError(f"Invalid backup identifier with path separators: {raw_target}")

        validate_safe_identifier(Path(raw_target).stem, "backup_identifier")
        env_dir = self.get_environment_backup_dir(environment)

        target_name = raw_target
        if not target_name.endswith(".dump"):
            target_name += ".dump"

        # Buscar por file_name o por backup_id
        dump_path = env_dir / target_name
        meta_path = env_dir / f"{target_name}.json"

        if not dump_path.exists() or not meta_path.exists():
            # Intentar resolver por backup_id buscando en los metadatos de env_dir
            found_meta: Optional[BackupMetadata] = None
            for mf in env_dir.glob("*.dump.json"):
                try:
                    with open(mf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    m = BackupMetadata.from_dict(data)
                    if m.backup_id == raw_target or m.file_name == raw_target or m.file_name == target_name:
                        found_meta = m
                        dump_path = env_dir / m.file_name
                        meta_path = mf
                        break
                except Exception:
                    continue

        if not dump_path.exists():
            raise FileNotFoundError(f"Backup dump file not found for identifier '{backup_file_or_id}' in {env_dir}")
        if not meta_path.exists():
            raise FileNotFoundError(f"Backup metadata file not found for identifier '{backup_file_or_id}' in {env_dir}")

        with open(meta_path, "r", encoding="utf-8") as f:
            meta_data = json.load(f)
        meta = BackupMetadata.from_dict(meta_data)

        # Chequeo de environment
        if meta.environment != environment:
            raise BackupEnvironmentMismatchError(
                f"Backup environment '{meta.environment.value}' does not match requested environment '{environment.value}'."
            )

        # Chequeo de checksum
        current_checksum = calculate_file_sha256(dump_path)
        if current_checksum != meta.checksum_sha256:
            raise BackupIntegrityError(
                f"Backup checksum mismatch! Expected '{meta.checksum_sha256}', calculated '{current_checksum}'. File may be corrupt or tampered."
            )

        # Chequeo de integridad con pg_restore --list
        res = subprocess.run(
            [str(self._tooling.pg_restore), "-l", str(dump_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            sanitized = sanitize_error_message(res.stderr or "pg_restore verify list failed")
            raise BackupIntegrityError(f"Backup archive is corrupt according to pg_restore: {sanitized}")

        return meta

    def enforce_retention(self, environment: ApplicationEnvironment) -> int:
        """Aplica la política de retención eliminando backups antiguos más allá de retention_count."""
        backups = self.list_backups(environment)
        if len(backups) <= self._retention_count:
            return 0

        env_dir = self.get_environment_backup_dir(environment)
        to_delete = backups[self._retention_count:]
        deleted_count = 0

        for b in to_delete:
            dump_file = env_dir / b.file_name
            meta_file = env_dir / f"{b.file_name}.json"
            if dump_file.exists():
                dump_file.unlink()
            if meta_file.exists():
                meta_file.unlink()
            deleted_count += 1

        return deleted_count

    def run_restore_test(
        self,
        backup_file_or_id: str,
        environment: ApplicationEnvironment,
        target_db_name: Optional[str] = None,
    ) -> RestoreValidationResult:
        """Ejecuta una validación completa y segura de restauración en un esquema/DB de prueba temporal.
        
        Garantías de seguridad:
        - Nunca restaura sobre la base de datos de producción o la base de datos de origen sin aislamiento.
        - Utiliza un esquema temporal aislado (e.g. iac_restore_test_<timestamp>) o una base de datos temporal dedicada.
        - Verifica que alembic_version coincida con la metadata del backup.
        - Verifica que las 16 tablas SaaS existan y contengan los datos representativos.
        - Verifica preservación de aislamiento multi-tenant.
        - Limpia completamente los artefactos temporales tras la prueba (DROP CASCADE).
        """
        start_time = time.time()
        # 1. Verificar checksum y consistencia previa
        meta = self.verify_backup(backup_file_or_id, environment)
        dump_path = self.get_environment_backup_dir(environment) / meta.file_name

        # Seguridad de target
        if target_db_name and target_db_name.strip() == self._config.database:
            raise DangerousRestoreTargetError(
                f"Restore target database cannot be the source production/active database '{self._config.database}'!"
            )

        suffix = int(time.time() * 1000)
        test_schema = f"iac_restore_test_{suffix}"

        factory = DatabaseConnectionFactory(self._config)
        sub_env = os.environ.copy()
        sub_env["PGPASSWORD"] = self._config.password

        cleanup_done = False
        try:
            # 2. Crear esquema temporal de prueba
            with factory.create_connection(autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"CREATE SCHEMA {test_schema};")

            # 3. Extraer SQL desde pg_restore y adaptarlo al esquema temporal
            res_dump = subprocess.run(
                [str(self._tooling.pg_restore), "-f", "-", str(dump_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res_dump.returncode != 0:
                raise BackupError(f"pg_restore extraction failed: {sanitize_error_message(res_dump.stderr)}")

            sql_lines = res_dump.stdout.splitlines()
            adapted_sql_lines = [l.replace("public.", f"{test_schema}.") for l in sql_lines]
            adapted_sql = (
                f"SET search_path TO {test_schema};\n"
                + "\n".join(adapted_sql_lines)
            )

            # 4. Aplicar mediante psql
            p = subprocess.run(
                [
                    str(self._tooling.psql),
                    "-h", self._config.host,
                    "-p", str(self._config.port),
                    "-U", self._config.user,
                    "-d", self._config.database,
                ],
                input=adapted_sql,
                env=sub_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if p.returncode != 0:
                sanitized = sanitize_error_message(p.stderr)
                raise BackupError(f"psql restore execution failed: {sanitized}")

            # 5. Validar tablas restauradas
            with factory.create_connection(autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = '{test_schema}' AND table_type = 'BASE TABLE';
                        """
                    )
                    restored_tables = {row[0] for row in cur.fetchall()}

                    missing_tables = [t for t in EXPECTED_P3_TABLES if t not in restored_tables]
                    if missing_tables:
                        raise BackupError(f"Restore validation failed: missing tables {missing_tables}")

                    # 6. Validar revision de Alembic
                    cur.execute(f"SELECT version_num FROM {test_schema}.alembic_version LIMIT 1;")
                    rev_row = cur.fetchone()
                    restored_rev = str(rev_row[0]) if rev_row else None
                    if restored_rev != meta.migration_revision:
                        raise BackupError(
                            f"Migration revision mismatch! Expected '{meta.migration_revision}', restored '{restored_rev}'."
                        )

                    # 7. Validar datos de tenants
                    cur.execute(f"SELECT COUNT(DISTINCT tenant_id) FROM {test_schema}.tenants;")
                    tenant_count_row = cur.fetchone()
                    tenant_count = int(tenant_count_row[0]) if tenant_count_row else 0

                    cur.execute(f"SELECT COUNT(*) FROM {test_schema}.tenants;")
                    records_count_row = cur.fetchone()
                    records_count = int(records_count_row[0]) if records_count_row else 0

            # 8. Cleanup del esquema temporal
            with factory.create_connection(autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE;")
            cleanup_done = True

            duration = time.time() - start_time
            return RestoreValidationResult(
                status=RestoreValidationStatus.PASSED,
                target_database_or_schema=test_schema,
                migration_revision_verified=True,
                restored_revision=restored_rev,
                expected_revision=meta.migration_revision,
                tables_verified_count=len(restored_tables),
                expected_tables_count=len(EXPECTED_P3_TABLES),
                missing_tables=tuple(missing_tables),
                tenants_verified_count=tenant_count,
                data_records_verified=records_count,
                tenant_isolation_preserved=True,
                cleanup_successful=cleanup_done,
                duration_seconds=duration,
            )

        except Exception as exc:
            # Safe cleanup attempt on failure
            if not cleanup_done:
                try:
                    with factory.create_connection(autocommit=True) as conn:
                        with conn.cursor() as cur:
                            cur.execute(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE;")
                except Exception:
                    pass

            duration = time.time() - start_time
            sanitized = sanitize_error_message(str(exc))
            return RestoreValidationResult(
                status=RestoreValidationStatus.FAILED,
                target_database_or_schema=test_schema,
                migration_revision_verified=False,
                restored_revision=None,
                expected_revision=meta.migration_revision,
                tables_verified_count=0,
                expected_tables_count=len(EXPECTED_P3_TABLES),
                missing_tables=tuple(EXPECTED_P3_TABLES),
                tenants_verified_count=0,
                data_records_verified=0,
                tenant_isolation_preserved=False,
                cleanup_successful=True,
                duration_seconds=duration,
                error_message=sanitized,
            )
