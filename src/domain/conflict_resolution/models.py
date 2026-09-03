"""
Modelos de dominio para Conflict Resolution (Hito L.8 - Transversal Data Quality / Governance).

Define:
- ConflictStatus: Estados canónicos (RESOLVED, UNRESOLVED, NO_CONFLICT, UNKNOWN, ERROR).
- ConflictReasonCode: Códigos estructurados y deterministas que justifican la decisión de resolución.
- ResolutionStrategy: Estrategias de resolución (SOURCE_PRIORITY, FRESHEST, HIGHEST_CONFIDENCE, CONSENSUS, MANUAL_REQUIRED).
- ConflictCandidate: Representación inmutable de un candidato a resolución de conflicto sobre una entidad/campo.
- ConflictResolutionPolicy: Política inmutable y versionada que define estrategia, precedencia de fuentes, requerimientos de frescura/confianza, reglas de consenso y comportamiento de desempate.
- ConflictResolutionResult: Resultado inmutable, determinista y auditable de la resolución.

Principios L.8:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- L.8 responde: "Cuando dos o más datos válidos sobre la misma entidad/hecho se contradicen, ¿cómo resolvemos el conflicto de forma explícita y reproducible?".
- REUSE > EXTEND > CREATE: Reutiliza L.1 (Source Registry), L.2 (Data Provenance), L.3 (Freshness), L.4 (Confidence), L.6 (Entity Resolution), L.7 (Duplicate Detection).
- No destructive merge / No evidence deletion: Nunca borrar ni sobrescribir valores originales ni procedencias.
- Preservar todas las referencias a los candidatos en ConflictResolutionResult.
- Determinismo estricto: Mismos candidatos + misma policy/versión + mismos assessments -> idéntico resultado lógico. (Sin random, sin uuid como id lógico, sin hash() de Python, sin iteración desordenada).
- NO "fuente ganadora" global ni hardcodeada sin policy versionada explícita.
- Empates o falta de evidencia concluyente -> UNRESOLVED seguro (selected_value = None).
- UNKNOWN freshness / UNKNOWN confidence nunca ganan silenciosamente ni se tratan como HIGH/FRESH.
- Los registros duplicados/replays (L.7) no inflan el consenso.
- Integridad verificable por checksums SHA-256 canónicos.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List, Union
import unicodedata

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
    SENSITIVE_KEYS,
)
from src.domain.freshness.models import FreshnessStatus, validate_semver
from src.domain.confidence.models import ConfidenceLevel


class ConflictStatus(str, Enum):
    """
    Estados canónicos de resolución de conflictos.
    UNKNOWN y UNRESOLVED permanecen estrictamente diferenciados.
    """
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    NO_CONFLICT = "NO_CONFLICT"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class ConflictReasonCode(str, Enum):
    """Códigos estructurados y deterministas que justifican la decisión."""
    NO_CONFLICT_SINGLE_CANDIDATE = "NO_CONFLICT_SINGLE_CANDIDATE"
    NO_CONFLICT_IDENTICAL_VALUES = "NO_CONFLICT_IDENTICAL_VALUES"
    NO_CONFLICT_DIFFERENT_TEMPORAL_FACTS = "NO_CONFLICT_DIFFERENT_TEMPORAL_FACTS"
    NO_CONFLICT_DIFFERENT_ENTITIES = "NO_CONFLICT_DIFFERENT_ENTITIES"
    RESOLVED_BY_SOURCE_PRIORITY = "RESOLVED_BY_SOURCE_PRIORITY"
    RESOLVED_BY_FRESHEST = "RESOLVED_BY_FRESHEST"
    RESOLVED_BY_HIGHEST_CONFIDENCE = "RESOLVED_BY_HIGHEST_CONFIDENCE"
    RESOLVED_BY_CONSENSUS = "RESOLVED_BY_CONSENSUS"
    UNRESOLVED_TIE = "UNRESOLVED_TIE"
    UNRESOLVED_INSUFFICIENT_EVIDENCE = "UNRESOLVED_INSUFFICIENT_EVIDENCE"
    UNRESOLVED_NO_CONSENSUS = "UNRESOLVED_NO_CONSENSUS"
    UNRESOLVED_EXPIRED_OR_STALE = "UNRESOLVED_EXPIRED_OR_STALE"
    UNRESOLVED_UNKNOWN_ASSESSMENTS = "UNRESOLVED_UNKNOWN_ASSESSMENTS"
    MANUAL_RESOLUTION_REQUIRED = "MANUAL_RESOLUTION_REQUIRED"
    MISSING_POLICY = "MISSING_POLICY"
    INVALID_CANDIDATES = "INVALID_CANDIDATES"
    DUPLICATE_INFLATION_PREVENTED = "DUPLICATE_INFLATION_PREVENTED"
    CORRUPTED_CANDIDATE_DATA = "CORRUPTED_CANDIDATE_DATA"


class ResolutionStrategy(str, Enum):
    """Estrategias explícitas de resolución de conflictos."""
    SOURCE_PRIORITY = "SOURCE_PRIORITY"
    FRESHEST = "FRESHEST"
    HIGHEST_CONFIDENCE = "HIGHEST_CONFIDENCE"
    CONSENSUS = "CONSENSUS"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"


def normalize_conflict_value(val: Any) -> Any:
    """Normaliza recursivamente tipos y valores para comparación determinista."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int,)):
        return val
    if isinstance(val, (float,)):
        return str(Decimal(str(val)))
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc).isoformat()
    if isinstance(val, str):
        norm = unicodedata.normalize("NFKC", val).strip()
        return re.sub(r"\s+", " ", norm)
    if isinstance(val, (list, tuple, set)):
        return [normalize_conflict_value(item) for item in val]
    if isinstance(val, (dict, MappingProxyType)):
        sorted_dict = {}
        for k in sorted(val.keys(), key=lambda x: str(x).lower()):
            key_text = str(k).strip().lower()
            if any(s in key_text for s in SENSITIVE_KEYS):
                continue
            sorted_dict[key_text] = normalize_conflict_value(val[k])
        return sorted_dict
    if hasattr(val, "value"):
        return val.value
    return str(val).strip()


