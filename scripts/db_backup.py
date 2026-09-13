"""CLI y orquestador de Backups y Restore Validation para PostgreSQL (P.4).

Proporciona comandos seguros, deterministas y sanitizados:
- create: Genera un backup completo (schema + data + alembic_version) en .runtime/backups/{environment}/
- list: Lista los backups existentes para un environment con su metadata y estado.
- verify: Comprueba integridad criptográfica (SHA-256) y legibilidad física (pg_restore -l).
- restore-test: Ejecuta una prueba real y aislada de restauración en esquema temporal sin afectar la base activa.

Seguridad:
- Cero exposición de contraseñas en CLI, logs o argumentos de subprocesos.
- Requiere environment explícito (development, staging, production).
- Sanitiza todos los mensajes de salida y error.
"""

import argparse
import os
from pathlib import Path
import sys
from typing import Optional

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.domain.backup.models import BackupError
from src.domain.deployment.models import (
    ApplicationEnvironment,
    normalize_environment_name,
)
from src.infrastructure.persistence.database.backup_service import (
    DatabaseBackupService,
    discover_postgres_tooling,
)
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConfigError,
    sanitize_error_message,
)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IA Autonomous Commerce - PostgreSQL Backup & Restore Validation Tooling (P.4)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcomandos de backup")

    # create
    create_parser = subparsers.add_parser("create", help="Crear un nuevo backup de base de datos")
    create_parser.add_argument(
        "-e", "--environment",
        required=True,
        help="Entorno de destino (development, staging, production, testing)",
    )
    create_parser.add_argument(
        "--backup-id",
        required=False,
        help="Identificador personalizado para el backup (opcional)",
    )
    create_parser.add_argument(
        "--no-retention",
        action="store_true",
        help="Omitir la aplicación de la política de retención",
    )

    # list
    list_parser = subparsers.add_parser("list", help="Listar backups registrados para un entorno")
    list_parser.add_argument(
        "-e", "--environment",
        required=True,
        help="Entorno a consultar (development, staging, production, testing)",
    )

    # verify
    verify_parser = subparsers.add_parser("verify", help="Verificar checksum y consistencia de un backup")
    verify_parser.add_argument(
        "backup",
        help="Nombre de archivo de dump o ID del backup a verificar",
    )
    verify_parser.add_argument(
        "-e", "--environment",
        required=True,
        help="Entorno al que pertenece el backup",
    )

    # restore-test
    restore_parser = subparsers.add_parser("restore-test", help="Validar restauración en esquema/entorno de prueba temporal")
    restore_parser.add_argument(
        "backup",
        help="Nombre de archivo de dump o ID del backup a validar",
    )
    restore_parser.add_argument(
        "-e", "--environment",
        required=True,
        help="Entorno al que pertenece el backup",
    )

    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    try:
        env = normalize_environment_name(args.environment)
    except Exception as exc:
        print(f"ERROR: Entorno inválido '{args.environment}': {exc}", file=sys.stderr)
        return 1

    try:
        service = DatabaseBackupService()
    except Exception as exc:
        sanitized = sanitize_error_message(str(exc))
        print(f"ERROR al inicializar servicio de backup: {sanitized}", file=sys.stderr)
        return 1

    try:
        if args.command == "create":
            print(f"Iniciando backup para entorno [{env.value}] en base de datos [{service.config.database}]...")
            result = service.create_backup(
                environment=env,
                custom_backup_id=args.backup_id,
                apply_retention=not args.no_retention,
            )
            print("BACKUP_CREATE: OK")
            print(f"ENVIRONMENT: {result.metadata.environment.value}")
            print(f"DATABASE: {result.metadata.database_name}")
            print(f"FILE: {result.backup_path.name}")
            print(f"SIZE_BYTES: {result.metadata.file_size_bytes}")
            print(f"CHECKSUM: {result.metadata.checksum_sha256}")
            print(f"MIGRATION_REVISION: {result.metadata.migration_revision}")
            print(f"DURATION_SECONDS: {result.duration_seconds:.2f}")
            return 0

        elif args.command == "list":
            backups = service.list_backups(env)
            print(f"=== Backups registrados para entorno [{env.value}] (Total: {len(backups)}) ===")
            if not backups:
                print("No hay backups disponibles.")
                return 0
            for b in backups:
                print(f"- ID: {b.backup_id}")
                print(f"  Archivo: {b.file_name}")
                print(f"  Fecha: {b.created_at.isoformat()}")
                print(f"  Tamaño: {b.file_size_bytes} bytes")
                print(f"  Alembic Rev: {b.migration_revision}")
                print(f"  SHA-256: {b.checksum_sha256[:16]}...")
            return 0

        elif args.command == "verify":
            print(f"Verificando integridad del backup [{args.backup}] para entorno [{env.value}]...")
            meta = service.verify_backup(args.backup, env)
            print("BACKUP_VERIFY: OK")
            print(f"ENVIRONMENT: {meta.environment.value}")
            print(f"DATABASE: {meta.database_name}")
            print(f"FILE: {meta.file_name}")
            print(f"CHECKSUM: {meta.checksum_sha256}")
            print(f"MIGRATION_REVISION: {meta.migration_revision}")
            print(f"POSTGRES_VERSION: {meta.postgres_version}")
            return 0

        elif args.command == "restore-test":
            print(f"Iniciando RESTORE-TEST controlado para backup [{args.backup}] en entorno [{env.value}]...")
            res = service.run_restore_test(args.backup, env)
            if res.status.value == "passed":
                print("RESTORE_VALIDATION: PASSED")
                print(f"TEMP_TARGET_SCHEMA: {res.target_database_or_schema}")
                print(f"REVISION_VERIFIED: {res.migration_revision_verified} ({res.restored_revision})")
                print(f"TABLES_VERIFIED: {res.tables_verified_count}/{res.expected_tables_count}")
                print(f"TENANTS_VERIFIED: {res.tenants_verified_count}")
                print(f"DATA_RECORDS_VERIFIED: {res.data_records_verified}")
                print(f"TENANT_ISOLATION_PRESERVED: {res.tenant_isolation_preserved}")
                print(f"CLEANUP_SUCCESSFUL: {res.cleanup_successful}")
                print(f"DURATION_SECONDS: {res.duration_seconds:.2f}")
                return 0
            else:
                print("RESTORE_VALIDATION: FAILED", file=sys.stderr)
                print(f"ERROR: {res.error_message}", file=sys.stderr)
                return 1

        else:
            print(f"Comando desconocido '{args.command}'", file=sys.stderr)
            return 1

    except Exception as exc:
        sanitized = sanitize_error_message(str(exc))
        print(f"ERROR: {sanitized}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
