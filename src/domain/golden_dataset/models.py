"""
Modelos de dominio para Golden Datasets (Hito K.5).

Define:
- GoldenDatasetStatus: DRAFT, VALIDATED, DEPRECATED.
- GoldenDatasetProvenance: MANUAL_CURATED, MIGRATED_FROM_TEST_FIXTURES, GENERATED_FROM_VALIDATED_SCENARIOS, ENGINEERING_SPEC.
- GoldenDatasetCuratorType: SYSTEM, USER, TEAM, IMPORT, MIGRATION.
- GoldenDatasetCurator: Identificación estructurada e inmutable del curador.
- DatasetCaseReference: Referencia canónica a un EvaluationCase (case_id, version, checksum_or_hash).
- GoldenDatasetManifest: Manifiesto determinista e inmutable que define la composición exacta y el checksum del dataset.
- GoldenDataset: Entidad inmutable raíz que representa un conjunto canónico versionado de casos de evaluación.

Principios K.5:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- K.5 responde: WHICH DATASET, WHICH VERSION, WHICH CASES, WHO/WHAT CURATED IT, WHEN, WITH WHICH PROVENANCE, WITH WHICH EXPECTED CRITERIA, IS IT REPRODUCIBLE.
- Reutiliza EvaluationCase de K.4 por referencia (case_id, version).
- Checksum determinista SHA-256 basado en contenido y orden canónico de casos.
- Idempotencia estricta por (dataset_id, version).
- Conflictos detectados si se intenta guardar mismo (dataset_id, version) con checksum diferente.
- Versiones VALIDATED o DEPRECATED son inmutables in-place.
- Sanitización recursiva de secretos.
- NO ejecuta Quality Gates (K.6).
- NO define thresholds de aceptación de release ni bloquea despliegues.
- NO autogenera datasets con LLM sin curación.
- NO invade Hito L (Data Quality / Master Data Management).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, List, Sequence, Union
import re

from src.domain.evaluation.models import EvaluationCase, _sanitize_eval_data, SENSITIVE_KEYS


_SEMVER_PATTERN = re.compile(
    r"^(?:v)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def parse_semver(version: str) -> Tuple[int, int, int, Tuple[Tuple[int, Union[int, str]], ...]]:
    """Valida SemVer 2.0.0 y devuelve una clave comparable determinista."""
    match = _SEMVER_PATTERN.fullmatch(str(version).strip())
    if not match:
        raise ValueError(f"Invalid semantic version: {version}")
    major, minor, patch, prerelease = match.groups()
    if prerelease is None:
        pre_key = ((1, ""),)
    else:
        identifiers = []
        for item in prerelease.split("."):
            if item.isdigit():
                if len(item) > 1 and item.startswith("0"):
                    raise ValueError(f"Invalid semantic version: {version}")
                identifiers.append((0, int(item)))
            else:
                identifiers.append((0, item))
        pre_key = tuple(identifiers)
    return int(major), int(minor), int(patch), pre_key


def semantic_version_key(version: str) -> Tuple[Any, ...]:
    major, minor, patch, prerelease = parse_semver(version)
    normalized_pre = tuple(
        (0, value) if isinstance(value, int) else (1, value)
        for _, value in prerelease
    )
    is_release = prerelease == ((1, ""),)
    return major, minor, patch, 1 if is_release else 0, normalized_pre


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple, set)):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_thaw(v) for v in value]
    return value


class GoldenDatasetStatus(str, Enum):
    """
    Estados del ciclo de vida de un Golden Dataset (K.5).
    - DRAFT: En proceso de curación o ensamblado inicial.
    - VALIDATED: Estructura, casos y checksums formalmente validados e inmutables.
    - DEPRECATED: Dataset histórico obsoleto pero preservado de forma reproducible.
    """
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    DEPRECATED = "DEPRECATED"


class GoldenDatasetProvenance(str, Enum):
    """
    Origen canónico y verificable de curación del dataset.
    """
    MANUAL_CURATED = "MANUAL_CURATED"
    MIGRATED_FROM_TEST_FIXTURES = "MIGRATED_FROM_TEST_FIXTURES"
    GENERATED_FROM_VALIDATED_SCENARIOS = "GENERATED_FROM_VALIDATED_SCENARIOS"
    ENGINEERING_SPEC = "ENGINEERING_SPEC"


class GoldenDatasetCuratorType(str, Enum):
    """
    Tipo de entidad responsable de la curación.
    """
    SYSTEM = "SYSTEM"
    USER = "USER"
    TEAM = "TEAM"
    IMPORT = "IMPORT"
    MIGRATION = "MIGRATION"


@dataclass(frozen=True)
class GoldenDatasetCurator:
    """
    Identificación inmutable de quién o qué curó el dataset.
    """
    curator_type: GoldenDatasetCuratorType
    curator_id: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        sanitized = _sanitize_eval_data(self.details)
        object.__setattr__(self, "details", _deep_freeze(sanitized))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "curator_type": self.curator_type.value,
            "curator_id": self.curator_id,
            "details": _deep_thaw(self.details),
        }


@dataclass(frozen=True)
class DatasetCaseReference:
    """
    Referencia estructurada e inmutable a un EvaluationCase de K.4.
    """
    case_id: str
    case_version: str = "1.0.0"
    evaluation_type: Optional[str] = None
    tags: Tuple[str, ...] = field(default_factory=tuple)
    expected_criteria_hash: str = ""
    case_fingerprint: str = ""

    def __post_init__(self):
        parse_semver(self.case_version)
        object.__setattr__(self, "tags", tuple(sorted(tuple(self.tags))))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_version": self.case_version,
            "evaluation_type": self.evaluation_type,
            "tags": list(self.tags),
            "expected_criteria_hash": self.expected_criteria_hash,
            "case_fingerprint": self.case_fingerprint or self.expected_criteria_hash,
        }


def compute_case_fingerprint(case: EvaluationCase) -> str:
    """Fija el contenido ejecutable completo del caso, no sólo sus expectativas."""
    payload = {
        "case_id": case.case_id,
        "version": case.version,
        "evaluation_type": case.evaluation_type.value,
        "input_reference": _sanitize_eval_data(case.input_reference),
        "expected_criteria": _sanitize_eval_data(case.expected_criteria),
        "provenance": case.provenance,
    }
    canonical_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_case_criteria_hash(expected_criteria: Mapping[str, Any]) -> str:
    """Compatibilidad: calcula un hash SHA-256 de criterios esperados."""
    sanitized = _sanitize_eval_data(expected_criteria)
    canonical_json = json.dumps(sanitized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_dataset_manifest_checksum(
    dataset_id: str,
    version: str,
    schema_version: str,
    case_references: Sequence[DatasetCaseReference],
    domain_scope: str = "",
    tags: Sequence[str] = (),
    baseline_metrics: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Calcula un checksum SHA-256 canónico y determinista del manifiesto.
    No depende de timestamps ni de orden de inserción de archivos en disco.
    """
    # Ordenar referencias canónicamente por case_id
    sorted_cases = sorted(case_references, key=lambda c: (c.case_id, c.case_version))
    cases_payload = [c.to_dict() for c in sorted_cases]
    sorted_tags = sorted(list(tags))

    manifest_dict = {
        "dataset_id": str(dataset_id).strip(),
        "version": str(version).strip(),
        "schema_version": str(schema_version).strip(),
        "domain_scope": str(domain_scope).strip(),
        "tags": sorted_tags,
        "cases": cases_payload,
        "baseline_metrics": _deep_thaw(
            _deep_freeze(_sanitize_eval_data(baseline_metrics or {}))
        ),
    }
    canonical_json = json.dumps(manifest_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GoldenDatasetManifest:
    """
    Manifiesto determinista e inmutable que describe la pertenencia exacta de casos.
    """
    dataset_id: str
    version: str
    schema_version: str
    checksum: str
    case_references: Tuple[DatasetCaseReference, ...]
    domain_scope: str = ""
    tags: Tuple[str, ...] = field(default_factory=tuple)
    baseline_metrics: Mapping[str, Any] = field(default_factory=dict)
    provenance: GoldenDatasetProvenance = GoldenDatasetProvenance.MANUAL_CURATED
    curator: Optional[GoldenDatasetCurator] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        parse_semver(self.version)
        parse_semver(self.schema_version)
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))

        sorted_cases = tuple(sorted(tuple(self.case_references), key=lambda c: (c.case_id, c.case_version)))
        object.__setattr__(self, "case_references", sorted_cases)
        object.__setattr__(self, "tags", tuple(sorted(tuple(self.tags))))

        sanitized_meta = _sanitize_eval_data(self.metadata)
        object.__setattr__(self, "metadata", _deep_freeze(sanitized_meta))
        sanitized_metrics = _sanitize_eval_data(self.baseline_metrics)
        object.__setattr__(
            self,
            "baseline_metrics",
            _deep_freeze(sanitized_metrics),
        )

        # Calcular o verificar checksum si no se proporcionó
        expected_checksum = compute_dataset_manifest_checksum(
            dataset_id=self.dataset_id,
            version=self.version,
            schema_version=self.schema_version,
            case_references=self.case_references,
            domain_scope=self.domain_scope,
            tags=self.tags,
            baseline_metrics=self.baseline_metrics,
        )
        if not self.checksum:
            object.__setattr__(self, "checksum", expected_checksum)
        elif self.checksum != expected_checksum:
            raise ValueError(
                f"Manifest checksum mismatch: expected {expected_checksum}, got {self.checksum}"
            )

    @property
    def case_count(self) -> int:
        return len(self.case_references)

    @property
    def case_ids(self) -> Tuple[str, ...]:
        return tuple(c.case_id for c in self.case_references)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "schema_version": self.schema_version,
            "checksum": self.checksum,
            "domain_scope": self.domain_scope,
            "tags": list(self.tags),
            "case_count": self.case_count,
            "case_references": [c.to_dict() for c in self.case_references],
            "baseline_metrics": _deep_thaw(self.baseline_metrics),
            "provenance": self.provenance.value,
            "curator": self.curator.to_dict() if self.curator else None,
            "created_at": self.created_at.isoformat(),
            "metadata": _deep_thaw(self.metadata),
        }