@dataclass(frozen=True)
class ConflictCandidate:
    """
    Representación inmutable de un valor candidato proporcionado por una fuente sobre una entidad/campo.
    """
    candidate_id: str
    source_id: str
    record_id: str
    canonical_entity_id: str
    field_path: str
    value: Any
    observed_at: Optional[datetime] = None
    provenance_id: Optional[str] = None
    freshness_status: Optional[FreshnessStatus] = None
    freshness_age_seconds: Optional[Decimal] = None
    confidence_level: Optional[ConfidenceLevel] = None
    confidence_score: Optional[Decimal] = None
    deduplication_fingerprint: Optional[str] = None
    is_duplicate: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.candidate_id, "candidate_id")
        validate_safe_identifier(self.source_id, "source_id")
        validate_safe_identifier(self.record_id, "record_id")
        validate_safe_identifier(self.canonical_entity_id, "canonical_entity_id")
        if not self.field_path or not self.field_path.strip():
            raise ValueError("field_path cannot be empty")
        if self.provenance_id is not None:
            validate_safe_identifier(self.provenance_id, "provenance_id")
        if self.confidence_score is not None:
            if not isinstance(self.confidence_score, Decimal):
                raise ValueError(f"confidence_score must be Decimal, got {type(self.confidence_score)}")
            if self.confidence_score < Decimal("0") or self.confidence_score > Decimal("1"):
                raise ValueError("confidence_score must be between 0 and 1")
        if self.freshness_age_seconds is not None:
            if not isinstance(self.freshness_age_seconds, Decimal):
                raise ValueError(f"freshness_age_seconds must be Decimal, got {type(self.freshness_age_seconds)}")
            if self.freshness_age_seconds < Decimal("0"):
                raise ValueError("freshness_age_seconds cannot be negative")

        # Inmutabilidad y sanitización
        sanitized_val = sanitize_security_data(self.value) if isinstance(self.value, (dict, list)) else self.value
        object.__setattr__(self, "value", deep_freeze(sanitized_val) if isinstance(sanitized_val, (dict, list)) else sanitized_val)
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if self.observed_at is not None and self.observed_at.tzinfo is None:
            object.__setattr__(self, "observed_at", self.observed_at.replace(tzinfo=timezone.utc))


