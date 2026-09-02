"""
Modelos de dominio para Quality Gates (Hito K.6).

Define:
- GateDecisionStatus: Estados canónicos de decisión de calidad (PASS, FAIL, UNKNOWN, ERROR).
- MissingCasePolicy: Política ante ausencia de casos requeridos (FAIL, UNKNOWN, ERROR).
- UnknownCasePolicy: Política de resolución para casos UNKNOWN (UNKNOWN, FAIL).
- ErrorCasePolicy: Política de resolución para casos con ERROR (ERROR, FAIL).
- QualityGateDefinition: Definición inmutable y declarativa de una compuerta de calidad versionada.
- QualityGateDecision: Decisión inmutable y determinista generada al evaluar resultados de K.4/K.5.

Principios K.6:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- K.6 responde: "¿Los resultados de evaluación cumplen los criterios mínimos definidos?".
- K.6 consume resultados de K.4 (Evaluation Harness) y K.5 (Golden Datasets).
- K.6 NO evalúa agentes ni ejecuta business logic de nuevo.
- K.6 NO modifica PolicyEngine comercial ni confunde QualityGate con PolicyEngine.
- K.6 NO implementa K.7 (Reliability) ni K.8 (Security Checks).
- K.6 NO bloquea despliegues reales en CI/CD.
- Semántica estricta PASS/FAIL/UNKNOWN/ERROR (no ocultar UNKNOWN ni ERROR como PASS).
- Aritmética exacta Decimal para cálculo y comparación de pass_rate.
- Checksums SHA-256 canónicos e idempotencia determinista.
- Sanitización recursiva de secretos.
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


_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
    "chain_of_thought",
    "reasoning",
    "reasoning_tokens",
    "internal_scratchpad",
    "card_number",
}


def quality_gate_version_key(version: str) -> Tuple[Any, ...]:
    match = _SEMVER_PATTERN.fullmatch(str(version).strip())
    if not match:
        raise ValueError(f"Invalid semantic version: {version}")
    major, minor, patch, prerelease = match.groups()
    if prerelease is None:
        pre_key = ((1, ""),)
    else:
        pre_key = tuple(
            (0, int(item)) if item.isdigit() else (1, item)
            for item in prerelease.split(".")
        )
    return int(major), int(minor), int(patch), prerelease is None, pre_key


def _sanitize_gate_data(val: Any) -> Any:
    """Sanitiza recursivamente estructuras de datos para eliminar secretos o credenciales."""
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _sanitize_gate_data(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_sanitize_gate_data(v) for v in val]
    return val


def _deep_freeze(val: Any) -> Any:
    """Convierte recursivamente diccionarios en MappingProxyType y listas en tuplas."""
    if isinstance(val, (dict, MappingProxyType)):
        return MappingProxyType({k: _deep_freeze(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(_deep_freeze(v) for v in val)
    return val


class GateDecisionStatus(str, Enum):
    """
    Estados canónicos de decisión para una compuerta de calidad (Quality Gate K.6).
    - PASS: Todos los criterios obligatorios y umbrales mínimos se cumplen.
    - FAIL: Al menos un criterio crítico o umbral explícito fue incumplido (regresión detectada).
    - UNKNOWN: No hay suficiente evidencia o certeza determinista para decidir (indeterminación).
    - ERROR: La evaluación de la compuerta falló debido a errores de ejecución, corrupción o inconsistencia de datos.
    """
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class MissingCasePolicy(str, Enum):
    """
    Política a aplicar cuando un caso de evaluación requerido por la compuerta no está presente en los resultados.
    """
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class UnknownCasePolicy(str, Enum):
    """
    Política a aplicar cuando se encuentran casos con estado UNKNOWN.
    """
    UNKNOWN = "UNKNOWN"
    FAIL = "FAIL"


class ErrorCasePolicy(str, Enum):
    """
    Política a aplicar cuando se encuentran casos con estado ERROR en la evaluación.
    """
    ERROR = "ERROR"
    FAIL = "FAIL"


def compute_gate_definition_checksum(
    gate_id: str,
    version: str,
    required_case_ids: Sequence[str],
    critical_case_ids: Sequence[str],
    minimum_pass_rate: Optional[Decimal],
    max_failures: int,
    max_unknown: int,
    max_errors: int,
    target_dataset_id: Optional[str] = None,
    target_dataset_version: Optional[str] = None,
    target_dataset_manifest_checksum: Optional[str] = None,
    missing_case_policy: str = "FAIL",
    unknown_case_policy: str = "UNKNOWN",
    error_case_policy: str = "ERROR",
    allowed_evaluator_versions: Sequence[str] = (),
    provenance: str = "ENGINEERING_SPEC",
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Calcula un hash SHA-256 determinista para la especificación del Quality Gate."""
    sanitized_metadata = _sanitize_gate_data(dict(metadata or {}))
    payload = {
        "gate_id": str(gate_id).strip(),
        "version": str(version).strip(),
        "target_dataset_id": str(target_dataset_id).strip() if target_dataset_id else None,
        "target_dataset_version": str(target_dataset_version).strip() if target_dataset_version else None,
        "target_dataset_manifest_checksum": str(target_dataset_manifest_checksum).strip() if target_dataset_manifest_checksum else None,
        "required_case_ids": sorted(list(set(required_case_ids))),
        "critical_case_ids": sorted(list(set(critical_case_ids))),
        "minimum_pass_rate": str(minimum_pass_rate) if minimum_pass_rate is not None else None,
        "max_failures": int(max_failures),
        "max_unknown": int(max_unknown),
        "max_errors": int(max_errors),
        "missing_case_policy": str(missing_case_policy),
        "unknown_case_policy": str(unknown_case_policy),
        "error_case_policy": str(error_case_policy),
        "allowed_evaluator_versions": sorted(list(set(allowed_evaluator_versions))),
        "provenance": str(provenance).strip(),
        "metadata": sanitized_metadata,
    }
    dumped = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def compute_gate_decision_checksum(
    decision_id: str,
    gate_id: str,
    gate_version: str,
    evaluation_run_id: str,
    status: str,
    passed_count: int,
    failed_count: int,
    unknown_count: int,
    error_count: int,
    pass_rate: Optional[Decimal],
    failed_case_ids: Sequence[str],
    critical_case_failures: Sequence[str],
    missing_required_case_ids: Sequence[str],
    unknown_case_ids: Sequence[str] = (),
    error_case_ids: Sequence[str] = (),
    total_cases: int = 0,
    evaluated_count: int = 0,
    reasons: Sequence[str] = (),
    dataset_id: Optional[str] = None,
    dataset_version: Optional[str] = None,
    dataset_manifest_checksum: Optional[str] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    trace_reference: Optional[str] = None,
    audit_reference: Optional[str] = None,
    cost_reference: Optional[str] = None,
    correlation_id: str = "",
    causation_id: Optional[str] = None,
    provenance: str = "QUALITY_GATE_ENGINE",
    idempotency_key: str = "",
    schema_version: str = "1.0.0",
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Calcula un hash SHA-256 determinista para la decisión de Quality Gate."""
    sanitized_evidence = _sanitize_gate_data(dict(evidence or {}))
    sanitized_meta = _sanitize_gate_data(dict(metadata or {}))
    payload = {
        "decision_id": str(decision_id).strip(),
        "gate_id": str(gate_id).strip(),
        "gate_version": str(gate_version).strip(),
        "evaluation_run_id": str(evaluation_run_id).strip(),
        "status": str(status).strip(),
        "total_cases": int(total_cases),
        "passed_count": int(passed_count),
        "failed_count": int(failed_count),
        "unknown_count": int(unknown_count),
        "error_count": int(error_count),
        "evaluated_count": int(evaluated_count),
        "pass_rate": str(pass_rate) if pass_rate is not None else None,
        "failed_case_ids": sorted(list(failed_case_ids)),
        "critical_case_failures": sorted(list(critical_case_failures)),
        "missing_required_case_ids": sorted(list(missing_required_case_ids)),
        "unknown_case_ids": sorted(list(unknown_case_ids)),
        "error_case_ids": sorted(list(error_case_ids)),
        "reasons": list(reasons),
        "dataset_id": str(dataset_id).strip() if dataset_id else None,
        "dataset_version": str(dataset_version).strip() if dataset_version else None,
        "dataset_manifest_checksum": str(dataset_manifest_checksum).strip() if dataset_manifest_checksum else None,
        "evidence": sanitized_evidence,
        "trace_reference": str(trace_reference).strip() if trace_reference else None,
        "audit_reference": str(audit_reference).strip() if audit_reference else None,
        "cost_reference": str(cost_reference).strip() if cost_reference else None,
        "correlation_id": str(correlation_id).strip(),
        "causation_id": str(causation_id).strip() if causation_id else None,
        "provenance": str(provenance).strip(),
        "idempotency_key": str(idempotency_key).strip(),
        "schema_version": str(schema_version).strip(),
        "metadata": sanitized_meta,
    }
    dumped = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QualityGateDefinition:
    """
    Definición inmutable y declarativa de una compuerta de calidad (Quality Gate - Hito K.6).
    """
    gate_id: str
    name: str
    description: str
    version: str = "1.0.0"
    target_dataset_id: Optional[str] = None
    target_dataset_version: Optional[str] = None
    target_dataset_manifest_checksum: Optional[str] = None
    required_case_ids: Tuple[str, ...] = field(default_factory=tuple)
    critical_case_ids: Tuple[str, ...] = field(default_factory=tuple)
    minimum_pass_rate: Optional[Decimal] = None
    max_failures: int = 0
    max_unknown: int = 0
    max_errors: int = 0
    missing_case_policy: MissingCasePolicy = MissingCasePolicy.FAIL
    unknown_case_policy: UnknownCasePolicy = UnknownCasePolicy.UNKNOWN
    error_case_policy: ErrorCasePolicy = ErrorCasePolicy.ERROR
    allowed_evaluator_versions: Tuple[str, ...] = field(default_factory=tuple)
    provenance: str = "ENGINEERING_SPEC"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.gate_id or not str(self.gate_id).strip():
            raise ValueError("QualityGateDefinition requires a non-empty gate_id.")
        if not self.name or not str(self.name).strip():
            raise ValueError("QualityGateDefinition requires a non-empty name.")
        if not self.version or not str(self.version).strip():
            raise ValueError("QualityGateDefinition requires a non-empty version.")
        quality_gate_version_key(self.version)
        if bool(self.target_dataset_id) != bool(self.target_dataset_version):
            raise ValueError("target_dataset_id and target_dataset_version must be provided together.")
        if self.target_dataset_version:
            quality_gate_version_key(self.target_dataset_version)

        if self.minimum_pass_rate is not None:
            rate = Decimal(str(self.minimum_pass_rate))
            if rate < Decimal("0.0") or rate > Decimal("1.0"):
                raise ValueError(f"minimum_pass_rate must be between 0.0 and 1.0, got: {rate}")
            object.__setattr__(self, "minimum_pass_rate", rate)

        if self.max_failures < 0:
            raise ValueError("max_failures cannot be negative.")
        if self.max_unknown < 0:
            raise ValueError("max_unknown cannot be negative.")
        if self.max_errors < 0:
            raise ValueError("max_errors cannot be negative.")

        critical_case_ids = tuple(sorted(set(self.critical_case_ids)))
        required_case_ids = tuple(sorted(set(self.required_case_ids) | set(critical_case_ids)))
        allowed_evaluator_versions = tuple(sorted(set(self.allowed_evaluator_versions)))
        if any(not isinstance(case_id, str) or not case_id.strip() for case_id in required_case_ids + critical_case_ids):
            raise ValueError("Quality Gate case ids must be non-empty strings.")
        if any(not isinstance(ver, str) or not ver.strip() for ver in allowed_evaluator_versions):
            raise ValueError("allowed_evaluator_versions must contain non-empty strings.")
        object.__setattr__(self, "required_case_ids", required_case_ids)
        object.__setattr__(self, "critical_case_ids", critical_case_ids)
        object.__setattr__(self, "allowed_evaluator_versions", allowed_evaluator_versions)

        if isinstance(self.missing_case_policy, str):
            object.__setattr__(self, "missing_case_policy", MissingCasePolicy(self.missing_case_policy))
        if isinstance(self.unknown_case_policy, str):
            object.__setattr__(self, "unknown_case_policy", UnknownCasePolicy(self.unknown_case_policy))
        if isinstance(self.error_case_policy, str):
            object.__setattr__(self, "error_case_policy", ErrorCasePolicy(self.error_case_policy))

        # Sanitizar y congelar profundamente metadata
        sanitized_meta = _sanitize_gate_data(dict(self.metadata))
        object.__setattr__(self, "metadata", _deep_freeze(sanitized_meta))

        calculated_checksum = compute_gate_definition_checksum(
            gate_id=self.gate_id,
            version=self.version,
            required_case_ids=self.required_case_ids,
            critical_case_ids=self.critical_case_ids,
            minimum_pass_rate=self.minimum_pass_rate,
            max_failures=self.max_failures,
            max_unknown=self.max_unknown,
            max_errors=self.max_errors,
            target_dataset_id=self.target_dataset_id,
            target_dataset_version=self.target_dataset_version,
            target_dataset_manifest_checksum=self.target_dataset_manifest_checksum,
            missing_case_policy=self.missing_case_policy.value,
            unknown_case_policy=self.unknown_case_policy.value,
            error_case_policy=self.error_case_policy.value,
            allowed_evaluator_versions=self.allowed_evaluator_versions,
            provenance=self.provenance,
            metadata=self.metadata,
        )

        if self.checksum:
            if self.checksum != calculated_checksum:
                raise ValueError("QualityGateDefinition checksum does not match its content.")
        else:
            object.__setattr__(self, "checksum", calculated_checksum)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la definición del gate a un diccionario determinista."""
        return {
            "gate_id": self.gate_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "target_dataset_id": self.target_dataset_id,
            "target_dataset_version": self.target_dataset_version,
            "target_dataset_manifest_checksum": self.target_dataset_manifest_checksum,
            "required_case_ids": list(self.required_case_ids),
            "critical_case_ids": list(self.critical_case_ids),
            "minimum_pass_rate": str(self.minimum_pass_rate) if self.minimum_pass_rate is not None else None,
            "max_failures": self.max_failures,
            "max_unknown": self.max_unknown,
            "max_errors": self.max_errors,
            "missing_case_policy": self.missing_case_policy.value,
            "unknown_case_policy": self.unknown_case_policy.value,
            "error_case_policy": self.error_case_policy.value,
            "allowed_evaluator_versions": list(self.allowed_evaluator_versions),
            "provenance": self.provenance,
            "created_at": self.created_at.isoformat(),
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class QualityGateDecision:
    """
    Decisión formal, inmutable y determinista producida por una compuerta de calidad (Hito K.6).
    """
    decision_id: str
    gate_id: str
    gate_version: str
    status: GateDecisionStatus
    evaluation_run_id: str
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total_cases: int = 0
    passed_count: int = 0
    failed_count: int = 0
    unknown_count: int = 0
    error_count: int = 0
    evaluated_count: int = 0
    pass_rate: Optional[Decimal] = None
    failed_case_ids: Tuple[str, ...] = field(default_factory=tuple)
    unknown_case_ids: Tuple[str, ...] = field(default_factory=tuple)
    error_case_ids: Tuple[str, ...] = field(default_factory=tuple)
    missing_required_case_ids: Tuple[str, ...] = field(default_factory=tuple)
    critical_case_failures: Tuple[str, ...] = field(default_factory=tuple)
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    dataset_id: Optional[str] = None
    dataset_version: Optional[str] = None
    dataset_manifest_checksum: Optional[str] = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    trace_reference: Optional[str] = None
    audit_reference: Optional[str] = None
    cost_reference: Optional[str] = None
    correlation_id: str = ""
    causation_id: Optional[str] = None
    provenance: str = "QUALITY_GATE_ENGINE"
    idempotency_key: str = ""
    checksum: str = ""
    schema_version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def deployment_allowed(self) -> bool:
        """Contrato de decisión de despliegue: PASS permite despliegue; FAIL, UNKNOWN, ERROR lo bloquean."""
        return self.status == GateDecisionStatus.PASS

    def __post_init__(self):
        if not self.decision_id or not str(self.decision_id).strip():
            raise ValueError("QualityGateDecision requires a non-empty decision_id.")
        if not self.gate_id or not str(self.gate_id).strip():
            raise ValueError("QualityGateDecision requires a non-empty gate_id.")
        if not self.gate_version or not str(self.gate_version).strip():
            raise ValueError("QualityGateDecision requires a non-empty gate_version.")
        if not self.evaluation_run_id or not str(self.evaluation_run_id).strip():
            raise ValueError("QualityGateDecision requires a non-empty evaluation_run_id.")

        if isinstance(self.status, str):
            object.__setattr__(self, "status", GateDecisionStatus(self.status))

        if isinstance(self.failed_case_ids, (list, set)):
            object.__setattr__(self, "failed_case_ids", tuple(sorted(list(self.failed_case_ids))))
        if isinstance(self.unknown_case_ids, (list, set)):
            object.__setattr__(self, "unknown_case_ids", tuple(sorted(list(self.unknown_case_ids))))
        if isinstance(self.error_case_ids, (list, set)):
            object.__setattr__(self, "error_case_ids", tuple(sorted(list(self.error_case_ids))))
        if isinstance(self.missing_required_case_ids, (list, set)):
            object.__setattr__(self, "missing_required_case_ids", tuple(sorted(list(self.missing_required_case_ids))))
        if isinstance(self.critical_case_failures, (list, set)):
            object.__setattr__(self, "critical_case_failures", tuple(sorted(list(self.critical_case_failures))))
        if isinstance(self.reasons, (list, set)):
            object.__setattr__(self, "reasons", tuple(self.reasons))

        if self.pass_rate is not None:
            object.__setattr__(self, "pass_rate", Decimal(str(self.pass_rate)))

        # Inmutabilidad y sanitización profunda de mappings
        sanitized_evidence = _sanitize_gate_data(dict(self.evidence))
        object.__setattr__(self, "evidence", _deep_freeze(sanitized_evidence))

        sanitized_meta = _sanitize_gate_data(dict(self.metadata))
        object.__setattr__(self, "metadata", _deep_freeze(sanitized_meta))

        # Idempotency key canónico por (gate_id, gate_version, evaluation_run_id) si no se especifica
        if not self.idempotency_key:
            raw_key = f"{self.gate_id}:{self.gate_version}:{self.evaluation_run_id}"
            object.__setattr__(self, "idempotency_key", hashlib.sha256(raw_key.encode("utf-8")).hexdigest())

        # Checksum canónico
        calculated_checksum = compute_gate_decision_checksum(
            decision_id=self.decision_id,
            gate_id=self.gate_id,
            gate_version=self.gate_version,
            evaluation_run_id=self.evaluation_run_id,
            status=self.status.value,
            passed_count=self.passed_count,
            failed_count=self.failed_count,
            unknown_count=self.unknown_count,
            error_count=self.error_count,
            total_cases=self.total_cases,
            evaluated_count=self.evaluated_count,
            pass_rate=self.pass_rate,
            failed_case_ids=self.failed_case_ids,
            critical_case_failures=self.critical_case_failures,
            missing_required_case_ids=self.missing_required_case_ids,
            unknown_case_ids=self.unknown_case_ids,
            error_case_ids=self.error_case_ids,
            reasons=self.reasons,
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            dataset_manifest_checksum=self.dataset_manifest_checksum,
            evidence=self.evidence,
            trace_reference=self.trace_reference,
            audit_reference=self.audit_reference,
            cost_reference=self.cost_reference,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            provenance=self.provenance,
            idempotency_key=self.idempotency_key,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )
        if self.checksum:
            if self.checksum != calculated_checksum:
                raise ValueError("QualityGateDecision checksum does not match its content.")
        else:
            object.__setattr__(self, "checksum", calculated_checksum)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la decisión del gate a un diccionario determinista."""
        return {
            "decision_id": self.decision_id,
            "gate_id": self.gate_id,
            "gate_version": self.gate_version,
            "status": self.status.value,
            "evaluation_run_id": self.evaluation_run_id,
            "decided_at": self.decided_at.isoformat(),
            "total_cases": self.total_cases,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "unknown_count": self.unknown_count,
            "error_count": self.error_count,
            "evaluated_count": self.evaluated_count,
            "pass_rate": str(self.pass_rate) if self.pass_rate is not None else None,
            "failed_case_ids": list(self.failed_case_ids),
            "unknown_case_ids": list(self.unknown_case_ids),
            "error_case_ids": list(self.error_case_ids),
            "missing_required_case_ids": list(self.missing_required_case_ids),
            "critical_case_failures": list(self.critical_case_failures),
            "reasons": list(self.reasons),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_manifest_checksum": self.dataset_manifest_checksum,
            "evidence": dict(self.evidence),
            "trace_reference": self.trace_reference,
            "audit_reference": self.audit_reference,
            "cost_reference": self.cost_reference,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "provenance": self.provenance,
            "idempotency_key": self.idempotency_key,
            "checksum": self.checksum,
            "schema_version": self.schema_version,
            "metadata": dict(self.metadata),
        }
