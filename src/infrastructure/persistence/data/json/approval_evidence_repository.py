"""
JSON Persistent Repository for Approval Evidence (Hito N.6 — Approval Policies).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Almacenamiento seguro de evidencias de aprobación con validación de checksum SHA-256.
- Idempotencia estricta para registros idénticos.
- Detección explícita de conflictos y corrupción física.
- Thread-safe mediante RLock.
- Path safety estricto (prevención de path traversal: .., /, \\, :).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Sequence
from contextlib import contextmanager
import threading

from src.domain.approval.models import (
    ApprovalEvidence,
    ApprovalStatus,
    compute_approval_checksum,
)
from src.domain.approval.ports import ApprovalEvidenceRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS, sanitize_security_data

logger = logging.getLogger(__name__)


class JsonApprovalEvidenceRepositoryError(Exception):
    """Excepción base para errores en el repositorio de evidencia de aprobación."""
    pass


class ApprovalEvidenceConflictError(JsonApprovalEvidenceRepositoryError):
    """Se lanza cuando se intenta registrar una evidencia con el mismo ID pero diferente contenido."""
    pass


class CorruptedApprovalEvidenceRecordError(JsonApprovalEvidenceRepositoryError):
    """Se lanza cuando un archivo persistido está corrupto o tiene checksum inválido."""
    pass


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista sanitizando claves sensibles."""
    if isinstance(val, datetime):
        return val.isoformat()
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if str(k) in ("approval_id", "target_action", "target_resource", "requesting_identity_id",
                          "approver_identity_id", "policy_name", "policy_version", "status",
                          "approved_at", "expires_at", "rejection_reason", "correlation_id", "checksum"):
                cleaned[str(k)] = _encode_json_value(v)
                continue
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonApprovalEvidenceRepository(ApprovalEvidenceRepositoryPort):
    """
    Repositorio persistente y atómico en formato JSON para ApprovalEvidence.

    Organización en disco:
      base_dir/
        approval_evidence/
          {approval_id}.json
        index/
          approval_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.evidence_dir = self.base_dir / "approval_evidence"
        self.index_dir = self.base_dir / "index"

        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.index_file = self.index_dir / "approval_index.jsonl"
        self._thread_lock = threading.RLock()
        with self._exclusive_lock():
            self._recover_index_if_needed()

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            yield

    def _get_evidence_file_path(self, approval_id: str) -> Path:
        validate_safe_identifier(approval_id, "approval_id")
        return self.evidence_dir / f"{approval_id}.json"

    def _recover_index_if_needed(self) -> None:
        """Reconstruye el índice a partir de los archivos de evidencias si no existe o está desincronizado."""
        if not self.index_file.exists():
            self._rebuild_index_from_disk()

    def _rebuild_index_from_disk(self) -> None:
        lines: List[str] = []
        for file_path in self.evidence_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                approval_id = data.get("approval_id")
                correlation_id = data.get("correlation_id", "")
                target_action = data.get("target_action", "")
                target_resource = data.get("target_resource", "")
                if approval_id:
                    entry = {
                        "approval_id": approval_id,
                        "correlation_id": correlation_id,
                        "target_action": target_action,
                        "target_resource": target_resource,
                        "file_name": file_path.name,
                    }
                    lines.append(json.dumps(entry, sort_keys=True))
            except Exception as e:
                logger.warning(f"Error reading {file_path} during index rebuild: {e}")

        tmp_index = self.index_dir / "approval_index.jsonl.tmp"
        with open(tmp_index, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(l + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_index, self.index_file)

    def save_evidence(self, evidence: ApprovalEvidence) -> None:
        validate_safe_identifier(evidence.approval_id, "approval_id")
        file_path = self._get_evidence_file_path(evidence.approval_id)

        with self._exclusive_lock():
            if file_path.exists():
                existing = self.get_by_id(evidence.approval_id)
                if existing is not None:
                    if existing.checksum == evidence.checksum:
                        return  # Idempotente
                    raise ApprovalEvidenceConflictError(
                        f"Approval evidence '{evidence.approval_id}' already exists with different checksum."
                    )

            payload = {
                "approval_id": evidence.approval_id,
                "target_action": evidence.target_action,
                "target_resource": evidence.target_resource,
                "requesting_identity_id": evidence.requesting_identity_id,
                "approver_identity_id": evidence.approver_identity_id,
                "policy_name": evidence.policy_name,
                "policy_version": evidence.policy_version,
                "status": evidence.status.value,
                "approved_at": evidence.approved_at.isoformat(),
                "expires_at": evidence.expires_at.isoformat() if evidence.expires_at else None,
                "rejection_reason": evidence.rejection_reason,
                "correlation_id": evidence.correlation_id,
                "checksum": evidence.checksum,
                "metadata": _encode_json_value(dict(evidence.metadata)),
            }

            tmp_file = file_path.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_file, file_path)

            # Actualizar índice
            index_entry = {
                "approval_id": evidence.approval_id,
                "correlation_id": evidence.correlation_id,
                "target_action": evidence.target_action,
                "target_resource": evidence.target_resource,
                "file_name": file_path.name,
            }
            with open(self.index_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(index_entry, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def get_by_id(self, approval_id: str) -> Optional[ApprovalEvidence]:
        file_path = self._get_evidence_file_path(approval_id)
        if not file_path.exists():
            return None

        with self._exclusive_lock():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                raise CorruptedApprovalEvidenceRecordError(
                    f"Corrupted JSON in approval evidence file: {file_path}"
                ) from e

            return self._parse_and_validate(data, file_path)

    def list_by_correlation_id(self, correlation_id: str) -> Sequence[ApprovalEvidence]:
        if not correlation_id:
            return ()
        results: List[ApprovalEvidence] = []
        with self._exclusive_lock():
            if not self.index_file.exists():
                self._rebuild_index_from_disk()

            with open(self.index_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("correlation_id") == correlation_id:
                            app_id = entry.get("approval_id")
                            if app_id:
                                ev = self.get_by_id(app_id)
                                if ev is not None:
                                    results.append(ev)
                    except Exception:
                        continue
        return tuple(results)

    def list_by_target(self, action: str, resource: str) -> Sequence[ApprovalEvidence]:
        results: List[ApprovalEvidence] = []
        with self._exclusive_lock():
            if not self.index_file.exists():
                self._rebuild_index_from_disk()

            with open(self.index_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("target_action") == action and entry.get("target_resource") == resource:
                            app_id = entry.get("approval_id")
                            if app_id:
                                ev = self.get_by_id(app_id)
                                if ev is not None:
                                    results.append(ev)
                    except Exception:
                        continue
        return tuple(results)

    def _parse_and_validate(self, data: Dict[str, Any], file_path: Path) -> ApprovalEvidence:
        try:
            approved_at_str = data.get("approved_at")
            approved_at = datetime.fromisoformat(approved_at_str) if approved_at_str else datetime.now(timezone.utc)

            expires_at_str = data.get("expires_at")
            expires_at = datetime.fromisoformat(expires_at_str) if expires_at_str else None

            persisted_checksum = data.get("checksum", "")
            raw_meta = data.get("metadata", {})

            expected_checksum = compute_approval_checksum(
                approval_id=data["approval_id"],
                target_action=data["target_action"],
                target_resource=data["target_resource"],
                requesting_identity_id=data["requesting_identity_id"],
                approver_identity_id=data["approver_identity_id"],
                policy_name=data["policy_name"],
                policy_version=data["policy_version"],
                status=data["status"],
                approved_at=approved_at,
                expires_at=expires_at,
                metadata=raw_meta,
            )

            if persisted_checksum != expected_checksum:
                raise CorruptedApprovalEvidenceRecordError(
                    f"Integrity checksum mismatch for approval evidence '{data.get('approval_id')}' in {file_path}."
                )

            return ApprovalEvidence(
                approval_id=data["approval_id"],
                target_action=data["target_action"],
                target_resource=data["target_resource"],
                requesting_identity_id=data["requesting_identity_id"],
                approver_identity_id=data["approver_identity_id"],
                policy_name=data["policy_name"],
                policy_version=data["policy_version"],
                status=ApprovalStatus(data["status"]),
                approved_at=approved_at,
                expires_at=expires_at,
                rejection_reason=data.get("rejection_reason"),
                correlation_id=data.get("correlation_id", ""),
                checksum=persisted_checksum,
                metadata=raw_meta,
            )
        except CorruptedApprovalEvidenceRecordError:
            raise
        except Exception as e:
            raise CorruptedApprovalEvidenceRecordError(
                f"Failed to parse approval evidence record in {file_path}: {e}"
            ) from e