@dataclass(frozen=True)
class GoldenDataset:
    """
    Entidad inmutable raíz que representa un Golden Dataset canónico (K.5).
    """
    dataset_id: str
    name: str
    description: str
    version: str
    schema_version: str
    status: GoldenDatasetStatus
    manifest: GoldenDatasetManifest
    domain_scope: str = ""
    tags: Tuple[str, ...] = field(default_factory=tuple)
    curator: GoldenDatasetCurator = field(
        default_factory=lambda: GoldenDatasetCurator(
            curator_type=GoldenDatasetCuratorType.SYSTEM,
            curator_id="system_curator",
        )
    )
    provenance: GoldenDatasetProvenance = GoldenDatasetProvenance.MANUAL_CURATED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    curated_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        parse_semver(self.version)
        parse_semver(self.schema_version)
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if self.curated_at and self.curated_at.tzinfo is None:
            object.__setattr__(self, "curated_at", self.curated_at.replace(tzinfo=timezone.utc))

        object.__setattr__(self, "tags", tuple(sorted(tuple(self.tags))))
        sanitized_meta = _sanitize_eval_data(self.metadata)
        object.__setattr__(self, "metadata", _deep_freeze(sanitized_meta))

        if self.dataset_id != self.manifest.dataset_id or self.version != self.manifest.version:
            raise ValueError("Dataset identity/version must match manifest")
        if self.schema_version != self.manifest.schema_version:
            raise ValueError("Dataset schema_version must match manifest")
        if self.domain_scope != self.manifest.domain_scope or self.tags != self.manifest.tags:
            raise ValueError("Dataset scope/tags must match manifest")
        if self.provenance != self.manifest.provenance:
            raise ValueError("Dataset provenance must match manifest")
        if self.manifest.curator is not None and self.curator != self.manifest.curator:
            raise ValueError("Dataset curator must match manifest")

    @property
    def checksum(self) -> str:
        return self.manifest.checksum

    @property
    def case_ids(self) -> Tuple[str, ...]:
        return self.manifest.case_ids

    @property
    def case_count(self) -> int:
        return self.manifest.case_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "checksum": self.checksum,
            "domain_scope": self.domain_scope,
            "tags": list(self.tags),
            "case_count": self.case_count,
            "case_ids": list(self.case_ids),
            "curator": self.curator.to_dict(),
            "provenance": self.provenance.value,
            "manifest": self.manifest.to_dict(),
            "created_at": self.created_at.isoformat(),
            "curated_at": self.curated_at.isoformat() if self.curated_at else None,
            "metadata": _deep_thaw(self.metadata),
        }
