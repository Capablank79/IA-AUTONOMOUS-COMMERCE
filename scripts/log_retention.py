"""CLI y herramienta canónica de Retención, Rotación y Purga Segura de Logs (P.9).

Proporciona comandos seguros, deterministas y sanitizados:
- status: Informa el estado actual de registros y archivos elegibles por clase y entorno.
- dry-run: Simula y proyecta exactamente qué registros o archivos serían eliminados/rotados sin realizar cambios físicos.
- purge: Ejecuta la rotación y purga segura, transaccional o atómica para el entorno y clase seleccionados.

Seguridad:
- Requiere environment explícito (development, staging, production, testing).
- Respeta aislamiento de tenants (--tenant-id opcional pero estrictamente validado).
- Protección incondicional de Audit Trail (K.1) y alertas vivas (P.8 ACTIVE/ACKNOWLEDGED).
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Optional

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.application.log_retention.log_retention_service import LogRetentionApplicationService
from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.log_retention.models import (
    RetentionClass,
    RetentionPolicy,
    RetentionStatus,
    LogRetentionError,
    LogRetentionSecurityError,
    LogRetentionPolicyError,
)
from src.domain.log_retention.policies import DefaultRetentionPolicyRegistry
from src.infrastructure.persistence.data.json.file_log_retention_store import FileLogRetentionStore
from src.infrastructure.persistence.data.json.metric_log_retention_store import MetricLogRetentionStore
from src.infrastructure.persistence.data.json.alert_log_retention_store import AlertLogRetentionStore
from src.infrastructure.persistence.data.json.trace_log_retention_store import TraceLogRetentionStore
from src.infrastructure.persistence.data.json.audit_log_retention_store import AuditLogRetentionStore
from src.infrastructure.persistence.data.json.metric_repository import JsonMetricRepository
from src.infrastructure.persistence.data.json.production_alert_repository import JsonProductionAlertRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository


def build_default_service(base_dir: Optional[Path] = None) -> LogRetentionApplicationService:
    root = base_dir or _PROJECT_ROOT
    runtime_dir = root / ".runtime"
    runtime_logs_dir = runtime_dir / "logs"
    runtime_traces_dir = runtime_dir / "traces"
    data_dir = root / "data"

    # Repositorios JSON subyacentes
    metric_repo = JsonMetricRepository(base_dir=data_dir)
    alert_repo = JsonProductionAlertRepository(base_dir=data_dir)
    trace_repo = JsonAgentTraceRepository(base_dir=runtime_traces_dir)
    audit_repo = JsonAuditRepository(storage_dir=data_dir / "audit")

    # Stores
    file_store = FileLogRetentionStore(base_log_dir=runtime_logs_dir)
    metric_store = MetricLogRetentionStore(metric_repository=metric_repo)
    alert_store = AlertLogRetentionStore(alert_repository=alert_repo)
    trace_store = TraceLogRetentionStore(trace_repository=trace_repo)
    audit_store = AuditLogRetentionStore(audit_repository=audit_repo)

    registry = DefaultRetentionPolicyRegistry()

    return LogRetentionApplicationService(
        stores=[file_store, metric_store, alert_store, trace_store, audit_store],
        policy_registry=registry,
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IA Autonomous Commerce - Log Retention & Operational Lifecycle CLI (P.9)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcomandos de retención")

    # 1. status
    status_parser = subparsers.add_parser("status", help="Mostrar estado y registros elegibles para retención")
    status_parser.add_argument("-e", "--environment", required=True, help="Entorno (development, staging, production, testing)")
    status_parser.add_argument("-c", "--data-class", required=False, help="Clase de datos (APPLICATION_LOG, MONITORING_SAMPLE, etc.)")
    status_parser.add_argument("-t", "--tenant-id", required=False, help="Tenant ID específico (opcional)")
    status_parser.add_argument("--json", action="store_true", help="Salida en formato JSON")

    # 2. dry-run
    dry_run_parser = subparsers.add_parser("dry-run", help="Simular purga y rotación sin realizar modificaciones")
    dry_run_parser.add_argument("-e", "--environment", required=True, help="Entorno (development, staging, production, testing)")
    dry_run_parser.add_argument("-c", "--data-class", required=True, help="Clase de datos a simular")
    dry_run_parser.add_argument("-t", "--tenant-id", required=False, help="Tenant ID específico (opcional)")
    dry_run_parser.add_argument("--retention-days", type=int, required=False, help="Override de días de retención")
    dry_run_parser.add_argument("--json", action="store_true", help="Salida en formato JSON")

    # 3. purge
    purge_parser = subparsers.add_parser("purge", help="Ejecutar purga y rotación real de logs y evidencias")
    purge_parser.add_argument("-e", "--environment", required=True, help="Entorno (development, staging, production, testing)")
    purge_parser.add_argument("-c", "--data-class", required=True, help="Clase de datos a purgar")
    purge_parser.add_argument("-t", "--tenant-id", required=False, help="Tenant ID específico (opcional)")
    purge_parser.add_argument("--retention-days", type=int, required=False, help="Override de días de retención")
    purge_parser.add_argument("--batch-size", type=int, required=False, help="Tamaño de lote para purga")
    purge_parser.add_argument("--force", action="store_true", help="Confirmar purga sin prompts interactivos")
    purge_parser.add_argument("--json", action="store_true", help="Salida en formato JSON")

    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    service = build_default_service()

    try:
        env = normalize_environment_name(args.environment)
    except Exception as e:
        print(f"Error: Entorno inválido '{args.environment}': {str(e)}", file=sys.stderr)
        return 1

    d_class: Optional[RetentionClass] = None
    if getattr(args, "data_class", None):
        try:
            d_class = RetentionClass(args.data_class.upper())
        except ValueError:
            print(f"Error: Clase de retención desconocida '{args.data_class}'", file=sys.stderr)
            return 1

    tenant_id = getattr(args, "tenant_id", None)

    if args.command == "status":
        summaries = service.get_status(environment=env, data_class=d_class, tenant_id=tenant_id)
        if getattr(args, "json", False):
            print(json.dumps([s.to_dict() for s in summaries], indent=2))
        else:
            print(f"\n=== ESTADO DE RETENCIÓN DE LOGS [Entorno: {env.value.upper()}] ===")
            for s in summaries:
                print(f"- Clase: {s.data_class.value:<20} | Retención: {s.retention_days:>3}d | Total: {s.total_records:>4} | Elegibles Purge: {s.eligible_for_purge:>4} | Tipo: {s.storage_type}")
        return 0

    elif args.command == "dry-run":
        if not d_class:
            print("Error: --data-class es requerido para dry-run", file=sys.stderr)
            return 1
        result = service.execute_retention(
            environment=env,
            data_class=d_class,
            dry_run=True,
            retention_days_override=getattr(args, "retention_days", None),
            tenant_id=tenant_id,
        )
        if getattr(args, "json", False):
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"\n=== DRY-RUN RETENCIÓN [Clase: {d_class.value}, Entorno: {env.value.upper()}] ===")
            print(f"- Escaneados: {result.scanned_count}")
            print(f"- Elegibles para purga: {result.eligible_count}")
            print(f"- Protegidos: {result.protected_count}")
            print(f"- Rotados simulados: {result.rotated_count}")
            print(f"- Omitidos/Corruptos: {result.skipped_count}")
            print(f"- Fecha corte UTC: {result.cutoff_utc.isoformat()}")
            print(f"- Estado: {result.status.value}")
        return 0

    elif args.command == "purge":
        if not d_class:
            print("Error: --data-class es requerido para purge", file=sys.stderr)
            return 1

        result = service.execute_retention(
            environment=env,
            data_class=d_class,
            dry_run=False,
            retention_days_override=getattr(args, "retention_days", None),
            batch_size_override=getattr(args, "batch_size", None),
            tenant_id=tenant_id,
        )
        if getattr(args, "json", False):
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"\n=== RESULTADO DE PURGA DE LOGS [Clase: {d_class.value}, Entorno: {env.value.upper()}] ===")
            print(f"- Purgados: {result.purged_count}")
            print(f"- Rotados: {result.rotated_count}")
            print(f"- Protegidos: {result.protected_count}")
            print(f"- Omitidos/Corruptos: {result.skipped_count}")
            print(f"- Estado: {result.status.value}")
            if result.errors:
                print(f"- Errores: {', '.join(result.errors)}")

        return 0 if result.is_successful else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
