"""
Implementación JSON persistente, atómica y crash-safe para Emergency Stop (Hito N.11).

Garantiza:
- Atomic write (.tmp -> os.replace) con fsync.
- Thread-safety estricto mediante RLock.
- Verificación de checksum SHA-256 para detección de manipulación o corrupción (Tamper evidence).
- Resiliencia ante reinicios del proceso: recarga determinista de todos los registros.
- Fail-safe estricto: si el archivo está corrupto o manipulado, se detecta y no se asume INACTIVE silenciosamente.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Dict, List, Optional, Sequence, Any

from src.domain.emergency_stop.models import (
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
    EmergencyStopReasonCode,
    compute_emergency_stop_checksum,
)
from src.domain.emergency_stop.ports import EmergencyStopRepositoryPort
from src.domain.security.models import validate_safe_identifier


class CorruptedEmergencyStopRecordError(Exception):
    """Lanzada cuando un registro persistido de Emergency Stop no es válido o su checksum no coincide."""
    pass


class JsonEmergencyStopRepository(EmergencyStopRepositoryPort):
    """
    Repositorio JSON persistente y crash-safe para Emergency Stop.
    """

    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self._lock = threading.RLock()
        self._records: Dict[str, EmergencyStopRecord] = {}
        self._is_corrupt: bool = False
        self._init_storage()

    @property
    def is_corrupt(self) -> bool:
        """Indica si el almacenamiento en disco está corrupto o manipulado."""
        return self._is_corrupt

    def _init_storage(self) -> None:
        """Inicializa directorios y carga los registros existentes desde disco."""
        with self._lock:
            if not self.file_path.parent.exists():
                self.file_path.parent.mkdir(parents=True, exist_ok=True)

            if not self.file_path.exists():
                self._persist_to_disk()
            else:
                try:
                    self._load_from_disk()
                except CorruptedEmergencyStopRecordError:
                    self._is_corrupt = True
                    # No re-lanzar para permitir que la capa de servicio detecte is_corrupt y aplique Fail-Safe

    def _load_from_disk(self) -> None:
        """Carga y valida los registros desde el archivo JSON con verificación de checksum."""
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    self._records = {}
                    return
                data = json.loads(content)

            if not isinstance(data, list):
                raise CorruptedEmergencyStopRecordError("Emergency stop store root must be a JSON array")

            loaded: Dict[str, EmergencyStopRecord] = {}
            for item in data:
                if not isinstance(item, dict):
                    raise CorruptedEmergencyStopRecordError("Each item in emergency stop store must be an object")

                stop_id = item.get("stop_id")
                if not stop_id:
                    raise CorruptedEmergencyStopRecordError("Record missing stop_id")

                # Reconstruir fechas timezone-aware
                activated_at_str = item.get("activated_at")
                activated_at = datetime.fromisoformat(activated_at_str) if activated_at_str else datetime.now(timezone.utc)
                if activated_at.tzinfo is None:
                    activated_at = activated_at.replace(tzinfo=timezone.utc)

                expires_at = None
                if item.get("expires_at"):
                    expires_at = datetime.fromisoformat(item["expires_at"])
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)

                deactivated_at = None
                if item.get("deactivated_at"):
                    deactivated_at = datetime.fromisoformat(item["deactivated_at"])
                    if deactivated_at.tzinfo is None:
                        deactivated_at = deactivated_at.replace(tzinfo=timezone.utc)

                record = EmergencyStopRecord(
                    stop_id=stop_id,
                    scope=EmergencyStopScope(item.get("scope", "GLOBAL")),
                    state=EmergencyStopState(item.get("state", "UNKNOWN")),
                    reason_code=EmergencyStopReasonCode(item.get("reason_code", "MANUAL_OPERATOR_HALT")),
                    reason_details=item.get("reason_details", ""),
                    activated_by_identity_id=item.get("activated_by_identity_id", "system"),
                    activated_at=activated_at,
                    target_id=item.get("target_id"),
                    expires_at=expires_at,
                    deactivated_by_identity_id=item.get("deactivated_by_identity_id"),
                    deactivated_at=deactivated_at,
                    policy_version=item.get("policy_version", "1.0.0"),
                    allow_read_only=bool(item.get("allow_read_only", True)),
                    metadata=item.get("metadata", {}),
                    checksum=item.get("checksum"),
                )

                # Validar integridad SHA-256
                payload_for_check = {
                    "stop_id": record.stop_id,
                    "scope": record.scope.value,
                    "target_id": record.target_id,
                    "state": record.state.value,
                    "reason_code": record.reason_code.value,
                    "reason_details": record.reason_details,
                    "activated_by_identity_id": record.activated_by_identity_id,
                    "activated_at": record.activated_at.isoformat() if record.activated_at else None,
                    "expires_at": record.expires_at.isoformat() if record.expires_at else None,
                    "deactivated_by_identity_id": record.deactivated_by_identity_id,
                    "deactivated_at": record.deactivated_at.isoformat() if record.deactivated_at else None,
                    "policy_version": record.policy_version,
                    "allow_read_only": record.allow_read_only,
                }
                expected_hash = compute_emergency_stop_checksum(payload_for_check)
                if record.checksum != expected_hash:
                    self._is_corrupt = True
                    raise CorruptedEmergencyStopRecordError(
                        f"Checksum verification failed for record {stop_id}: stored {record.checksum} != expected {expected_hash}"
                    )

                loaded[stop_id] = record

            self._records = loaded
            self._is_corrupt = False

        except Exception as e:
            self._is_corrupt = True
            raise CorruptedEmergencyStopRecordError(f"Failed to load emergency stop store from disk: {e}") from e

    def _persist_to_disk(self) -> None:
        """Escribe todos los registros a disco de forma atómica (tmp file -> fsync -> os.replace)."""
        tmp_path = self.file_path.with_suffix(".tmp")
        raw_list = []
        for rec in self._records.values():
            raw_list.append({
                "stop_id": rec.stop_id,
                "scope": rec.scope.value,
                "target_id": rec.target_id,
                "state": rec.state.value,
                "reason_code": rec.reason_code.value,
                "reason_details": rec.reason_details,
                "activated_by_identity_id": rec.activated_by_identity_id,
                "activated_at": rec.activated_at.isoformat() if rec.activated_at else None,
                "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
                "deactivated_by_identity_id": rec.deactivated_by_identity_id,
                "deactivated_at": rec.deactivated_at.isoformat() if rec.deactivated_at else None,
                "policy_version": rec.policy_version,
                "allow_read_only": rec.allow_read_only,
                "metadata": dict(rec.metadata),
                "checksum": rec.checksum,
            })

        data_str = json.dumps(raw_list, indent=2, sort_keys=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data_str)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, self.file_path)

    def save(self, record: EmergencyStopRecord) -> None:
        """Guarda o actualiza un registro en memoria y disco atómicamente."""
        with self._lock:
            self._records[record.stop_id] = record
            self._persist_to_disk()

    def get_by_id(self, stop_id: str) -> Optional[EmergencyStopRecord]:
        """Obtiene un registro por su ID."""
        with self._lock:
            return self._records.get(stop_id)

    def list_records(
        self,
        scope: Optional[EmergencyStopScope] = None,
        target_id: Optional[str] = None,
        state: Optional[EmergencyStopState] = None,
    ) -> Sequence[EmergencyStopRecord]:
        """Lista registros aplicando filtros deterministas."""
        with self._lock:
            res = list(self._records.values())
            if scope is not None:
                res = [r for r in res if r.scope == scope]
            if target_id is not None:
                res = [r for r in res if r.target_id == target_id]
            if state is not None:
                res = [r for r in res if r.state == state]
            # Orden cronológico determinista
            res.sort(key=lambda r: (r.activated_at.timestamp() if r.activated_at else 0.0, r.stop_id))
            return tuple(res)

    def list_active_records(self, current_time: datetime) -> Sequence[EmergencyStopRecord]:
        """Retorna todos los registros activos y no expirados."""
        with self._lock:
            active = [r for r in self._records.values() if r.is_active_at(current_time)]
            active.sort(key=lambda r: (r.activated_at.timestamp() if r.activated_at else 0.0, r.stop_id))
            return tuple(active)
