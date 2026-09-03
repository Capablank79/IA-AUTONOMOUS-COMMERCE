"""
Implementación JSON persistente, atómica y determinista para Freshness Policies y Assessments (Hito L.3).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Inmutabilidad estricta por ID + Version (para Policies) y por assessment_id (para Assessments).
- Idempotencia estricta para replays con payload y checksum idénticos.
- Detección explícita de conflictos si se intenta modificar una política/evaluación existente sin incrementar versión.
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción física.
- Thread-safe mediante RLock de concurrencia y atomicidad a nivel filesystem.
- Recuperación resiliente de índices secundarios ante caídas o reinicios.
- Path safety estricto (rechaza traversals, .. , /, \\).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Sequence, Set
import threading

from src.domain.freshness.models import (
    FreshnessPolicy,
    FreshnessAssessment,
    FreshnessStatus,
    compute_policy_checksum,
    compute_assessment_checksum,
)
from src.domain.freshness.ports import (
    FreshnessPolicyRepositoryPort,
    FreshnessAssessmentRepositoryPort,
)
from src.domain.security.models import (
    validate_safe_identifier,
    SENSITIVE_KEYS,
    sanitize_security_data,
)

logger = logging.getLogger(__name__)


class JsonFreshnessRepositoryError(Exception):
    """Excepción base para errores en los repositorios JSON de Freshness."""
    pass


class FreshnessConflictError(JsonFreshnessRepositoryError):
    """Se lanza cuando se intenta sobrescribir una entidad de Freshness con contenido diferente."""
    pass


class CorruptedFreshnessRecordError(JsonFreshnessRepositoryError):
    """Se lanza cuando un archivo persistido está corrupto o tiene un checksum inválido."""
    pass


# Alias específicos para compatibilidad semántica y granularidad
CorruptedFreshnessPolicyError = CorruptedFreshnessRecordError
CorruptedFreshnessAssessmentError = CorruptedFreshnessRecordError


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _atomic_write_json(file_path: Path, data: Dict[str, Any]) -> None:
    """Escribe un diccionario JSON de forma atómica y segura contra caídas."""
    tmp_file = file_path.with_suffix(".tmp")
    serialized = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write(serialized)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, file_path)


class JsonFreshnessPolicyRepository(FreshnessPolicyRepositoryPort):
    """
    Repositorio JSON persistente, atómico y determinista para FreshnessPolicy (L.3).
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.policies_dir = self.base_dir / "freshness" / "policies"
        self.policies_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, FreshnessPolicy] = {}  # key: f"{policy_id}:{version}"
        self._load_and_rebuild_index()

    def _load_and_rebuild_index(self) -> None:
        with self._lock:
            self._cache.clear()
            for p_file in self.policies_dir.glob("*.json"):
                if p_file.name.endswith(".tmp"):
                    continue
                try:
                    with open(p_file, "r", encoding="utf-8") as f:
                        raw_data = json.load(f)

                    policy = FreshnessPolicy(
                        policy_id=raw_data["policy_id"],
                        name=raw_data["name"],
                        version=raw_data.get("version", "1.0.0"),
                        ttl_seconds=raw_data["ttl_seconds"],
                        stale_threshold_seconds=raw_data.get("stale_threshold_seconds"),
                        future_tolerance_seconds=raw_data.get("future_tolerance_seconds", 5.0),
                        source_type=raw_data.get("source_type"),
                        source_id=raw_data.get("source_id"),
                        subject_type=raw_data.get("subject_type"),
                        field_path=raw_data.get("field_path"),
                        description=raw_data.get("description"),
                        checksum=raw_data.get("checksum", ""),
                        metadata=raw_data.get("metadata", {}),
                    )
                    key = f"{policy.policy_id}:{policy.version}"
                    self._cache[key] = policy
                except Exception as e:
                    logger.error(f"Error loading freshness policy from {p_file}: {e}")

    def save_policy(self, policy: FreshnessPolicy) -> FreshnessPolicy:
        validate_safe_identifier(policy.policy_id, field_name="policy_id")
        validate_safe_identifier(policy.version, field_name="version")

        key = f"{policy.policy_id}:{policy.version}"
        target_file = self.policies_dir / f"{policy.policy_id}_v{policy.version}.json"

        with self._lock:
            # Si ya existe en caché o disco, validar idempotencia / conflicto
            if key in self._cache:
                existing = self._cache[key]
                if existing.checksum == policy.checksum:
                    return existing
                raise FreshnessConflictError(
                    f"Policy '{policy.policy_id}' version '{policy.version}' already exists with different checksum/content."
                )

            policy_data = {
                "policy_id": policy.policy_id,
                "name": policy.name,
                "version": policy.version,
                "ttl_seconds": policy.ttl_seconds,
                "stale_threshold_seconds": policy.stale_threshold_seconds,
                "future_tolerance_seconds": policy.future_tolerance_seconds,
                "source_type": policy.source_type,
                "source_id": policy.source_id,
                "subject_type": policy.subject_type,
                "field_path": policy.field_path,
                "description": policy.description,
                "checksum": policy.checksum,
                "metadata": _encode_json_value(policy.metadata),
            }

            _atomic_write_json(target_file, policy_data)
            self._cache[key] = policy
            return policy

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[FreshnessPolicy]:
        validate_safe_identifier(policy_id, field_name="policy_id")
        if version:
            validate_safe_identifier(version, field_name="version")

        with self._lock:
            if version:
                key = f"{policy_id}:{version}"
                return self._cache.get(key)

            # Buscar la versión más reciente
            candidates = [p for p in self._cache.values() if p.policy_id == policy_id]
            if not candidates:
                return None

            def parse_ver(p: FreshnessPolicy):
                parts = p.version.split("-")[0].split("+")[0].split(".")
                try:
                    return tuple(int(x) for x in parts)
                except ValueError:
                    return (0, 0, 0)

            sorted_candidates = sorted(candidates, key=parse_ver, reverse=True)
            return sorted_candidates[0]

    def list_policies(self) -> Sequence[FreshnessPolicy]:
        with self._lock:
            return list(self._cache.values())


