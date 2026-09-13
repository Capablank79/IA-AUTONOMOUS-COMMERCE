"""CLI y Runner canónico de Disaster Recovery para IA Autonomous Commerce (P.5).

Proporciona comandos seguros, deterministas y sanitizados:
- plan: Genera y visualiza un plan estructurado de recuperación ante desastres según escenario y entorno.
- validate: Valida conectividad del recovery target, coherencia de backups y compatibilidad de migraciones Alembic.
- simulate: Ejecuta una simulación completa de recuperación end-to-end contra PostgreSQL real con medición cuantitativa de RPO y RTO.

Seguridad:
- Cero exposición de contraseñas o datos sensibles en CLI, logs o archivos.
- Verificación estricta de aislamiento: source_database != recovery_target.
- Requiere environment explícito (development, staging, production, testing).
"""

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Optional

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.domain.deployment.models import (
    ApplicationEnvironment,
    normalize_environment_name,
)
from src.domain.disaster_recovery.models import (
    DisasterRecoveryError,
    DisasterRecoveryPolicy,
    DisasterScenarioType,
    RecoveryStatus,
)
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConfigError,
    sanitize_error_message,
)
from src.infrastructure.persistence.database.disaster_recovery_service import (
    DisasterRecoveryService,
)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IA Autonomous Commerce - Disaster Recovery Tooling (P.5)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcomandos de Disaster Recovery")

    # plan
    plan_parser = subparsers.add_parser("plan", help="Generar un plan determinista de recuperación")
    plan_parser.add_argument(
        "-e", "--environment",
        required=True,
        help="Entorno de destino (development, staging, production, testing)",
    )
    plan_parser.add_argument(
        "-s", "--scenario",
        default="database_loss",
        choices=[s.value for s in DisasterScenarioType],
        help="Escenario de desastre a simular/planificar (por defecto: database_loss)",
    )
    plan_parser.add_argument(
        "--backup-id",
        required=False,
        help="ID específico del backup a utilizar (opcional)",
    )

    # validate
    val_parser = subparsers.add_parser("validate", help="Validar conectividad del target y backups")
    val_parser.add_argument(
        "-e", "--environment",
        required=True,
        help="Entorno a validar (development, staging, production, testing)",
    )
    val_parser.add_argument(
        "--backup-id",
        required=False,
        help="ID específico del backup a verificar (opcional)",
    )

    # simulate
    sim_parser = subparsers.add_parser("simulate", help="Ejecutar simulación real de DR y medir RPO/RTO")
    sim_parser.add_argument(
        "-e", "--environment",
        required=True,
        help="Entorno para la simulación (development, staging, production, testing)",
    )
    sim_parser.add_argument(
        "-s", "--scenario",
        default="database_loss",
        choices=[s.value for s in DisasterScenarioType],
        help="Escenario de desastre a simular (por defecto: database_loss)",
    )
    sim_parser.add_argument(
        "--backup-id",
        required=False,
        help="ID específico del backup a restaurar (opcional)",
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
        scenario = DisasterScenarioType(args.scenario) if hasattr(args, "scenario") else DisasterScenarioType.DATABASE_LOSS
    except ValueError:
        scenario = DisasterScenarioType.DATABASE_LOSS

    try:
        service = DisasterRecoveryService()
    except Exception as exc:
        sanitized = sanitize_error_message(str(exc))
        print(f"ERROR al inicializar servicio de Disaster Recovery: {sanitized}", file=sys.stderr)
        return 1

    try:
        if args.command == "plan":
            plan = service.generate_plan(
                scenario=scenario,
                environment=env,
                custom_backup_id=args.backup_id,
            )
            print("==================================================")
            print(f"DISASTER RECOVERY PLAN: {plan.plan_id}")
            print("==================================================")
            print(f"SCENARIO: {plan.scenario.value}")
            print(f"ENVIRONMENT: {plan.environment.value}")
            print(f"SOURCE_DATABASE: {plan.source_database}")
            print(f"RECOVERY_TARGET: {plan.recovery_target}")
            print(f"BACKUP_SELECTED: {plan.backup_metadata.backup_id if plan.backup_metadata else 'None'}")
            if plan.estimated_rpo_seconds is not None:
                print(f"ESTIMATED_RPO: {plan.estimated_rpo_seconds:.2f}s ({plan.estimated_rpo_seconds / 60:.1f} min)")
            print(f"TARGET_RTO_MAX: {plan.estimated_rto_seconds:.2f}s ({plan.estimated_rto_seconds / 60:.1f} min)")
            print("\nOPERATIONAL STEPS:")
            for s in plan.steps:
                print(f"  {s}")
            print("==================================================")
            return 0

        elif args.command == "validate":
            print(f"Validando estado de Disaster Recovery para entorno [{env.value}]...")
            # 1. Target connection check
            check = service.verify_target_connection(service.config)
            print(f"TARGET_CONNECTION_CHECK: PASS ({check['database']} as {check['user']})")

            # 2. Backups check
            backups = service.backup_service.list_backups(env)
            print(f"AVAILABLE_BACKUPS_COUNT: {len(backups)}")
            if args.backup_id:
                verified = service.backup_service.verify_backup(args.backup_id, env)
                print(f"BACKUP_INTEGRITY_CHECK: PASS ({verified.backup_id}, revision={verified.migration_revision})")
            elif backups:
                latest = backups[0]
                verified = service.backup_service.verify_backup(latest.file_name, env)
                print(f"LATEST_BACKUP_INTEGRITY_CHECK: PASS ({verified.backup_id}, revision={verified.migration_revision})")
            else:
                print("WARNING: No backups found for environment. Run 'python scripts/db_backup.py create' first.")

            return 0

        elif args.command == "simulate":
            print("==================================================")
            print(f"INICIANDO SIMULACIÓN DE DISASTER RECOVERY [{scenario.value}]")
            print(f"ENVIRONMENT: {env.value}")
            print("==================================================")
            
            result = service.execute_recovery_simulation(
                environment=env,
                scenario=scenario,
                custom_backup_id=args.backup_id,
            )

            print(f"EXECUTION_ID: {result.execution_id}")
            print(f"STATUS: {result.status.value.upper()}")
            print(f"TARGET_CONNECTION_CHECK: {'PASS' if result.target_connection_verified else 'FAIL'}")
            print(f"BACKUP_VERIFIED: {'PASS' if result.backup_verified else 'FAIL'}")
            print(f"RESTORE_VERIFIED: {'PASS' if result.restore_verified else 'FAIL'}")
            print(f"MIGRATION_REVISION_VERIFIED: {'PASS' if result.migration_revision_verified else 'FAIL'} ({result.restored_revision})")
            print(f"TABLES_RESTORED: {result.tables_restored_count}/{result.expected_tables_count}")
            print(f"TENANTS_RESTORED: {result.tenants_restored_count}")
            if result.actual_rpo_seconds is not None:
                print(f"MEASURED_RPO_SECONDS: {result.actual_rpo_seconds:.2f}s (SLA Compliant: {result.rpo_compliant})")
            print(f"MEASURED_RTO_SECONDS: {result.actual_rto_seconds:.2f}s (SLA Compliant: {result.rto_compliant})")
            print(f"CLEANUP_SUCCESSFUL: {'YES' if result.cleanup_successful else 'NO'}")

            if result.status == RecoveryStatus.COMPLETED:
                print("==================================================")
                print("DR_SIMULATION: PASS (Recovery validated, RPO/RTO compliant, no data leak)")
                print("==================================================")
                return 0
            else:
                print("==================================================")
                print(f"DR_SIMULATION: FAIL - Error: {result.error_message}", file=sys.stderr)
                print("==================================================")
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
