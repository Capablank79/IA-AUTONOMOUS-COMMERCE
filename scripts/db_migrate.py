"""CLI y ejecutor de migraciones de base de datos para PostgreSQL (P.3).

Proporciona comandos de gestión de esquema deterministas y reproducibles:
- upgrade [revision]: Aplica migraciones hasta la revisión indicada (por defecto 'head').
- current: Muestra la revisión actual de la base de datos.
- history: Muestra la historia de revisiones disponibles.
- check: Verifica si la base de datos está al día con el HEAD del código (exit 0 si sincronizado, 1 si pendiente).
- downgrade <revision>: Revierte migraciones hasta la revisión especificada.

Seguridad:
- Todas las salidas ocultan contraseñas y secretos (sanitización estricta).
- Utiliza Alembic y SQLAlchemy para serialización atómica y locking advisory.
"""

import argparse
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConfigError,
    DatabaseConnectionError,
    sanitize_error_message,
)


def get_alembic_config(config_path: Optional[str] = None) -> Config:
    """Carga y prepara la configuración de Alembic inyectando la URL de DatabaseConfig."""
    project_root = Path(__file__).resolve().parent.parent
    ini_file = config_path or str(project_root / "alembic.ini")
    
    if not os.path.exists(ini_file):
        raise FileNotFoundError(f"Alembic configuration file not found at {ini_file}")

    alembic_cfg = Config(ini_file)
    # Cargar DatabaseConfig para inyectar URL de conexión real
    db_config = DatabaseConfig.from_env()
    alembic_cfg.set_main_option("sqlalchemy.url", db_config.sqlalchemy_url)
    alembic_cfg.set_main_option("script_location", str(project_root / "src" / "infrastructure" / "persistence" / "database" / "alembic"))
    return alembic_cfg


