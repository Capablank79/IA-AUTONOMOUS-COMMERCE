"""
Persistencia JSON atómica, versionada e íntegra para Duplicate Detection L.7.

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Inmutabilidad estricta por policy_id + version, result_id y group_id.
- Idempotencia estricta para payloads y checksums idénticos.
- Detección explícita de conflictos si se intenta sobrescribir con contenido diferente bajo el mismo ID/versión.
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción física sin autorreparación silenciosa.
- Thread-safe mediante RLock de concurrencia.
- Path safety estricto (rechaza traversals).
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, Sequence, List
import threading

from src.domain.duplicate_detection.models import (
    DuplicateStatus,
    DuplicateReasonCode,
    DuplicateCandidate,
    DuplicateDetectionPolicy,
    DuplicateDetectionResult,
    DuplicateGroup,
    compute_duplicate_policy_checksum,
    compute_duplicate_result_checksum,
    compute_duplicate_group_checksum,
)
from src.domain.duplicate_detection.ports import (
    DuplicateDetectionPolicyRepositoryPort,
    DuplicateDetectionRepositoryPort,
)
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS

logger = logging.getLogger(__name__)


class JsonDuplicateDetectionRepositoryError(Exception):
    """Excepción base de persistencia Duplicate Detection L.7."""


class DuplicateDetectionConflictError(JsonDuplicateDetectionRepositoryError):
    """Conflicto semántico bajo la misma identidad de resultado o grupo de duplicados."""


class DuplicateDetectionPolicyConflictError(JsonDuplicateDetectionRepositoryError):
    """Conflicto semántico bajo la misma identidad/versión de política."""


class CorruptedDuplicateDetectionRecordError(JsonDuplicateDetectionRepositoryError):
    """Registro corrupto o checksum inválido; nunca se repara silenciosamente."""


CorruptedDuplicatePolicyError = CorruptedDuplicateDetectionRecordError
CorruptedDuplicateResultError = CorruptedDuplicateDetectionRecordError
CorruptedDuplicateGroupError = CorruptedDuplicateDetectionRecordError


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, (dict, MappingProxyType)):
        result = {}
        for key, val in value.items():
            key_text = str(key)
            if any(sensitive in key_text.lower() for sensitive in SENSITIVE_KEYS):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _encode(val)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_encode(item) for item in value]
    return value


class JsonDuplicateDetectionPolicyRepository(DuplicateDetectionPolicyRepositoryPort):
    """Repositorio JSON atómico para políticas de deduplicación."""

    def __init__(self, base_directory: Union[str, Path]):
        self._base_dir = Path(base_directory)
        self._policies_dir = self._base_dir / "policies"
        self._policies_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_file_path(self, policy_id: str, version: str) -> Path:
        validate_safe_identifier(policy_id, "policy_id")
        return self._policies_dir / f"{policy_id}_{version}.json"

    def save_policy(self, policy: DuplicateDetectionPolicy) -> DuplicateDetectionPolicy:
        with self._lock:
            validate_safe_identifier(policy.policy_id, "policy_id")
            path = self._get_file_path(policy.policy_id, policy.version)

            payload = {
                "policy_id": policy.policy_id,
                "name": policy.name,
                "version": policy.version,
                "identity_fields": list(policy.identity_fields),
                "ignored_fields": list(policy.ignored_fields),
                "require_same_source": policy.require_same_source,
                "allow_cross_source_duplicates": policy.allow_cross_source_duplicates,
                "temporal_window_seconds": policy.temporal_window_seconds,
                "allow_replay_idempotency": policy.allow_replay_idempotency,
                "metadata": _encode(policy.metadata),
                "checksum": policy.checksum,
            }

            if path.exists():
                existing = self.get_policy(policy.policy_id, policy.version)
                if existing and existing.checksum == policy.checksum:
                    return policy
                raise DuplicateDetectionPolicyConflictError(
                    f"Policy conflict: ID '{policy.policy_id}' version '{policy.version}' exists with different content."
                )

            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, path)
            return policy

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[DuplicateDetectionPolicy]:
        with self._lock:
            validate_safe_identifier(policy_id, "policy_id")
            if version:
                path = self._get_file_path(policy_id, version)
                if not path.exists():
                    return None
                return self._read_policy_file(path)

            # Buscar la versión más reciente
            candidates = list(self._policies_dir.glob(f"{policy_id}_*.json"))
            if not candidates:
                return None

            # Parsear y ordenar
            policies = []
            for p in candidates:
                pol = self._read_policy_file(p)
                if pol:
                    policies.append(pol)

            if not policies:
                return None
            policies.sort(key=lambda x: [int(part) for part in x.version.split(".")], reverse=True)
            return policies[0]

    def list_policies(self) -> Sequence[DuplicateDetectionPolicy]:
        with self._lock:
            policies = []
            for path in self._policies_dir.glob("*.json"):
                if path.name.endswith(".tmp"):
                    continue
                pol = self._read_policy_file(path)
                if pol:
                    policies.append(pol)
            return tuple(policies)

    def _read_policy_file(self, path: Path) -> DuplicateDetectionPolicy:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedDuplicatePolicyError(f"Failed to read policy file {path}: {e}")

        policy = DuplicateDetectionPolicy(
            policy_id=data["policy_id"],
            name=data["name"],
            version=data["version"],
            identity_fields=tuple(data.get("identity_fields", ())),
            ignored_fields=tuple(data.get("ignored_fields", ())),
            require_same_source=data.get("require_same_source", False),
            allow_cross_source_duplicates=data.get("allow_cross_source_duplicates", False),
            temporal_window_seconds=data.get("temporal_window_seconds"),
            allow_replay_idempotency=data.get("allow_replay_idempotency", True),
            metadata=data.get("metadata", {}),
            checksum=data.get("checksum"),
        )

        expected_chk = compute_duplicate_policy_checksum(policy)
        if policy.checksum != expected_chk:
            raise CorruptedDuplicatePolicyError(
                f"Policy checksum mismatch in {path}: file has {policy.checksum}, computed {expected_chk}"
            )

        return policy


class JsonDuplicateDetectionRepository(DuplicateDetectionRepositoryPort):
    """Repositorio JSON atómico para resultados de deduplicación y DuplicateGroups."""

    def __init__(self, base_directory: Union[str, Path]):
        self._base_dir = Path(base_directory)
        self._results_dir = self._base_dir / "results"
        self._groups_dir = self._base_dir / "groups"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._groups_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_result_path(self, result_id: str) -> Path:
        validate_safe_identifier(result_id, "result_id")
        return self._results_dir / f"{result_id}.json"

    def _get_group_path(self, group_id: str) -> Path:
        validate_safe_identifier(group_id, "group_id")
        return self._groups_dir / f"{group_id}.json"

    def save_result(self, result: DuplicateDetectionResult) -> DuplicateDetectionResult:
        with self._lock:
            validate_safe_identifier(result.result_id, "result_id")
            path = self._get_result_path(result.result_id)

            payload = {
                "result_id": result.result_id,
                "primary_record_id": result.primary_record_id,
                "secondary_record_id": result.secondary_record_id,
                "status": result.status.value,
                "reason_code": result.reason_code.value,
                "policy_id": result.policy_id,
                "policy_version": result.policy_version,
                "primary_fingerprint": result.primary_fingerprint,
                "secondary_fingerprint": result.secondary_fingerprint,
                "evaluated_at": result.evaluated_at.isoformat(),
                "is_exact_replay": result.is_exact_replay,
                "confidence_score": str(result.confidence_score),
                "details": _encode(result.details),
                "checksum": result.checksum,
            }

            if path.exists():
                existing = self.get_result(result.result_id)
                if existing and existing.checksum == result.checksum:
                    return result
                raise DuplicateDetectionConflictError(
                    f"Result conflict: ID '{result.result_id}' exists with different content."
                )

            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, path)
            return result

    def get_result(self, result_id: str) -> Optional[DuplicateDetectionResult]:
        with self._lock:
            validate_safe_identifier(result_id, "result_id")
            path = self._get_result_path(result_id)
            if not path.exists():
                return None
            return self._read_result_file(path)

    def find_results_by_record(self, record_id: str) -> Sequence[DuplicateDetectionResult]:
        with self._lock:
            validate_safe_identifier(record_id, "record_id")
            matched: List[DuplicateDetectionResult] = []
            for path in self._results_dir.glob("*.json"):
                if path.name.endswith(".tmp"):
                    continue
                res = self._read_result_file(path)
                if res and (res.primary_record_id == record_id or res.secondary_record_id == record_id):
                    matched.append(res)
            return tuple(matched)

    def _read_result_file(self, path: Path) -> DuplicateDetectionResult:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedDuplicateResultError(f"Failed to read result file {path}: {e}")

        result = DuplicateDetectionResult(
            result_id=data["result_id"],
            primary_record_id=data["primary_record_id"],
            secondary_record_id=data["secondary_record_id"],
            status=DuplicateStatus(data["status"]),
            reason_code=DuplicateReasonCode(data["reason_code"]),
            policy_id=data["policy_id"],
            policy_version=data["policy_version"],
            primary_fingerprint=data["primary_fingerprint"],
            secondary_fingerprint=data["secondary_fingerprint"],
            evaluated_at=datetime.fromisoformat(data["evaluated_at"]),
            is_exact_replay=data.get("is_exact_replay", False),
            confidence_score=Decimal(str(data.get("confidence_score", "1.0000"))),
            details=data.get("details", {}),
            checksum=data.get("checksum"),
        )

        expected_chk = compute_duplicate_result_checksum(result)
        if result.checksum != expected_chk:
            raise CorruptedDuplicateResultError(
                f"Result checksum mismatch in {path}: file has {result.checksum}, computed {expected_chk}"
            )

        return result

    def save_group(self, group: DuplicateGroup) -> DuplicateGroup:
        with self._lock:
            validate_safe_identifier(group.group_id, "group_id")
            path = self._get_group_path(group.group_id)

            payload = {
                "group_id": group.group_id,
                "canonical_fingerprint": group.canonical_fingerprint,
                "member_record_ids": list(group.member_record_ids),
                "canonical_entity_id": group.canonical_entity_id,
                "created_at": group.created_at.isoformat() if group.created_at else None,
                "updated_at": group.updated_at.isoformat() if group.updated_at else None,
                "metadata": _encode(group.metadata),
                "checksum": group.checksum,
            }

            if path.exists():
                existing = self.get_group(group.group_id)
                if existing and existing.checksum == group.checksum:
                    return group
                raise DuplicateDetectionConflictError(
                    f"Group conflict: ID '{group.group_id}' exists with different content."
                )

            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, path)
            return group

    def get_group(self, group_id: str) -> Optional[DuplicateGroup]:
        with self._lock:
            validate_safe_identifier(group_id, "group_id")
            path = self._get_group_path(group_id)
            if not path.exists():
                return None
            return self._read_group_file(path)

    def get_group_by_fingerprint(self, fingerprint: str) -> Optional[DuplicateGroup]:
        with self._lock:
            for path in self._groups_dir.glob("*.json"):
                if path.name.endswith(".tmp"):
                    continue
                grp = self._read_group_file(path)
                if grp and grp.canonical_fingerprint == fingerprint:
                    return grp
            return None

    def list_groups(self) -> Sequence[DuplicateGroup]:
        with self._lock:
            groups = []
            for path in self._groups_dir.glob("*.json"):
                if path.name.endswith(".tmp"):
                    continue
                grp = self._read_group_file(path)
                if grp:
                    groups.append(grp)
            return tuple(groups)

    def _read_group_file(self, path: Path) -> DuplicateGroup:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedDuplicateGroupError(f"Failed to read group file {path}: {e}")

        group = DuplicateGroup(
            group_id=data["group_id"],
            canonical_fingerprint=data["canonical_fingerprint"],
            member_record_ids=tuple(data.get("member_record_ids", ())),
            canonical_entity_id=data.get("canonical_entity_id"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None,
            metadata=data.get("metadata", {}),
            checksum=data.get("checksum"),
        )

        expected_chk = compute_duplicate_group_checksum(group)
        if group.checksum != expected_chk:
            raise CorruptedDuplicateGroupError(
                f"Group checksum mismatch in {path}: file has {group.checksum}, computed {expected_chk}"
            )

        return group
