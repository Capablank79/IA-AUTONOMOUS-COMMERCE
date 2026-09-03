"""
Modelos de dominio para Duplicate Detection (Hito L.7 - Transversal Data Quality / Governance).

Define:
- DuplicateStatus: Estados canónicos (DUPLICATE, NOT_DUPLICATE, POSSIBLE_DUPLICATE, EXACT_DUPLICATE, REPLAY_DUPLICATE, UNKNOWN, ERROR).
- DuplicateReasonCode: Razones estructuradas y deterministas.
- DuplicateCandidate: Representación inmutable de un registro candidato a evaluar.
- DuplicateDetectionPolicy: Política inmutable y versionada que define campos de identidad, campos ignorados, source-sensitive behavior, ventana temporal y versión.
- DuplicateDetectionResult: Resultado inmutable y auditable de la detección entre candidatos o contra un conjunto de registros.
- DuplicateGroup: Agregado inmutable que agrupa registros duplicados identificados sin fusionar ni borrar.

Principios L.7:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- L.6 responde: "¿Representan la misma entidad lógica?".
- L.7 responde: "¿Estos registros concretos son el mismo hecho lógico repetido?".
- SAME ENTITY != DUPLICATE (Misma entidad con diferente precio/fecha u otra fuente independiente NO es duplicado automáticamente).
- L.7 NO es Conflict Resolution L.8 (L.7 no decide qué valor gana ante discrepancias ni borra registros).
- No destructive merge: Nunca borrar ni fusionar registros físicos.
- Preservar source_id, provenance_id y correlation_id.
- Fingerprint semántico determinista SHA-256 (keys ordenadas, normalizado, excluye secretos y ruido técnico, no usa hash() de Python).
- Preservación rigurosa de UNKNOWN (!= NOT_DUPLICATE) y POSSIBLE_DUPLICATE (!= DUPLICATE).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
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
from src.domain.freshness.models import validate_semver


class DuplicateStatus(str, Enum):
    """
    Estados canónicos de detección de duplicados.
    UNKNOWN permanece estrictamente separado de NOT_DUPLICATE y POSSIBLE_DUPLICATE.
    """
    DUPLICATE = "DUPLICATE"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    REPLAY_DUPLICATE = "REPLAY_DUPLICATE"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    NOT_DUPLICATE = "NOT_DUPLICATE"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class DuplicateReasonCode(str, Enum):
    """Códigos estructurados y deterministas que justifican la decisión de duplicación."""
    EXACT_SEMANTIC_MATCH = "EXACT_SEMANTIC_MATCH"
    REPLAY_PAYLOAD_MATCH = "REPLAY_PAYLOAD_MATCH"
    SAME_ENTITY_IDENTICAL_OBSERVATION = "SAME_ENTITY_IDENTICAL_OBSERVATION"
    SAME_ENTITY_DISTINCT_TEMPORAL_EVENT = "SAME_ENTITY_DISTINCT_TEMPORAL_EVENT"
    SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE = "SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE"
    DIFFERENT_CANONICAL_ENTITY = "DIFFERENT_CANONICAL_ENTITY"
    SEMANTIC_PAYLOAD_MISMATCH = "SEMANTIC_PAYLOAD_MISMATCH"
    OUTSIDE_TEMPORAL_WINDOW = "OUTSIDE_TEMPORAL_WINDOW"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    POLICY_MISMATCH = "POLICY_MISMATCH"
    AMBIGUOUS_EVIDENCE = "AMBIGUOUS_EVIDENCE"


def normalize_value(val: Any) -> Any:
    """Normaliza recursivamente tipos y valores para fingerprint determinista."""
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
        # Colapsar espacios múltiples internos
        return re.sub(r"\s+", " ", norm)
    if isinstance(val, (list, tuple, set)):
        return [normalize_value(item) for item in val]
    if isinstance(val, (dict, MappingProxyType)):
        sorted_dict = {}
        for k in sorted(val.keys(), key=lambda x: str(x).lower()):
            key_text = str(k).strip().lower()
            # Omitir secretos
            if any(s in key_text for s in SENSITIVE_KEYS):
                continue
            v = val[k]
            if isinstance(v, str):
                v_norm = unicodedata.normalize("NFKC", v).strip().lower()
                sorted_dict[key_text] = re.sub(r"\s+", " ", v_norm)
            else:
                sorted_dict[key_text] = normalize_value(v)
        return sorted_dict
    if hasattr(val, "value"):
        return val.value
    return str(val).strip()


def compute_semantic_fingerprint(
    payload: Mapping[str, Any],
    identity_fields: Optional[Sequence[str]] = None,
    ignored_fields: Optional[Sequence[str]] = None,
    canonical_entity_id: Optional[str] = None,
) -> str:
    """
    Calcula un fingerprint determinista SHA-256 sobre el payload semántico.
    - Keys ordenadas
    - Normalización de texto/números/fechas
    - Exclusión estricta de secretos y ruido técnico (ignored_fields)
    - Opcionalmente incluye canonical_entity_id si se provee
    - NO usa hash() de Python.
    """
    ignored_set = set(ignored_fields or ())
    # Añadir siempre ruido técnico estándar si no fue explícito
    tech_noise = {"_id", "trace_id", "span_id", "internal_row_id", "temp_id"}
    all_ignored = ignored_set.union(tech_noise)

    filtered_payload: Dict[str, Any] = {}

    if identity_fields:
        for field in identity_fields:
            if field in payload and field not in all_ignored:
                key_text = str(field).strip()
                if not any(s in key_text.lower() for s in SENSITIVE_KEYS):
                    filtered_payload[key_text] = payload[field]
    else:
        for k, v in payload.items():
            key_text = str(k).strip()
            if key_text in all_ignored:
                continue
            if any(s in key_text.lower() for s in SENSITIVE_KEYS):
                continue
            filtered_payload[key_text] = v

    norm_dict = normalize_value(filtered_payload)

    # Normalizar canonical_entity_id a lowercase
    canonical_container = {
        "canonical_entity_id": (canonical_entity_id or "").strip().lower(),
        "semantic_payload": norm_dict,
    }

    serialized = json.dumps(
        canonical_container,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DuplicateCandidate:
    """
    Representación inmutable de un registro candidato para detección de duplicados.
    """
    record_id: str
    source_id: str
    payload: Mapping[str, Any]
    observed_at: Optional[datetime] = None
    canonical_entity_id: Optional[str] = None
    provenance_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    fingerprint: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.record_id, "record_id")
        validate_safe_identifier(self.source_id, "source_id")
        if self.canonical_entity_id is not None:
            validate_safe_identifier(self.canonical_entity_id, "canonical_entity_id")
        if self.provenance_id is not None:
            validate_safe_identifier(self.provenance_id, "provenance_id")
        if self.idempotency_key is not None:
            validate_safe_identifier(self.idempotency_key, "idempotency_key")

        # Inmutabilidad profunda y sanitización
        sanitized_payload = sanitize_security_data(dict(self.payload))
        object.__setattr__(self, "payload", deep_freeze(sanitized_payload))
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if self.observed_at is not None and self.observed_at.tzinfo is None:
            object.__setattr__(self, "observed_at", self.observed_at.replace(tzinfo=timezone.utc))

        if self.fingerprint is None:
            fp = compute_semantic_fingerprint(
                payload=self.payload,
                canonical_entity_id=self.canonical_entity_id,
            )
            object.__setattr__(self, "fingerprint", fp)


def compute_duplicate_candidate_checksum(candidate: DuplicateCandidate) -> str:
    """Calcula el checksum SHA-256 de un DuplicateCandidate."""
    data = {
        "record_id": candidate.record_id,
        "source_id": candidate.source_id,
        "payload": dict(candidate.payload),
        "observed_at": candidate.observed_at.astimezone(timezone.utc).isoformat() if candidate.observed_at else None,
        "canonical_entity_id": candidate.canonical_entity_id,
        "provenance_id": candidate.provenance_id,
        "idempotency_key": candidate.idempotency_key,
        "fingerprint": candidate.fingerprint,
        "metadata": dict(candidate.metadata),
    }
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DuplicateDetectionPolicy:
    """
    Política versionada e inmutable para evaluación de duplicados.
    """
    policy_id: str
    name: str
    version: str
    identity_fields: Tuple[str, ...] = field(default_factory=tuple)
    ignored_fields: Tuple[str, ...] = field(default_factory=tuple)
    require_same_source: bool = False
    allow_cross_source_duplicates: bool = False
    temporal_window_seconds: Optional[int] = None
    allow_replay_idempotency: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.policy_id, "policy_id")
        validate_semver(self.version)
        if not self.name or not self.name.strip():
            raise ValueError("policy name cannot be empty")
        if self.temporal_window_seconds is not None and self.temporal_window_seconds < 0:
            raise ValueError("temporal_window_seconds cannot be negative")

        object.__setattr__(self, "identity_fields", tuple(sorted(set(self.identity_fields))))
        object.__setattr__(self, "ignored_fields", tuple(sorted(set(self.ignored_fields))))

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if self.checksum is None:
            chk = compute_duplicate_policy_checksum(self)
            object.__setattr__(self, "checksum", chk)


def compute_duplicate_policy_checksum(policy: DuplicateDetectionPolicy) -> str:
    """Calcula el checksum SHA-256 de una DuplicateDetectionPolicy."""
    data = {
        "policy_id": policy.policy_id,
        "name": policy.name,
        "version": policy.version,
        "identity_fields": list(policy.identity_fields),
        "ignored_fields": list(policy.ignored_fields),
        "require_same_source": policy.require_same_source,
        "allow_cross_source_duplicates": policy.allow_cross_source_duplicates,
        "temporal_window_seconds": policy.temporal_window_seconds,
        "allow_replay_idempotency": policy.allow_replay_idempotency,
        "metadata": dict(policy.metadata),
    }
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DuplicateDetectionResult:
    """
    Resultado inmutable de la evaluación de duplicación entre dos registros o candidato vs existente.
    """
    result_id: str
    primary_record_id: str
    secondary_record_id: str
    status: DuplicateStatus
    reason_code: DuplicateReasonCode
    policy_id: str
    policy_version: str
    primary_fingerprint: str
    secondary_fingerprint: str
    evaluated_at: datetime
    is_exact_replay: bool = False
    confidence_score: Decimal = Decimal("1.0000")
    details: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.result_id, "result_id")
        validate_safe_identifier(self.primary_record_id, "primary_record_id")
        validate_safe_identifier(self.secondary_record_id, "secondary_record_id")
        validate_safe_identifier(self.policy_id, "policy_id")
        validate_semver(self.policy_version)

        if isinstance(self.status, str):
            object.__setattr__(self, "status", DuplicateStatus(self.status))
        if isinstance(self.reason_code, str):
            object.__setattr__(self, "reason_code", DuplicateReasonCode(self.reason_code))

        if not isinstance(self.confidence_score, Decimal):
            try:
                object.__setattr__(self, "confidence_score", Decimal(str(self.confidence_score)))
            except (InvalidOperation, TypeError):
                raise ValueError(f"Invalid confidence_score: {self.confidence_score}")

        if not (Decimal("0.0000") <= self.confidence_score <= Decimal("1.0000")):
            raise ValueError("confidence_score must be between 0.0000 and 1.0000")

        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))

        sanitized_details = sanitize_security_data(dict(self.details))
        object.__setattr__(self, "details", deep_freeze(sanitized_details))

        if self.checksum is None:
            chk = compute_duplicate_result_checksum(self)
            object.__setattr__(self, "checksum", chk)


def compute_duplicate_result_checksum(result: DuplicateDetectionResult) -> str:
    """Calcula el checksum SHA-256 de un DuplicateDetectionResult."""
    data = {
        "result_id": result.result_id,
        "primary_record_id": result.primary_record_id,
        "secondary_record_id": result.secondary_record_id,
        "status": result.status.value,
        "reason_code": result.reason_code.value,
        "policy_id": result.policy_id,
        "policy_version": result.policy_version,
        "primary_fingerprint": result.primary_fingerprint,
        "secondary_fingerprint": result.secondary_fingerprint,
        "evaluated_at": result.evaluated_at.astimezone(timezone.utc).isoformat(),
        "is_exact_replay": result.is_exact_replay,
        "confidence_score": str(result.confidence_score),
        "details": dict(result.details),
    }
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DuplicateGroup:
    """
    Agregado inmutable que agrupa registros identificados como duplicados.
    No borra ni fusiona datos: sólo vincula miembros y preserva trazabilidad.
    """
    group_id: str
    canonical_fingerprint: str
    member_record_ids: Tuple[str, ...]
    canonical_entity_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.group_id, "group_id")
        if self.canonical_entity_id is not None:
            validate_safe_identifier(self.canonical_entity_id, "canonical_entity_id")

        object.__setattr__(self, "member_record_ids", tuple(sorted(set(self.member_record_ids))))

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if self.created_at is not None and self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if self.updated_at is not None and self.updated_at.tzinfo is None:
            object.__setattr__(self, "updated_at", self.updated_at.replace(tzinfo=timezone.utc))

        if self.checksum is None:
            chk = compute_duplicate_group_checksum(self)
            object.__setattr__(self, "checksum", chk)


def compute_duplicate_group_checksum(group: DuplicateGroup) -> str:
    """Calcula el checksum SHA-256 de un DuplicateGroup."""
    data = {
        "group_id": group.group_id,
        "canonical_fingerprint": group.canonical_fingerprint,
        "member_record_ids": list(group.member_record_ids),
        "canonical_entity_id": group.canonical_entity_id or "",
        "created_at": group.created_at.astimezone(timezone.utc).isoformat() if group.created_at else "",
        "updated_at": group.updated_at.astimezone(timezone.utc).isoformat() if group.updated_at else "",
        "metadata": dict(group.metadata),
    }
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
