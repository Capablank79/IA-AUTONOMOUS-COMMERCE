"""
Persistencia JSON atómica, versionada e íntegra para Conflict Resolution L.8.

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Inmutabilidad estricta por policy_id + version y conflict_id.
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

from src.domain.conflict_resolution.models import (
    ConflictStatus,
    ConflictReasonCode,
    ResolutionStrategy,
    ConflictCandidate,
    ConflictResolutionPolicy,
    ConflictResolutionResult,
    compute_conflict_policy_checksum,
    compute_conflict_result_checksum,
)
from src.domain.conflict_resolution.ports import (
    ConflictResolutionPolicyRepositoryPort,
    ConflictResolutionRepositoryPort,
)
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS

logger = logging.getLogger(__name__)


class JsonConflictResolutionRepositoryError(Exception):
    """Excepción base de persistencia Conflict Resolution L.8."""


class ConflictResolutionConflictError(JsonConflictResolutionRepositoryError):
    """Conflicto semántico bajo la misma identidad de resultado de conflicto."""


class ConflictResolutionPolicyConflictError(JsonConflictResolutionRepositoryError):
    """Conflicto semántico bajo la misma identidad/versión de política."""


class CorruptedConflictResolutionRecordError(JsonConflictResolutionRepositoryError):
    """Registro corrupto o checksum inválido; nunca se repara silenciosamente."""


CorruptedConflictPolicyError = CorruptedConflictResolutionRecordError
CorruptedConflictResultError = CorruptedConflictResolutionRecordError


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
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _atomic_write_json(file_path: Path, data: Dict[str, Any]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_suffix(".tmp")
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, file_path)


class JsonConflictResolutionPolicyRepository(ConflictResolutionPolicyRepositoryPort):
    """Repositorio JSON thread-safe y crash-safe para políticas de resolución de conflictos."""

    def __init__(self, base_dir: Union[str, Path]):
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_policy_path(self, policy_id: str, version: str) -> Path:
        validate_safe_identifier(policy_id, "policy_id")
        return self._base_dir / f"{policy_id}_v{version}.json"

    def save_policy(self, policy: ConflictResolutionPolicy) -> ConflictResolutionPolicy:
        with self._lock:
            path = self._get_policy_path(policy.policy_id, policy.version)
            expected_checksum = compute_conflict_policy_checksum(policy)

            if path.exists():
                existing = self.get_policy(policy.policy_id, policy.version)
                if existing:
                    if existing.checksum == expected_checksum:
                        return existing
                    raise ConflictResolutionPolicyConflictError(
                        f"Conflict detected for policy '{policy.policy_id}' version '{policy.version}': content mismatch"
                    )

            data = {
                "policy_id": policy.policy_id,
                "name": policy.name,
                "version": policy.version,
                "applicable_subject_type": policy.applicable_subject_type,
                "applicable_field_path": policy.applicable_field_path,
                "strategy": policy.strategy.value,
                "source_precedence": list(policy.source_precedence),
                "require_freshness": policy.require_freshness,
                "max_acceptable_age_seconds": policy.max_acceptable_age_seconds,
                "min_confidence_level": policy.min_confidence_level.value if policy.min_confidence_level else None,
                "min_confidence_score": str(policy.min_confidence_score) if policy.min_confidence_score is not None else None,
                "consensus_min_votes": policy.consensus_min_votes,
                "consensus_min_ratio": str(policy.consensus_min_ratio),
                "tie_break_strategy": policy.tie_break_strategy.value if policy.tie_break_strategy else None,
                "allow_unresolved": policy.allow_unresolved,
                "metadata": _encode(policy.metadata),
                "checksum": expected_checksum,
            }
            _atomic_write_json(path, data)
            return policy

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[ConflictResolutionPolicy]:
        with self._lock:
            validate_safe_identifier(policy_id, "policy_id")
            if version:
                path = self._get_policy_path(policy_id, version)
                if not path.exists():
                    return None
                return self._load_policy_from_file(path)

            candidates: List[Path] = []
            for file in self._base_dir.glob(f"{policy_id}_v*.json"):
                candidates.append(file)
            if not candidates:
                return None
            candidates.sort(key=lambda p: p.name)
            return self._load_policy_from_file(candidates[-1])

    def list_policies(self) -> Sequence[ConflictResolutionPolicy]:
        with self._lock:
            policies = []
            for path in sorted(self._base_dir.glob("*_v*.json")):
                policy = self._load_policy_from_file(path)
                policies.append(policy)
            return tuple(policies)

    def _load_policy_from_file(self, path: Path) -> ConflictResolutionPolicy:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedConflictPolicyError(f"Failed to read policy file {path.name}: {e}")

        stored_chk = data.get("checksum")
        min_conf_level = data.get("min_confidence_level")
        min_conf_score = data.get("min_confidence_score")
        tie_break = data.get("tie_break_strategy")

        policy = ConflictResolutionPolicy(
            policy_id=data["policy_id"],
            name=data["name"],
            version=data["version"],
            applicable_subject_type=data.get("applicable_subject_type"),
            applicable_field_path=data.get("applicable_field_path"),
            strategy=ResolutionStrategy(data["strategy"]),
            source_precedence=tuple(data.get("source_precedence", [])),
            require_freshness=bool(data.get("require_freshness", False)),
            max_acceptable_age_seconds=data.get("max_acceptable_age_seconds"),
            min_confidence_level=min_conf_level,
            min_confidence_score=Decimal(min_conf_score) if min_conf_score is not None else None,
            consensus_min_votes=int(data.get("consensus_min_votes", 2)),
            consensus_min_ratio=Decimal(str(data.get("consensus_min_ratio", "0.6667"))),
            tie_break_strategy=ResolutionStrategy(tie_break) if tie_break else None,
            allow_unresolved=bool(data.get("allow_unresolved", True)),
            metadata=data.get("metadata", {}),
            checksum=stored_chk,
        )

        expected_chk = compute_conflict_policy_checksum(policy)
        if stored_chk != expected_chk:
            raise CorruptedConflictPolicyError(
                f"Policy checksum mismatch for {path.name}: expected {expected_chk}, got {stored_chk}"
            )
        return policy


class JsonConflictResolutionRepository(ConflictResolutionRepositoryPort):
    """Repositorio JSON thread-safe y crash-safe para resultados de resolución de conflictos."""

    def __init__(self, base_dir: Union[str, Path]):
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_result_path(self, conflict_id: str) -> Path:
        validate_safe_identifier(conflict_id, "conflict_id")
        return self._base_dir / f"result_{conflict_id}.json"

    def save_result(self, result: ConflictResolutionResult) -> ConflictResolutionResult:
        with self._lock:
            path = self._get_result_path(result.conflict_id)
            expected_checksum = compute_conflict_result_checksum(result)

            if path.exists():
                existing = self.get_result(result.conflict_id)
                if existing:
                    if existing.checksum == expected_checksum:
                        return existing
                    raise ConflictResolutionConflictError(
                        f"Conflict detected for conflict result '{result.conflict_id}': content mismatch"
                    )

            data = {
                "conflict_id": result.conflict_id,
                "canonical_entity_id": result.canonical_entity_id,
                "field_path": result.field_path,
                "candidate_ids": list(result.candidate_ids),
                "strategy": result.strategy.value,
                "status": result.status.value,
                "reason_code": result.reason_code.value,
                "selected_candidate_id": result.selected_candidate_id,
                "selected_value": _encode(result.selected_value),
                "policy_id": result.policy_id,
                "policy_version": result.policy_version,
                "evaluated_at": result.evaluated_at.astimezone(timezone.utc).isoformat(),
                "correlation_id": result.correlation_id,
                "details": _encode(result.details),
                "checksum": expected_checksum,
            }
            _atomic_write_json(path, data)
            return result

    def get_result(self, conflict_id: str) -> Optional[ConflictResolutionResult]:
        with self._lock:
            validate_safe_identifier(conflict_id, "conflict_id")
            path = self._get_result_path(conflict_id)
            if not path.exists():
                return None
            return self._load_result_from_file(path)

    def find_results_by_entity(self, canonical_entity_id: str) -> Sequence[ConflictResolutionResult]:
        with self._lock:
            validate_safe_identifier(canonical_entity_id, "canonical_entity_id")
            results = []
            for path in sorted(self._base_dir.glob("result_*.json")):
                res = self._load_result_from_file(path)
                if res.canonical_entity_id == canonical_entity_id:
                    results.append(res)
            return tuple(results)

    def find_results_by_correlation(self, correlation_id: str) -> Sequence[ConflictResolutionResult]:
        with self._lock:
            validate_safe_identifier(correlation_id, "correlation_id")
            results = []
            for path in sorted(self._base_dir.glob("result_*.json")):
                res = self._load_result_from_file(path)
                if res.correlation_id == correlation_id:
                    results.append(res)
            return tuple(results)

    def _load_result_from_file(self, path: Path) -> ConflictResolutionResult:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedConflictResultError(f"Failed to read result file {path.name}: {e}")

        stored_chk = data.get("checksum")
        evaluated_at_raw = data.get("evaluated_at")
        evaluated_at = datetime.fromisoformat(evaluated_at_raw) if evaluated_at_raw else datetime.now(timezone.utc)

        result = ConflictResolutionResult(
            conflict_id=data["conflict_id"],
            canonical_entity_id=data["canonical_entity_id"],
            field_path=data["field_path"],
            candidate_ids=tuple(data.get("candidate_ids", [])),
            strategy=ResolutionStrategy(data["strategy"]),
            status=ConflictStatus(data["status"]),
            reason_code=ConflictReasonCode(data["reason_code"]),
            selected_candidate_id=data.get("selected_candidate_id"),
            selected_value=data.get("selected_value"),
            policy_id=data["policy_id"],
            policy_version=data["policy_version"],
            evaluated_at=evaluated_at,
            correlation_id=data["correlation_id"],
            details=data.get("details", {}),
            checksum=stored_chk,
        )

        expected_chk = compute_conflict_result_checksum(result)
        if stored_chk != expected_chk:
            raise CorruptedConflictResultError(
                f"Result checksum mismatch for {path.name}: expected {expected_chk}, got {stored_chk}"
            )
        return result
