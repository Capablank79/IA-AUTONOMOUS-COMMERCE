"""
Modelos de dominio para el Registro de Trazas Operacionales de Agentes (Agent Trace - Hito K.2).

Define:
- StepType: Taxonomía canónica de pasos operacionales observables de un agente o servicio autónomo.
- TraceStatus: Estados canónicos de ejecución (STARTED, SUCCESS, FAILED, UNKNOWN, SKIPPED).
- AgentTraceRecord: Entidad de dominio inmutable para un paso operacional observable.
- ExecutionTraceTimeline: Agregado inmutable que agrupa y reconstruye cronológica y determinísticamente los pasos de una ejecución.

Principios K.2:
- Inmutabilidad estricta (frozen=True, MappingProxyType).
- Agent Trace responde: CÓMO se ejecutó operacionalmente un agente a través de pasos observables (START -> OBSERVE -> SERVICE_CALL -> TOOL_CALL -> COMPLETE).
- Prohibición de Chain-of-Thought (CoT): CERO tokens de razonamiento privado, reflexiones internas o scratchpads.
- Prohibición de persistir prompts privados completos salvo referencias necesarias.
- Sanitización recursiva de secretos (API keys, passwords, PAN, CVV, tokens).
- Preservación estricta de incertidumbre UNKNOWN.
- Idempotencia estricta por (execution_id, step_number, operation).
- Derivación estricta de duración: completed_at - started_at.
- NO duplica Audit Trail K.1 ni implementa Cost Tracking K.3.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Union, List
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
}


def _sanitize_trace_metadata(val: Any) -> Any:
    """Sanitiza recursivamente cualquier estructura de metadatos para eliminar secretos y CoT."""
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _sanitize_trace_metadata(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_sanitize_trace_metadata(v) for v in val]
    return val


class StepType(str, Enum):
    """
    Taxonomía canónica y mínima de tipos de pasos operacionales observables (K.2).
    """
    START = "START"
    OBSERVE = "OBSERVE"
    SERVICE_CALL = "SERVICE_CALL"
    POLICY_EVALUATION = "POLICY_EVALUATION"
    TOOL_CALL = "TOOL_CALL"
    PERSIST = "PERSIST"
    EMIT_EVENT = "EMIT_EVENT"
    COMPLETE = "COMPLETE"
    FAILURE = "FAILURE"


class TraceStatus(str, Enum):
    """
    Estados canónicos de un paso operacional observable.
    UNKNOWN debe preservarse sin convertirlo en SUCCESS/FAILED.
    """
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class AgentTraceRecord:
    """
    Entidad de dominio inmutable para un Paso Operacional Observable (Agent Trace - K.2).
    Representa un paso discreto, auditable y causal de la ejecución de un agente/servicio autónomo.

    Límites:
    - NO almacena chain-of-thought interno, reasoning tokens ni prompts privados.
    - Utiliza referencias de entrada y salida (input_reference, output_reference) en vez de payloads masivos.
    - Inmutable y determinista.
    """
    trace_id: str
    component_name: str
    execution_id: str
    step_number: int
    step_type: StepType
    operation: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: TraceStatus = TraceStatus.STARTED
    tool_or_service: Optional[str] = None
    input_reference: Optional[str] = None
    output_reference: Optional[str] = None
    correlation_id: str = ""
    causation_id: Optional[str] = None
    mission_id: Optional[str] = None
    cycle_id: Optional[str] = None
    provenance: str = "AGENT"
    idempotency_key: str = ""
    checksum: Optional[str] = None
    schema_version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.trace_id or not isinstance(self.trace_id, str):
            raise ValueError("trace_id must be a non-empty string")
        if not self.component_name or not isinstance(self.component_name, str):
            raise ValueError("component_name must be a non-empty string")
        if not self.execution_id or not isinstance(self.execution_id, str):
            raise ValueError("execution_id must be a non-empty string")
        if self.step_number < 0 or not isinstance(self.step_number, int):
            raise ValueError("step_number must be a non-negative integer")
        if not isinstance(self.step_type, StepType):
            try:
                object.__setattr__(self, "step_type", StepType(self.step_type))
            except Exception as e:
                raise ValueError(f"Invalid step_type: {self.step_type}") from e
        if not self.operation or not isinstance(self.operation, str):
            raise ValueError("operation must be a non-empty string")
        if not isinstance(self.status, TraceStatus):
            try:
                object.__setattr__(self, "status", TraceStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid status: {self.status}") from e

        # Asegurar timezones UTC
        if self.started_at.tzinfo is None:
            object.__setattr__(self, "started_at", self.started_at.replace(tzinfo=timezone.utc))
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            object.__setattr__(self, "completed_at", self.completed_at.replace(tzinfo=timezone.utc))

        # Sanitizar metadatos y convertir a MappingProxyType inmutable
        sanitized = _sanitize_trace_metadata(dict(self.metadata))
        object.__setattr__(self, "metadata", MappingProxyType(sanitized))

        # Idempotency key determinista si no viene provista
        if not self.idempotency_key:
            idem_content = f"{self.execution_id}:{self.step_number}:{self.operation}:{self.component_name}"
            auto_key = hashlib.sha256(idem_content.encode("utf-8")).hexdigest()
            object.__setattr__(self, "idempotency_key", auto_key)

        # Checksum criptográfico para detección de manipulación
        if not self.checksum:
            computed_checksum = self._compute_checksum()
            object.__setattr__(self, "checksum", computed_checksum)

    @property
    def duration_seconds(self) -> Optional[float]:
        """
        Duración derivada de la ejecución del paso (completed_at - started_at).
        Derivada estrictamente, sin almacenar estado mutable.
        """
        if self.completed_at is None:
            return None
        return max(0.0, (self.completed_at - self.started_at).total_seconds())

    def _compute_checksum(self) -> str:
        """Calcula el hash SHA-256 canónico del paso para verificación de integridad."""
        started_iso = self.started_at.isoformat()
        completed_iso = self.completed_at.isoformat() if self.completed_at else ""
        canonical_str = (
            f"{self.trace_id}|{self.component_name}|{self.execution_id}|{self.step_number}|"
            f"{self.step_type.value}|{self.operation}|{self.status.value}|{started_iso}|"
            f"{completed_iso}|{self.tool_or_service or ''}|{self.input_reference or ''}|"
            f"{self.output_reference or ''}|{self.correlation_id}|{self.causation_id or ''}|"
            f"{self.mission_id or ''}|{self.cycle_id or ''}|{self.idempotency_key}"
        )
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

    def verify_checksum(self) -> bool:
        """Verifica si el checksum coincide exactamente con los atributos del registro."""
        return self.checksum == self._compute_checksum()


@dataclass(frozen=True)
class ExecutionTraceTimeline:
    """
    Agregado inmutable que agrupa y reconstruye cronológica y determinísticamente
    la secuencia completa de pasos de una ejecución de agente/servicio.
    """
    execution_id: str
    component_name: str
    mission_id: Optional[str]
    cycle_id: Optional[str]
    correlation_id: str
    status: TraceStatus
    started_at: datetime
    completed_at: Optional[datetime]
    steps: Tuple[AgentTraceRecord, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.steps, tuple):
            object.__setattr__(self, "steps", tuple(self.steps))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.completed_at is None:
            return None
        return max(0.0, (self.completed_at - self.started_at).total_seconds())

    @classmethod
    def build_from_records(cls, execution_id: str, records: List[AgentTraceRecord]) -> "ExecutionTraceTimeline":
        """
        Construye una timeline ordenada determinísticamente a partir de una lista de AgentTraceRecord.
        Ordenación: step_number ASC, started_at ASC, trace_id ASC.
        """
        filtered = [r for r in records if r.execution_id == execution_id]
        if not filtered:
            now = datetime.now(timezone.utc)
            return cls(
                execution_id=execution_id,
                component_name="UNKNOWN",
                mission_id=None,
                cycle_id=None,
                correlation_id="",
                status=TraceStatus.UNKNOWN,
                started_at=now,
                completed_at=None,
                steps=(),
            )

        # Orden determinista
        sorted_records = sorted(
            filtered,
            key=lambda r: (r.step_number, r.started_at, r.trace_id)
        )

        first = sorted_records[0]
        last = sorted_records[-1]

        # Determinar estado global de la ejecución
        has_failed = any(r.status == TraceStatus.FAILED for r in sorted_records)
        has_unknown = any(r.status == TraceStatus.UNKNOWN for r in sorted_records)
        is_completed = any(r.step_type == StepType.COMPLETE and r.status == TraceStatus.SUCCESS for r in sorted_records)

        if has_failed:
            overall_status = TraceStatus.FAILED
        elif has_unknown:
            overall_status = TraceStatus.UNKNOWN
        elif is_completed:
            overall_status = TraceStatus.SUCCESS
        elif last.completed_at is not None:
            overall_status = last.status
        else:
            overall_status = TraceStatus.STARTED

        started_at = first.started_at
        completed_at = last.completed_at if overall_status != TraceStatus.STARTED else None

        return cls(
            execution_id=execution_id,
            component_name=first.component_name,
            mission_id=first.mission_id,
            cycle_id=first.cycle_id,
            correlation_id=first.correlation_id,
            status=overall_status,
            started_at=started_at,
            completed_at=completed_at,
            steps=tuple(sorted_records),
            metadata={"step_count": len(sorted_records)},
        )
