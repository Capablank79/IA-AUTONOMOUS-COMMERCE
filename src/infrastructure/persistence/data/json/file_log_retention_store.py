"""
Adaptador de Retención para Logs en Filesystem (.log, .jsonl, rotated files) (P.9).

Soporta:
- APPLICATION_LOG, ACCESS_LOG, ERROR_LOG
- Rotación determinista por tamaño (max_file_size_bytes) y límite de backups (backup_count).
- Purga basada en timestamp estructurado o fecha embebida en filename rotated.
- Protección estricta contra Path Traversal y symlinks fuera del directorio de logs.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.log_retention.models import (
    RetentionAction,
    RetentionClass,
    RetentionDecision,
    RetentionPolicy,
    RetentionResult,
    RetentionStatus,
    RetentionStatusSummary,
    LogRetentionError,
    LogRetentionSecurityError,
)
from src.domain.log_retention.ports import LogRetentionStorePort
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


class FileLogRetentionStore(LogRetentionStorePort):
    """
    Gestiona la rotación y purga de archivos de log estructurados en disco.
    Ruta estándar: {base_log_dir} / {environment} / [{tenant_id}] / *.log | *.jsonl
    """

    SUPPORTED = (
        RetentionClass.APPLICATION_LOG,
        RetentionClass.ACCESS_LOG,
        RetentionClass.ERROR_LOG,
    )

    def __init__(self, base_log_dir: Path) -> None:
        self.base_log_dir = Path(base_log_dir).resolve()
        self.base_log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def supported_classes(self) -> Sequence[RetentionClass]:
        return self.SUPPORTED

    def _resolve_target_dir(self, environment: ApplicationEnvironment, tenant_id: Optional[str] = None) -> Path:
        env = normalize_environment_name(environment)
        target = self.base_log_dir / env.value
        if tenant_id:
            try:
                validate_safe_identifier(tenant_id, "tenant_id")
            except ValueError as ve:
                raise LogRetentionSecurityError(str(ve))
            target = target / "tenants" / tenant_id

        # Path traversal guard
        target_resolved = target.resolve()
        try:
            target_resolved.relative_to(self.base_log_dir)
        except ValueError:
            raise LogRetentionSecurityError(f"Path traversal detectado fuera de log root: {target}")

        target_resolved.mkdir(parents=True, exist_ok=True)
        return target_resolved

    def _parse_timestamp_from_file_or_content(self, file_path: Path) -> Optional[datetime]:
        """
        Intenta obtener timestamp estructurado del contenido o del nombre del archivo rotado.
        Evita depender únicamente de mtime que puede ser alterado por operaciones del SO.
        """
        name = file_path.name
        # Patrón rotated: app_2026-03-20T10-00-00Z.log o app.log.2026-03-20
        match = re.search(r"(\d{4}-\d{2}-\d{2}[T_]?\d{0,2}-?\d{0,2}-?\d{0,2})", name)
        if match:
            raw_ts = match.group(1).replace("_", "T").replace("-", ":")
            # Probar parsing
            for fmt in ("%Y:%m:%dT%H:%M:%SZ", "%Y:%m:%dT%H:%M:%S", "%Y:%m:%d"):
                try:
                    # Restaurar separadores estándar
                    clean_str = match.group(1)
                    if "T" in clean_str:
                        parts = clean_str.split("T")
                        date_part = parts[0]
                        time_part = parts[1].replace("-", ":").replace("Z", "")
                        dt = datetime.fromisoformat(f"{date_part}T{time_part}").replace(tzinfo=timezone.utc)
                        return dt
                    else:
                        dt = datetime.strptime(clean_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        return dt
                except Exception:
                    pass

        # Si es .jsonl o .log estructurado, leer la primera o última línea
        if file_path.is_file() and file_path.stat().st_size > 0:
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    first_line = f.readline().strip()
                    if first_line.startswith("{") and first_line.endswith("}"):
                        data = json.loads(first_line)
                        for key in ("timestamp", "occurred_at", "created_at", "time"):
                            if key in data and isinstance(data[key], str):
                                try:
                                    dt = datetime.fromisoformat(data[key].replace("Z", "+00:00"))
                                    if dt.tzinfo is None:
                                        dt = dt.replace(tzinfo=timezone.utc)
                                    return dt
                                except Exception:
                                    pass
            except Exception:
                pass

        # Fallback seguro: mtime como datetime UTC
        try:
            mtime = file_path.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=timezone.utc)
        except Exception:
            return None

    def get_status_summary(
        self,
        policy: RetentionPolicy,
        now: Optional[datetime] = None,
    ) -> RetentionStatusSummary:
        current_time = now or datetime.now(timezone.utc)
        target_dir = self._resolve_target_dir(policy.environment, policy.tenant_id)
        decisions = self.evaluate_retention(policy, now=current_time)

        total_records = len(decisions)
        eligible_for_purge = sum(1 for d in decisions if d.action == RetentionAction.PURGE)

        timestamps = [d.occurred_at for d in decisions if d.occurred_at is not None]
        oldest_record_at = min(timestamps) if timestamps else None
        newest_record_at = max(timestamps) if timestamps else None

        return RetentionStatusSummary(
            environment=policy.environment,
            data_class=policy.data_class,
            retention_days=policy.retention_days,
            total_records=total_records,
            eligible_for_purge=eligible_for_purge,
            oldest_record_at=oldest_record_at,
            newest_record_at=newest_record_at,
            last_purge_at=None,
            last_purge_result=None,
            storage_type="filesystem",
            location_summary=str(target_dir),
            tenant_id=policy.tenant_id,
        )

    def evaluate_retention(
        self,
        policy: RetentionPolicy,
        now: Optional[datetime] = None,
    ) -> Sequence[RetentionDecision]:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        target_dir = self._resolve_target_dir(policy.environment, policy.tenant_id)
        cutoff = policy.calculate_cutoff(current_time)
        decisions: List[RetentionDecision] = []

        with self._lock:
            # Filtrar archivos según clase
            file_patterns = {
                RetentionClass.APPLICATION_LOG: ["app*.log*", "application*.log*", "*.log", "*.jsonl"],
                RetentionClass.ACCESS_LOG: ["access*.log*", "http*.log*", "web*.log*"],
                RetentionClass.ERROR_LOG: ["error*.log*", "err*.log*"],
            }
            patterns = file_patterns.get(policy.data_class, ["*.log", "*.jsonl"])
            all_files: Set[Path] = set()

            for pattern in patterns:
                for f in target_dir.glob(pattern):
                    if f.is_file():
                        all_files.add(f)

            # Ordenar archivos por nombre determinista
            sorted_files = sorted(list(all_files), key=lambda p: p.name)

            for file_path in sorted_files:
                # Verificar symlinks peligrosos
                if file_path.is_symlink():
                    real_target = file_path.resolve()
                    try:
                        real_target.relative_to(self.base_log_dir)
                    except ValueError:
                        decisions.append(
                            RetentionDecision(
                                item_id=file_path.name,
                                data_class=policy.data_class,
                                action=RetentionAction.SKIP_CORRUPT,
                                reason="Symlink apunta fuera del log root seguro",
                                occurred_at=None,
                                cutoff_date=cutoff,
                                tenant_id=policy.tenant_id,
                                environment=policy.environment,
                            )
                        )
                        continue

                # Parsear timestamp
                file_ts = self._parse_timestamp_from_file_or_content(file_path)
                if file_ts is None:
                    decisions.append(
                        RetentionDecision(
                            item_id=file_path.name,
                            data_class=policy.data_class,
                            action=RetentionAction.SKIP_CORRUPT,
                            reason="No se pudo determinar timestamp válido para el archivo",
                            occurred_at=None,
                            cutoff_date=cutoff,
                            tenant_id=policy.tenant_id,
                            environment=policy.environment,
                        )
                    )
                    continue

                # 1. Comprobar si requiere rotación por tamaño (si es archivo activo no rotado)
                try:
                    file_size = file_path.stat().st_size
                except OSError:
                    file_size = 0

                is_rotated_file = bool(re.search(r"(\.rotated|\.\d+|\d{4}-\d{2}-\d{2})", file_path.name))

                if not is_rotated_file and file_size >= policy.max_file_size_bytes:
                    decisions.append(
                        RetentionDecision(
                            item_id=file_path.name,
                            data_class=policy.data_class,
                            action=RetentionAction.ROTATE,
                            reason=f"Tamaño ({file_size} B) excede el umbral de rotación ({policy.max_file_size_bytes} B)",
                            occurred_at=file_ts,
                            cutoff_date=cutoff,
                            metadata={"file_size": file_size},
                            tenant_id=policy.tenant_id,
                            environment=policy.environment,
                        )
                    )
                    continue

                # 2. Comprobar si es eligible para purge por antigüedad
                if file_ts < cutoff:
                    decisions.append(
                        RetentionDecision(
                            item_id=file_path.name,
                            data_class=policy.data_class,
                            action=RetentionAction.PURGE,
                            reason=f"Timestamp ({file_ts.isoformat()}) anterior al corte UTC ({cutoff.isoformat()})",
                            occurred_at=file_ts,
                            cutoff_date=cutoff,
                            metadata={"file_size": file_size},
                            tenant_id=policy.tenant_id,
                            environment=policy.environment,
                        )
                    )
                else:
                    decisions.append(
                        RetentionDecision(
                            item_id=file_path.name,
                            data_class=policy.data_class,
                            action=RetentionAction.KEEP,
                            reason=f"Timestamp ({file_ts.isoformat()}) dentro de la ventana de retención ({policy.retention_days} días)",
                            occurred_at=file_ts,
                            cutoff_date=cutoff,
                            metadata={"file_size": file_size},
                            tenant_id=policy.tenant_id,
                            environment=policy.environment,
                        )
                    )

        return tuple(decisions)

    def execute_purge(
        self,
        policy: RetentionPolicy,
        decisions: Sequence[RetentionDecision],
    ) -> RetentionResult:
        current_time = datetime.now(timezone.utc)
        target_dir = self._resolve_target_dir(policy.environment, policy.tenant_id)
        errors: List[str] = []
        purged_count = 0
        rotated_count = 0
        protected_count = 0
        skipped_count = 0

        with self._lock:
            for decision in decisions:
                file_path = target_dir / decision.item_id

                if decision.action == RetentionAction.SKIP_CORRUPT:
                    skipped_count += 1
                    continue

                if decision.action == RetentionAction.PROTECT:
                    protected_count += 1
                    continue

                if decision.action == RetentionAction.ROTATE:
                    if not file_path.is_file():
                        continue
                    try:
                        timestamp_str = current_time.strftime("%Y%m%dT%H%M%SZ")
                        rotated_name = f"{file_path.stem}_{timestamp_str}{file_path.suffix}"
                        rotated_path = target_dir / rotated_name
                        # Atómicamente renombrar
                        shutil.move(str(file_path), str(rotated_path))
                        # Crear nuevo archivo vacío
                        file_path.touch()
                        rotated_count += 1
                    except Exception as e:
                        errors.append(f"Error al rotar {decision.item_id}: {str(e)}")

                elif decision.action == RetentionAction.PURGE:
                    if not file_path.exists():
                        continue
                    try:
                        if file_path.is_file():
                            file_path.unlink()
                            purged_count += 1
                    except Exception as e:
                        errors.append(f"Error al eliminar {decision.item_id}: {str(e)}")

        status = RetentionStatus.SUCCESS if len(errors) == 0 else RetentionStatus.PARTIAL
        if len(errors) > 0 and purged_count == 0 and rotated_count == 0:
            status = RetentionStatus.FAILED

        return RetentionResult(
            environment=policy.environment,
            data_class=policy.data_class,
            dry_run=False,
            scanned_count=len(decisions),
            eligible_count=sum(1 for d in decisions if d.action == RetentionAction.PURGE),
            purged_count=purged_count,
            rotated_count=rotated_count,
            protected_count=protected_count,
            skipped_count=skipped_count,
            cutoff_utc=policy.calculate_cutoff(current_time),
            started_at=current_time,
            completed_at=datetime.now(timezone.utc),
            status=status,
            decisions=tuple(decisions),
            errors=tuple(errors),
            tenant_id=policy.tenant_id,
        )
