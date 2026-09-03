"""Persistencia JSON atómica, versionada e íntegra para Confidence Model L.4."""

from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, Sequence
import threading

from src.domain.confidence.models import (
    ConfidencePolicy,
    ConfidenceAssessment,
    ConfidenceFactor,
    ConfidenceLevel,
    DerivedAggregationStrategy,
)
from src.domain.confidence.ports import ConfidencePolicyRepositoryPort, ConfidenceAssessmentRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS


class JsonConfidenceRepositoryError(Exception):
    """Excepción base de persistencia Confidence L.4."""


class ConfidenceConflictError(JsonConfidenceRepositoryError):
    """Conflicto semántico bajo la misma identidad/version."""


class CorruptedConfidenceRecordError(JsonConfidenceRepositoryError):
    """Registro corrupto o checksum inválido; nunca se repara silenciosamente."""


CorruptedConfidencePolicyError = CorruptedConfidenceRecordError
CorruptedConfidenceAssessmentError = CorruptedConfidenceRecordError


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


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    serialized = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class JsonConfidencePolicyRepository(ConfidencePolicyRepositoryPort):
    """Repositorio crash-safe e idempotente para ConfidencePolicy."""

    def __init__(self, base_dir: Union[str, Path]):
        self.policies_dir = Path(base_dir) / "confidence" / "policies"
        self.policies_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, ConfidencePolicy] = {}
        self._load()

    def _deserialize(self, raw: Dict[str, Any]) -> ConfidencePolicy:
        try:
            return ConfidencePolicy(
                policy_id=raw["policy_id"],
                name=raw["name"],
                version=raw.get("version", "1.0.0"),
                source_type=raw.get("source_type"),
                source_id=raw.get("source_id"),
                subject_type=raw.get("subject_type"),
                field_path=raw.get("field_path"),
                description=raw.get("description"),
                high_threshold=Decimal(raw["high_threshold"]),
                medium_threshold=Decimal(raw["medium_threshold"]),
                weights={key: Decimal(value) for key, value in raw.get("weights", {}).items()},
                factor_scores={key: Decimal(value) for key, value in raw.get("factor_scores", {}).items()},
                require_provenance=raw.get("require_provenance", True),
                require_freshness=raw.get("require_freshness", False),
                derived_aggregation=DerivedAggregationStrategy(raw.get("derived_aggregation", "MIN")),
                checksum=raw.get("checksum", ""),
                metadata=raw.get("metadata", {}),
            )
        except Exception as exc:
            raise CorruptedConfidencePolicyError(f"Invalid confidence policy record: {exc}") from exc

    def _load(self) -> None:
        with self._lock:
            self._cache.clear()
            for path in self.policies_dir.glob("*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as stream:
                        policy = self._deserialize(json.load(stream))
                    self._cache[f"{policy.policy_id}:{policy.version}"] = policy
                except CorruptedConfidenceRecordError:
                    raise
                except Exception as exc:
                    raise CorruptedConfidencePolicyError(f"Cannot load {path.name}: {exc}") from exc

    def save_policy(self, policy: ConfidencePolicy) -> ConfidencePolicy:
        validate_safe_identifier(policy.policy_id, field_name="policy_id")
        validate_safe_identifier(policy.version, field_name="version")
        key = f"{policy.policy_id}:{policy.version}"
        target = self.policies_dir / f"{policy.policy_id}_v{policy.version}.json"
        with self._lock:
            existing = self._cache.get(key)
            if existing:
                if existing.checksum == policy.checksum:
                    return existing
                raise ConfidenceConflictError(
                    f"Policy '{policy.policy_id}' version '{policy.version}' already exists with different content"
                )
            data = {
                "policy_id": policy.policy_id,
                "name": policy.name,
                "version": policy.version,
                "source_type": policy.source_type,
                "source_id": policy.source_id,
                "subject_type": policy.subject_type,
                "field_path": policy.field_path,
                "description": policy.description,
                "high_threshold": str(policy.high_threshold),
                "medium_threshold": str(policy.medium_threshold),
                "weights": {key: str(value) for key, value in policy.weights.items()},
                "factor_scores": {key: str(value) for key, value in policy.factor_scores.items()},
                "require_provenance": policy.require_provenance,
                "require_freshness": policy.require_freshness,
                "derived_aggregation": policy.derived_aggregation.value,
                "checksum": policy.checksum,
                "metadata": _encode(policy.metadata),
            }
            _atomic_write_json(target, data)
            self._cache[key] = policy
            return policy

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[ConfidencePolicy]:
        validate_safe_identifier(policy_id, field_name="policy_id")
        with self._lock:
            if version:
                validate_safe_identifier(version, field_name="version")
                return self._cache.get(f"{policy_id}:{version}")
            candidates = [item for item in self._cache.values() if item.policy_id == policy_id]
            if not candidates:
                return None
            return max(candidates, key=lambda item: tuple(int(x) for x in item.version.split("-")[0].split("+")[0].split(".")))

    def list_policies(self) -> Sequence[ConfidencePolicy]:
        with self._lock:
            return tuple(self._cache.values())


class JsonConfidenceAssessmentRepository(ConfidenceAssessmentRepositoryPort):
    """Repositorio crash-safe, histórico e idempotente para ConfidenceAssessment."""

    def __init__(self, base_dir: Union[str, Path]):
        self.assessments_dir = Path(base_dir) / "confidence" / "assessments"
        self.assessments_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, ConfidenceAssessment] = {}
        self._load()

    def _deserialize(self, raw: Dict[str, Any]) -> ConfidenceAssessment:
        try:
            factors = tuple(
                ConfidenceFactor(
                    factor_name=item["factor_name"],
                    factor_type=item["factor_type"],
                    score=Decimal(item["score"]) if item.get("score") is not None else None,
                    weight=Decimal(item["weight"]) if item.get("weight") is not None else None,
                    impact=item.get("impact", "NEUTRAL"),
                    details=item.get("details", {}),
                )
                for item in raw.get("factors", [])
            )
            return ConfidenceAssessment(
                assessment_id=raw["assessment_id"],
                subject_type=raw["subject_type"],
                subject_id=raw["subject_id"],
                level=ConfidenceLevel(raw["level"]),
                reason=raw["reason"],
                evaluated_at=datetime.fromisoformat(raw["evaluated_at"]),
                policy_id=raw["policy_id"],
                score=Decimal(raw["score"]) if raw.get("score") is not None else None,
                policy_version=raw.get("policy_version", "1.0.0"),
                field_path=raw.get("field_path"),
                source_id=raw.get("source_id"),
                provenance_id=raw.get("provenance_id"),
                factors=factors,
                correlation_id=raw.get("correlation_id", "default-correlation"),
                checksum=raw.get("checksum", ""),
                metadata=raw.get("metadata", {}),
            )
        except Exception as exc:
            raise CorruptedConfidenceAssessmentError(f"Invalid confidence assessment record: {exc}") from exc

    def _load(self) -> None:
        with self._lock:
            self._cache.clear()
            for path in self.assessments_dir.glob("*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as stream:
                        assessment = self._deserialize(json.load(stream))
                    self._cache[assessment.assessment_id] = assessment
                except CorruptedConfidenceRecordError:
                    raise
                except Exception as exc:
                    raise CorruptedConfidenceAssessmentError(f"Cannot load {path.name}: {exc}") from exc

    def save_assessment(self, assessment: ConfidenceAssessment) -> ConfidenceAssessment:
        validate_safe_identifier(assessment.assessment_id, field_name="assessment_id")
        target = self.assessments_dir / f"{assessment.assessment_id}.json"
        with self._lock:
            existing = self._cache.get(assessment.assessment_id)
            if existing:
                if existing.checksum == assessment.checksum:
                    return existing
                raise ConfidenceConflictError(
                    f"Assessment '{assessment.assessment_id}' already exists with different content"
                )
            data = {
                "assessment_id": assessment.assessment_id,
                "subject_type": assessment.subject_type,
                "subject_id": assessment.subject_id,
                "level": assessment.level.value,
                "reason": assessment.reason,
                "evaluated_at": assessment.evaluated_at.isoformat(),
                "policy_id": assessment.policy_id,
                "score": str(assessment.score) if assessment.score is not None else None,
                "policy_version": assessment.policy_version,
                "field_path": assessment.field_path,
                "source_id": assessment.source_id,
                "provenance_id": assessment.provenance_id,
                "factors": [factor.to_dict() for factor in assessment.factors],
                "correlation_id": assessment.correlation_id,
                "checksum": assessment.checksum,
                "metadata": _encode(assessment.metadata),
            }
            _atomic_write_json(target, data)
            self._cache[assessment.assessment_id] = assessment
            return assessment

    def get_assessment(self, assessment_id: str) -> Optional[ConfidenceAssessment]:
        validate_safe_identifier(assessment_id, field_name="assessment_id")
        with self._lock:
            return self._cache.get(assessment_id)

    def find_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
        field_path: Optional[str] = None,
    ) -> Sequence[ConfidenceAssessment]:
        with self._lock:
            matches = []
            subject_type_value = subject_type.value if hasattr(subject_type, "value") else (str(subject_type) if subject_type else None)
            for assessment in self._cache.values():
                if assessment.subject_id != subject_id:
                    continue
                if subject_type_value is not None and assessment.subject_type != subject_type_value:
                    continue
                if field_path is not None and assessment.field_path != field_path:
                    continue
                matches.append(assessment)
            return tuple(sorted(matches, key=lambda item: item.evaluated_at, reverse=True))

    def get_latest_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[str] = None,
        field_path: Optional[str] = None,
    ) -> Optional[ConfidenceAssessment]:
        matches = self.find_by_subject(subject_id, subject_type, field_path)
        return matches[0] if matches else None