def get_current_revision(alembic_cfg: Config) -> Optional[str]:
    """Obtiene la revisión actual aplicada en la base de datos de manera segura."""
    db_config = DatabaseConfig.from_env()
    engine = create_engine(db_config.sqlalchemy_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return context.get_current_revision()
    finally:
        engine.dispose()


def get_head_revision(alembic_cfg: Config) -> Optional[str]:
    """Obtiene la revisión HEAD definida en los scripts de migración."""
    script = ScriptDirectory.from_config(alembic_cfg)
    return script.get_current_head()


def check_schema_compatibility(alembic_cfg: Optional[Config] = None) -> Dict[str, Any]:
    """Comprueba el estado del esquema vs el código fuente.
    
    Retorna un diccionario con current_revision, head_revision, is_up_to_date y error_message.
    """
    try:
        cfg = alembic_cfg or get_alembic_config()
        current_rev = get_current_revision(cfg)
        head_rev = get_head_revision(cfg)
        
        is_up_to_date = (current_rev == head_rev) and (head_rev is not None)
        return {
            "status": "ok",
            "current_revision": current_rev,
            "head_revision": head_rev,
            "is_up_to_date": is_up_to_date,
            "error_message": None,
        }
    except Exception as exc:
        sanitized = sanitize_error_message(str(exc))
        return {
            "status": "error",
            "current_revision": None,
            "head_revision": None,
            "is_up_to_date": False,
            "error_message": sanitized,
        }


def run_upgrade(revision: str = "head", config_path: Optional[str] = None) -> int:
    """Aplica migraciones hacia adelante de manera idempotente."""
    try:
        cfg = get_alembic_config(config_path)
        db_config = DatabaseConfig.from_env()
        print(f"[MIGRATION] Target database: {db_config.sanitized_dsn}")
        print(f"[MIGRATION] Upgrading to: {revision}...")
        command.upgrade(cfg, revision)
        current = get_current_revision(cfg)
        print(f"[MIGRATION] Upgrade SUCCESSFUL. Current revision: {current}")
        return 0
    except Exception as exc:
        sanitized = sanitize_error_message(str(exc))
        print(f"[MIGRATION_ERROR] Upgrade failed: {sanitized}", file=sys.stderr)
        return 1


def run_downgrade(revision: str, config_path: Optional[str] = None) -> int:
    """Revierte migraciones hasta la revisión indicada."""
    try:
        cfg = get_alembic_config(config_path)
        db_config = DatabaseConfig.from_env()
        print(f"[MIGRATION] Target database: {db_config.sanitized_dsn}")
        print(f"[MIGRATION] Downgrading to: {revision}...")
        command.downgrade(cfg, revision)
        current = get_current_revision(cfg)
        print(f"[MIGRATION] Downgrade SUCCESSFUL. Current revision: {current}")
        return 0
    except Exception as exc:
        sanitized = sanitize_error_message(str(exc))
        print(f"[MIGRATION_ERROR] Downgrade failed: {sanitized}", file=sys.stderr)
        return 1


def run_current(config_path: Optional[str] = None) -> int:
    """Muestra la versión actual del esquema."""
    try:
        cfg = get_alembic_config(config_path)
        current = get_current_revision(cfg)
        head = get_head_revision(cfg)
        print(f"Current DB revision: {current or '(none / empty DB)'}")
        print(f"Head script revision: {head or '(none)'}")
        return 0
    except Exception as exc:
        sanitized = sanitize_error_message(str(exc))
        print(f"[MIGRATION_ERROR] Current check failed: {sanitized}", file=sys.stderr)
        return 1


def run_history(config_path: Optional[str] = None) -> int:
    """Muestra el historial de migraciones disponibles."""
    try:
        cfg = get_alembic_config(config_path)
        command.history(cfg)
        return 0
    except Exception as exc:
        sanitized = sanitize_error_message(str(exc))
        print(f"[MIGRATION_ERROR] History failed: {sanitized}", file=sys.stderr)
        return 1


def run_check(config_path: Optional[str] = None) -> int:
    """Verifica si la base de datos está al día. Exit 0 si OK, exit 1 si desactualizada o error."""
    result = check_schema_compatibility(get_alembic_config(config_path) if config_path else None)
    if result["status"] != "ok":
        print(f"[MIGRATION_CHECK] Error verifying schema: {result['error_message']}", file=sys.stderr)
        return 1
    
    if result["is_up_to_date"]:
        print(f"[MIGRATION_CHECK] Schema is UP TO DATE (revision: {result['current_revision']}).")
        return 0
    else:
        print(
            f"[MIGRATION_CHECK] Schema is OUT OF DATE! Current: {result['current_revision']}, Head: {result['head_revision']}",
            file=sys.stderr,
        )
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Database Migration CLI (P.3 — Database Migrations)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", "-c", help="Path to alembic.ini config file", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True, help="Migration command to run")

    # Upgrade
    p_up = subparsers.add_parser("upgrade", help="Upgrade database schema to a target revision")
    p_up.add_argument("revision", nargs="?", default="head", help="Target revision (default: head)")

    # Downgrade
    p_down = subparsers.add_parser("downgrade", help="Downgrade database schema to a target revision")
    p_down.add_argument("revision", help="Target revision (e.g. base or revision_id)")

    # Current
    subparsers.add_parser("current", help="Show current revision of the database")

    # History
    subparsers.add_parser("history", help="List available migration revisions in chronological order")

    # Check
    subparsers.add_parser("check", help="Check if database schema is up-to-date with HEAD (exit 0 if yes, 1 if not)")

    args = parser.parse_args()

    if args.command == "upgrade":
        sys.exit(run_upgrade(args.revision, args.config))
    elif args.command == "downgrade":
        sys.exit(run_downgrade(args.revision, args.config))
    elif args.command == "current":
        sys.exit(run_current(args.config))
    elif args.command == "history":
        sys.exit(run_history(args.config))
    elif args.command == "check":
        sys.exit(run_check(args.config))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
