"""Motor de migraciones determinista y transaccional para PostgreSQL (P.3).

Proporciona:
1. BaseMigration: Clase base para migraciones versionadas con upgrade/downgrade y checksum.
2. MigrationRunner: Ejecutor transaccional de migraciones con idempotencia y control de versiones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import inspect
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    import psycopg
    from psycopg import sql
except ImportError:
    psycopg = None
    sql = None

from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConnectionError,
    DatabaseConnectionFactory,
    sanitize_error_message,
)


@dataclass(frozen=True)
class MigrationRecord:
    """Registro de migración aplicada en la base de datos."""
    version: str
    name: str
    applied_at: datetime
    checksum: str


class Migration:
    """Clase base para migraciones de base de datos."""
    version: str = ""
    name: str = ""
    dependencies: Tuple[str, ...] = ()

    def upgrade(self, conn: psycopg.Connection) -> None:
        """Aplica la migración."""
        raise NotImplementedError

    def downgrade(self, conn: psycopg.Connection) -> None:
        """Revierte la migración (si es soportado)."""
        raise NotImplementedError

    def compute_checksum(self) -> str:
        """Calcula un checksum SHA-256 del código de la migración."""
        try:
            source = inspect.getsource(self.__class__)
        except Exception:
            source = f"{self.version}:{self.name}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()


class MigrationRunner:
    """Ejecutor de migraciones de PostgreSQL."""

    def __init__(
        self,
        connection_factory: DatabaseConnectionFactory,
        migrations_package: Optional[Sequence[Migration]] = None,
    ) -> None:
        self._factory = connection_factory
        self._migrations: List[Migration] = []
        if migrations_package is not None:
            self._migrations = list(migrations_package)
        else:
            self._load_registered_migrations()
        # Orden determinista por versión
        self._migrations.sort(key=lambda m: m.version)

    def _load_registered_migrations(self) -> None:
        """Carga automáticamente las migraciones desde el paquete versions."""
        from src.infrastructure.persistence.database.migrations.versions import (
            REGISTERED_MIGRATIONS,
        )
        self._migrations = list(REGISTERED_MIGRATIONS)

    @property
    def registered_migrations(self) -> List[Migration]:
        return list(self._migrations)

    @property
    def head_version(self) -> Optional[str]:
        return self._migrations[-1].version if self._migrations else None

    def ensure_migration_table(self, conn: psycopg.Connection) -> None:
        """Crea la tabla de control de migraciones si no existe."""
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    checksum VARCHAR(64) NOT NULL
                );
            """)

    def get_applied_migrations(self, conn: psycopg.Connection) -> List[MigrationRecord]:
        """Obtiene la lista de migraciones aplicadas ordenadas por applied_at."""
        self.ensure_migration_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, name, applied_at, checksum FROM schema_migrations ORDER BY version ASC;"
            )
            rows = cur.fetchall()
            return [
                MigrationRecord(
                    version=r[0],
                    name=r[1],
                    applied_at=r[2],
                    checksum=r[3],
                )
                for r in rows
            ]

    def current_version(self) -> Optional[str]:
        """Retorna la última versión aplicada en la base de datos."""
        conn = self._factory.create_connection(autocommit=True)
        try:
            applied = self.get_applied_migrations(conn)
            return applied[-1].version if applied else None
        finally:
            if not conn.closed:
                conn.close()

    def get_status(self) -> Dict[str, Any]:
        """Obtiene el estado actual del esquema vs migraciones registradas."""
        conn = self._factory.create_connection(autocommit=True)
        try:
            applied = self.get_applied_migrations(conn)
            applied_map = {m.version: m for m in applied}
            pending = [m for m in self._migrations if m.version not in applied_map]
            
            return {
                "current_version": applied[-1].version if applied else None,
                "head_version": self.head_version,
                "applied_count": len(applied),
                "pending_count": len(pending),
                "applied_migrations": [
                    {
                        "version": m.version,
                        "name": m.name,
                        "applied_at": m.applied_at.isoformat() if m.applied_at else None,
                        "checksum": m.checksum,
                    }
                    for m in applied
                ],
                "pending_migrations": [
                    {
                        "version": m.version,
                        "name": m.name,
                        "checksum": m.compute_checksum(),
                    }
                    for m in pending
                ],
                "is_up_to_date": len(pending) == 0,
            }
        finally:
            if not conn.closed:
                conn.close()

    def check_compatibility(self) -> bool:
        """Verifica si la base de datos está al día con el HEAD del código."""
        status = self.get_status()
        return bool(status["is_up_to_date"])

    def upgrade(self, target_version: Optional[str] = None) -> List[str]:
        """Aplica todas las migraciones pendientes hasta target_version (o HEAD).
        
        Cada migración se ejecuta dentro de una transacción atómica.
        Retorna la lista de versiones aplicadas.
        """
        conn = self._factory.create_connection(autocommit=False)
        applied_now: List[str] = []
        try:
            self.ensure_migration_table(conn)
            conn.commit()

            applied_records = self.get_applied_migrations(conn)
            applied_versions = {r.version for r in applied_records}

            for migration in self._migrations:
                if migration.version in applied_versions:
                    continue

                # Validar dependencias
                for dep in migration.dependencies:
                    if dep not in applied_versions and dep not in applied_now:
                        raise RuntimeError(
                            f"Cannot apply migration {migration.version}: missing dependency {dep}"
                        )

                # Ejecutar migración dentro de una transacción atómica
                try:
                    with conn.transaction():
                        migration.upgrade(conn)
                        chk = migration.compute_checksum()
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO schema_migrations (version, name, applied_at, checksum)
                                VALUES (%s, %s, %s, %s);
                                """,
                                (
                                    migration.version,
                                    migration.name,
                                    datetime.now(timezone.utc),
                                    chk,
                                ),
                            )
                    applied_now.append(migration.version)
                except Exception as exc:
                    conn.rollback()
                    sanitized = sanitize_error_message(str(exc))
                    raise RuntimeError(
                        f"Migration {migration.version} ({migration.name}) failed and was rolled back: {sanitized}"
                    ) from exc

                if target_version and migration.version == target_version:
                    break

            return applied_now
        finally:
            if not conn.closed:
                conn.close()
