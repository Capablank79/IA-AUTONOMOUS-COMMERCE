"""
Modelos de dominio para el Arnés de Evaluación (Evaluation Harness - Hito K.4).

Define:
- EvaluationType: Taxonomía canónica de tipos de evaluación (EXACT_MATCH, STRUCTURAL, NUMERIC, STATUS, POLICY, SAFETY, TRACE, IDEMPOTENCY, TEMPORAL, END_TO_END).
- EvaluationStatus: Estados de resultado de evaluación (PASS, FAIL, UNKNOWN, ERROR).
- EvaluationCase: Definición inmutable y declarativa de un caso de evaluación con criterios esperados explícitos.
- EvaluationMetric: Métrica individual estructurada de evaluación.
- EvaluationResult: Resultado inmutable de la ejecución y evaluación de un caso.
- BatchEvaluationSummary: Resumen agregado determinista de un lote de evaluaciones.

Principios K.4:
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- K.4 responde: WHAT WAS EVALUATED, AGAINST WHICH CASE, WITH WHICH EXPECTED CRITERIA, WHAT ACTUALLY HAPPENED, PASS/FAIL/UNKNOWN/ERROR, WITH WHICH EVIDENCE.
- Evaluaciones deterministas y reproducibles.
- Preservación explícita de semánticas UNKNOWN y ERROR (no convertir ERROR en FAIL silenciosamente).
- Enlace no intrusivo con K.1 Audit Trail, K.2 Agent Trace y K.3 Cost Tracking.
- Sanitización recursiva de secretos y exclusión estricta de PII / API keys / tokens / passwords / PAN / CVV.
- Idempotencia estricta por (case_id, target_execution_reference, evaluator_version).
- NO crea Golden Datasets (K.5).
- NO implementa Quality Gates de release blocking (K.6).
- NO usa LLM-as-a-judge.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Union, Sequence, List
import hashlib
import json


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


def _sanitize_eval_data(val: Any) -> Any:
    """Sanitiza recursivamente estructuras de datos para eliminar secretos o credenciales."""
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _sanitize_eval_data(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_sanitize_eval_data(v) for v in val]
    return val


class EvaluationType(str, Enum):
    """
    Taxonomía canónica de tipos de evaluación determinista (K.4).
    """
    EXACT_MATCH = "EXACT_MATCH"
    STRUCTURAL = "STRUCTURAL"
    NUMERIC = "NUMERIC"
    STATUS = "STATUS"
    POLICY = "POLICY"
    SAFETY = "SAFETY"
    TRACE = "TRACE"
    IDEMPOTENCY = "IDEMPOTENCY"
    TEMPORAL = "TEMPORAL"
    END_TO_END = "END_TO_END"


class EvaluationStatus(str, Enum):
    """
    Estados canónicos de resultado de una evaluación.
    - PASS: El resultado cumplió todos los criterios esperados.
    - FAIL: El resultado violó uno o más criterios esperados de manera verificable.
    - UNKNOWN: El sistema o evaluación produjo incertidumbre o los datos/criterios fueron insuficientes para decidir.
    - ERROR: Ocurrió una excepción/fallo durante la ejecución del target o del evaluador.
    """
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


@dataclass(frozen=True)
class EvaluationMetric:
    """
    Representación estructurada e inmutable de una métrica de evaluación individual.
    """
    metric_name: str
    metric_value: Any
    unit: str = "COUNT"
    expected_value: Optional[Any] = None
    min_value: Optional[Any] = None
    max_value: Optional[Any] = None
    status: EvaluationStatus = EvaluationStatus.UNKNOWN
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        sanitized_ev = _sanitize_eval_data(self.evidence)
        object.__setattr__(self, "evidence", MappingProxyType(dict(sanitized_ev)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "metric_value": str(self.metric_value) if isinstance(self.metric_value, Decimal) else self.metric_value,
            "unit": self.unit,
            "expected_value": str(self.expected_value) if isinstance(self.expected_value, Decimal) else self.expected_value,
            "min_value": str(self.min_value) if isinstance(self.min_value, Decimal) else self.min_value,
            "max_value": str(self.max_value) if isinstance(self.max_value, Decimal) else self.max_value,
            "status": self.status.value,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class EvaluationCase:
    """
    Definición inmutable y declarativa de un caso de evaluación (K.4).
    Representa una prueba explícita de comportamiento o salida esperada del sistema.
    """
    case_id: str
    name: str
    description: str
    evaluation_type: EvaluationType
    input_reference: Mapping[str, Any] = field(default_factory=dict)
    expected_criteria: Mapping[str, Any] = field(default_factory=dict)
    tags: Tuple[str, ...] = field(default_factory=tuple)
    version: str = "1.0.0"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provenance: str = "ENGINEERING_SPEC"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Asegurar UTC timezone
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))

        # Sanitizar estructuras
        sanitized_in = _sanitize_eval_data(self.input_reference)
        object.__setattr__(self, "input_reference", MappingProxyType(dict(sanitized_in)))

        sanitized_exp = _sanitize_eval_data(self.expected_criteria)
        object.__setattr__(self, "expected_criteria", MappingProxyType(dict(sanitized_exp)))

        if isinstance(self.tags, (list, set)):
            object.__setattr__(self, "tags", tuple(sorted(self.tags)))

        sanitized_meta = _sanitize_eval_data(self.metadata)
        object.__setattr__(self, "metadata", MappingProxyType(dict(sanitized_meta)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "name": self.name,
            "description": self.description,
            "evaluation_type": self.evaluation_type.value,
            "input_reference": dict(self.input_reference),
            "expected_criteria": dict(self.expected_criteria),
            "tags": list(self.tags),
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "provenance": self.provenance,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class EvaluationResult:
    """
    Resultado inmutable de la ejecución y evaluación de un caso (K.4).
    """
    result_id: str
    case_id: str
    execution_id: str
    evaluated_component: str
    started_at: datetime
    completed_at: datetime
    status: EvaluationStatus
    metrics: Tuple[EvaluationMetric, ...] = field(default_factory=tuple)
    expected_reference: Mapping[str, Any] = field(default_factory=dict)
    actual_reference: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    trace_reference: Optional[str] = None
    audit_reference: Optional[str] = None
    cost_reference: Optional[str] = None
    correlation_id: str = ""
    causation_id: Optional[str] = None
    provenance: str = "EVALUATION_HARNESS"
    evaluator_version: str = "1.0.0"
    idempotency_key: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Fechas en UTC
        if self.started_at.tzinfo is None:
            object.__setattr__(self, "started_at", self.started_at.replace(tzinfo=timezone.utc))
        if self.completed_at.tzinfo is None:
            object.__setattr__(self, "completed_at", self.completed_at.replace(tzinfo=timezone.utc))

        if isinstance(self.metrics, list):
            object.__setattr__(self, "metrics", tuple(self.metrics))

        sanitized_exp = _sanitize_eval_data(self.expected_reference)
        object.__setattr__(self, "expected_reference", MappingProxyType(dict(sanitized_exp)))

        sanitized_act = _sanitize_eval_data(self.actual_reference)
        object.__setattr__(self, "actual_reference", MappingProxyType(dict(sanitized_act)))

        sanitized_ev = _sanitize_eval_data(self.evidence)
        object.__setattr__(self, "evidence", MappingProxyType(dict(sanitized_ev)))

        sanitized_meta = _sanitize_eval_data(self.metadata)
        object.__setattr__(self, "metadata", MappingProxyType(dict(sanitized_meta)))

        # Derivar idempotency_key si no se suministra explícitamente
        if not self.idempotency_key:
            target_ref_str = json.dumps(dict(self.actual_reference), sort_keys=True, default=str)
            raw_key = f"{self.case_id}:{self.execution_id}:{self.evaluator_version}:{target_ref_str}"
            derived_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            object.__setattr__(self, "idempotency_key", derived_key)

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.completed_at - self.started_at).total_seconds() * 1000.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result_id": self.result_id,
            "case_id": self.case_id,
            "execution_id": self.execution_id,
            "evaluated_component": self.evaluated_component,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "metrics": [m.to_dict() for m in self.metrics],
            "expected_reference": dict(self.expected_reference),
            "actual_reference": dict(self.actual_reference),
            "evidence": dict(self.evidence),
            "trace_reference": self.trace_reference,
            "audit_reference": self.audit_reference,
            "cost_reference": self.cost_reference,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "provenance": self.provenance,
            "evaluator_version": self.evaluator_version,
            "idempotency_key": self.idempotency_key,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class BatchEvaluationSummary:
    """
    Resumen inmutable y determinista de la ejecución de un lote (batch) de evaluaciones.
    """
    total_cases: int
    passed_count: int
    failed_count: int
    unknown_count: int
    error_count: int
    results: Tuple[EvaluationResult, ...] = field(default_factory=tuple)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def __post_init__(self):
        if isinstance(self.results, list):
            object.__setattr__(self, "results", tuple(self.results))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "unknown_count": self.unknown_count,
            "error_count": self.error_count,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "results": [r.to_dict() for r in self.results],
        }