def compute_candidate_checksum(candidate: ConflictCandidate) -> str:
    """Calcula el checksum SHA-256 canónico de un ConflictCandidate."""
    data = {
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "record_id": candidate.record_id,
        "canonical_entity_id": candidate.canonical_entity_id,
        "field_path": candidate.field_path,
        "value": normalize_conflict_value(candidate.value),
        "observed_at": candidate.observed_at.astimezone(timezone.utc).isoformat() if candidate.observed_at else None,
        "provenance_id": candidate.provenance_id,
        "freshness_status": candidate.freshness_status.value if candidate.freshness_status else None,
        "freshness_age_seconds": str(candidate.freshness_age_seconds) if candidate.freshness_age_seconds is not None else None,
        "confidence_level": candidate.confidence_level.value if candidate.confidence_level else None,
        "confidence_score": str(candidate.confidence_score) if candidate.confidence_score is not None else None,
        "deduplication_fingerprint": candidate.deduplication_fingerprint,
        "is_duplicate": candidate.is_duplicate,
        "metadata": dict(candidate.metadata),
    }
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConflictResolutionPolicy:
    """
    Política versionada e inmutable de resolución de conflictos.
    """
    policy_id: str
    name: str
    version: str
    applicable_subject_type: Optional[str] = None
    applicable_field_path: Optional[str] = None
    strategy: ResolutionStrategy = ResolutionStrategy.SOURCE_PRIORITY
    source_precedence: Tuple[str, ...] = field(default_factory=tuple)
    require_freshness: bool = False
    max_acceptable_age_seconds: Optional[int] = None
    min_confidence_level: Optional[ConfidenceLevel] = None
    min_confidence_score: Optional[Decimal] = None
    consensus_min_votes: int = 2
    consensus_min_ratio: Decimal = Decimal("0.6667")  # Al menos 2/3 de los votos
    tie_break_strategy: Optional[ResolutionStrategy] = None
    allow_unresolved: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.policy_id, "policy_id")
        validate_semver(self.version)
        if not self.name or not self.name.strip():
            raise ValueError("policy name cannot be empty")
        if self.max_acceptable_age_seconds is not None and self.max_acceptable_age_seconds < 0:
            raise ValueError("max_acceptable_age_seconds cannot be negative")
        if self.min_confidence_score is not None:
            if not isinstance(self.min_confidence_score, Decimal):
                raise ValueError("min_confidence_score must be Decimal")
            if self.min_confidence_score < Decimal("0") or self.min_confidence_score > Decimal("1"):
                raise ValueError("min_confidence_score must be between 0 and 1")
        if self.consensus_min_votes < 1:
            raise ValueError("consensus_min_votes must be >= 1")
        if not isinstance(self.consensus_min_ratio, Decimal) or self.consensus_min_ratio <= Decimal("0") or self.consensus_min_ratio > Decimal("1"):
            raise ValueError("consensus_min_ratio must be a Decimal between 0 (exclusive) and 1 (inclusive)")

        object.__setattr__(self, "source_precedence", tuple(self.source_precedence))
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if self.checksum is None:
            chk = compute_conflict_policy_checksum(self)
            object.__setattr__(self, "checksum", chk)


def compute_conflict_policy_checksum(policy: ConflictResolutionPolicy) -> str:
    """Calcula el checksum SHA-256 canónico de una ConflictResolutionPolicy."""
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
        "metadata": dict(policy.metadata),
    }
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConflictResolutionResult:
    """
    Resultado inmutable, determinista y auditable de una resolución de conflicto.
    """
    conflict_id: str
    canonical_entity_id: str
    field_path: str
    candidate_ids: Tuple[str, ...]
    strategy: ResolutionStrategy
    status: ConflictStatus
    reason_code: ConflictReasonCode
    selected_candidate_id: Optional[str]
    selected_value: Any
    policy_id: str
    policy_version: str
    evaluated_at: datetime
    correlation_id: str
    details: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.conflict_id, "conflict_id")
        validate_safe_identifier(self.canonical_entity_id, "canonical_entity_id")
        if not self.field_path or not self.field_path.strip():
            raise ValueError("field_path cannot be empty")
        validate_safe_identifier(self.policy_id, "policy_id")
        validate_semver(self.policy_version)
        validate_safe_identifier(self.correlation_id, "correlation_id")
        if self.selected_candidate_id is not None:
            validate_safe_identifier(self.selected_candidate_id, "selected_candidate_id")

        object.__setattr__(self, "candidate_ids", tuple(self.candidate_ids))

        sanitized_val = sanitize_security_data(self.selected_value) if isinstance(self.selected_value, (dict, list)) else self.selected_value
        object.__setattr__(self, "selected_value", deep_freeze(sanitized_val) if isinstance(sanitized_val, (dict, list)) else sanitized_val)

        sanitized_details = sanitize_security_data(dict(self.details))
        object.__setattr__(self, "details", deep_freeze(sanitized_details))

        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))

        if self.checksum is None:
            chk = compute_conflict_result_checksum(self)
            object.__setattr__(self, "checksum", chk)


def compute_conflict_result_checksum(result: ConflictResolutionResult) -> str:
    """Calcula el checksum SHA-256 canónico de un ConflictResolutionResult."""
    data = {
        "conflict_id": result.conflict_id,
        "canonical_entity_id": result.canonical_entity_id,
        "field_path": result.field_path,
        "candidate_ids": list(result.candidate_ids),
        "strategy": result.strategy.value,
        "status": result.status.value,
        "reason_code": result.reason_code.value,
        "selected_candidate_id": result.selected_candidate_id,
        "selected_value": normalize_conflict_value(result.selected_value),
        "policy_id": result.policy_id,
        "policy_version": result.policy_version,
        "evaluated_at": result.evaluated_at.astimezone(timezone.utc).isoformat(),
        "correlation_id": result.correlation_id,
        "details": dict(result.details),
    }
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