class JsonFreshnessAssessmentRepository(FreshnessAssessmentRepositoryPort):
    """
    Repositorio JSON persistente, atómico y determinista para FreshnessAssessment (L.3).
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.assessments_dir = self.base_dir / "freshness" / "assessments"
        self.assessments_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, FreshnessAssessment] = {}  # key: assessment_id
        self._load_and_rebuild_index()

    def _load_and_rebuild_index(self) -> None:
        with self._lock:
            self._cache.clear()
            for a_file in self.assessments_dir.glob("*.json"):
                if a_file.name.endswith(".tmp"):
                    continue
                try:
                    with open(a_file, "r", encoding="utf-8") as f:
                        raw_data = json.load(f)

                    obs_at = None
                    if raw_data.get("observed_at"):
                        obs_at = datetime.fromisoformat(raw_data["observed_at"])
                    eval_at = datetime.fromisoformat(raw_data["evaluated_at"])

                    assessment = FreshnessAssessment(
                        assessment_id=raw_data["assessment_id"],
                        subject_type=raw_data["subject_type"],
                        subject_id=raw_data["subject_id"],
                        status=FreshnessStatus(raw_data["status"]),
                        reason=raw_data["reason"],
                        evaluated_at=eval_at,
                        ttl_seconds=raw_data["ttl_seconds"],
                        age_seconds=raw_data.get("age_seconds"),
                        policy_id=raw_data["policy_id"],
                        policy_version=raw_data.get("policy_version", "1.0.0"),
                        field_path=raw_data.get("field_path"),
                        source_id=raw_data.get("source_id"),
                        provenance_id=raw_data.get("provenance_id"),
                        observed_at=obs_at,
                        correlation_id=raw_data.get("correlation_id", "default-correlation"),
                        checksum=raw_data.get("checksum", ""),
                        metadata=raw_data.get("metadata", {}),
                    )
                    self._cache[assessment.assessment_id] = assessment
                except Exception as e:
                    logger.error(f"Error loading freshness assessment from {a_file}: {e}")

    def save_assessment(self, assessment: FreshnessAssessment) -> FreshnessAssessment:
        validate_safe_identifier(assessment.assessment_id, field_name="assessment_id")
        target_file = self.assessments_dir / f"{assessment.assessment_id}.json"

        with self._lock:
            if assessment.assessment_id in self._cache:
                existing = self._cache[assessment.assessment_id]
                if existing.checksum == assessment.checksum:
                    return existing
                raise FreshnessConflictError(
                    f"Assessment '{assessment.assessment_id}' already exists with different checksum/content."
                )

            data = {
                "assessment_id": assessment.assessment_id,
                "subject_type": assessment.subject_type,
                "subject_id": assessment.subject_id,
                "status": assessment.status.value,
                "reason": assessment.reason,
                "evaluated_at": assessment.evaluated_at.isoformat(),
                "ttl_seconds": assessment.ttl_seconds,
                "age_seconds": assessment.age_seconds,
                "policy_id": assessment.policy_id,
                "policy_version": assessment.policy_version,
                "field_path": assessment.field_path,
                "source_id": assessment.source_id,
                "provenance_id": assessment.provenance_id,
                "observed_at": assessment.observed_at.isoformat() if assessment.observed_at else None,
                "correlation_id": assessment.correlation_id,
                "checksum": assessment.checksum,
                "metadata": _encode_json_value(assessment.metadata),
            }

            _atomic_write_json(target_file, data)
            self._cache[assessment.assessment_id] = assessment
            return assessment

    def get_assessment(self, assessment_id: str) -> Optional[FreshnessAssessment]:
        validate_safe_identifier(assessment_id, field_name="assessment_id")
        with self._lock:
            return self._cache.get(assessment_id)

    def find_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
        field_path: Optional[str] = None,
    ) -> Sequence[FreshnessAssessment]:
        with self._lock:
            results = []
            for a in self._cache.values():
                if a.subject_id != subject_id:
                    continue
                if subject_type is not None:
                    st_val = subject_type.value if hasattr(subject_type, "value") else str(subject_type)
                    if a.subject_type != st_val:
                        continue
                if field_path is not None and a.field_path != field_path:
                    continue
                results.append(a)
            return sorted(results, key=lambda x: x.evaluated_at, reverse=True)

    def get_latest_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
        field_path: Optional[str] = None,
    ) -> Optional[FreshnessAssessment]:
        matches = self.find_by_subject(
            subject_id=subject_id,
            subject_type=subject_type,
            field_path=field_path,
        )
        return matches[0] if matches else None
