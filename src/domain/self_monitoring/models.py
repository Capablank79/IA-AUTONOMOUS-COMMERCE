"""
Modelos de dominio para Self-monitoring (Hito R.7 — Advanced Autonomy).

Define:
- MissionHealthStatus: Estados de salud canónicos (HEALTHY, DEGRADED, AT_RISK, BLOCKED, UNKNOWN).
- SignalType: Taxonomía canónica de señales de salud operacionales.
- SignalSource: Fuentes canónicas de señales de ejecución.
- SignalSeverity: Severidad de la señal observada (INFO, WARNING, HIGH, CRITICAL, UNKNOWN).
- HealthSignal: Señal individual normalizada y tipada con evidencia y trazabilidad.
- DegradationReasonCode: Códigos explícitos de motivo de degradación o riesgo.
- SelfMonitoringAction: Respuestas internas permitidas (NONE, CONTINUE_WITH_WARNING, PAUSE, BLOCK, REQUEST_REPLAN, REQUEST_DELEGATION, ESCALATE).
- SelfMonitoringDecision: Decisión de control interno acotada producida por la evaluación de salud.
- MissionHealthSnapshot: Snapshot inmutable de salud de misión y agregación de señales.
- HealthAssessment: Agregado inmutable que encapsula la evaluación completa, historial y decisión de salud.

Principios R.7:
1. Responde a: "¿Puede el sistema autónomo observar su propio estado operacional durante una misión, detectar degradaciones relevantes y reaccionar de forma segura dentro de límites explícitos?".
2. REUSE > EXTEND > CREATE: Reutiliza señales reales de R.1 Planning, R.2 Sub-missions, R.3 Specialists, R.4 Coordination, R.5 Dynamic Delegation, R.6 Long-running Missions, K.3 Cost Tracking, P.7 Monitoring, P.8 Alerting, P.11 Rate Limits, O.7 Quotas, M.* Cost Controls.
3. UNKNOWN SEMANTICS: UNKNOWN != HEALTHY y UNKNOWN != 0. La falta de señales o datos insuficientes computa como UNKNOWN o evalúa fail-safe.
4. DETERMINISTIC ANOMALY DETECTION: Detección explícita basada en ClockPort sin sleeps (stale heartbeat, stalled mission, failure rate con denominador > 0, cost spikes en Decimal sin mezclar currencies, conflictos de coordinación, presión de cuotas).
5. BOUNDED INTERNAL SAFE RESPONSE: Respuestas internas acotadas sin loops infinitos de replan/delegación. No puede alterar políticas, aumentar presupuestos, saltarse Emergency Stop ni realizar mutaciones externas automáticas.
6. ZERO-COT & SENSITIVE DATA DEFENSE: Snapshots sanitizados sin secretos, tokens, reasoning tokens ni private prompts (N.9/K.2).
7. TENANT ISOLATION: Aislamiento multi-tenant estricto mediante CrossTenantGuard.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List, Set

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class MissionHealthStatus(str, Enum):
    """
    Estados canónicos de salud de una misión autónoma.
    Regla: UNKNOWN != HEALTHY.
    """
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AT_RISK = "AT_RISK"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class SignalType(str, Enum):
    """
    Taxonomía canónica de señales de salud operacional.
    """
    HEARTBEAT_LIVENESS = "HEARTBEAT_LIVENESS"
    LEASE_VALIDITY = "LEASE_VALIDITY"
    EXECUTION_PROGRESS = "EXECUTION_PROGRESS"
    STALL_TEMPORAL = "STALL_TEMPORAL"
    STEP_FAILURE = "STEP_FAILURE"
    TECHNICAL_FAILURE_RATE = "TECHNICAL_FAILURE_RATE"
    COORDINATION_CONFLICT = "COORDINATION_CONFLICT"
    DELEGATION_PRESSURE = "DELEGATION_PRESSURE"
    REPLAN_PRESSURE = "REPLAN_PRESSURE"
    CHILD_MISSION_HEALTH = "CHILD_MISSION_HEALTH"
    AGENT_AVAILABILITY = "AGENT_AVAILABILITY"
    COST_ANOMALY = "COST_ANOMALY"
    BUDGET_PRESSURE = "BUDGET_PRESSURE"
    QUOTA_SATURATION = "QUOTA_SATURATION"
    RATE_LIMIT_PRESSURE = "RATE_LIMIT_PRESSURE"
    LATENCY_ANOMALY = "LATENCY_ANOMALY"
    EMERGENCY_STOP_STATUS = "EMERGENCY_STOP_STATUS"
    POLICY_COMPLIANCE = "POLICY_COMPLIANCE"


class SignalSource(str, Enum):
    """
    Fuentes canónicas y verídicas de señales del sistema.
    """
    R1_PLANNING = "R1_PLANNING"
    R2_SUB_MISSION = "R2_SUB_MISSION"
    R3_SPECIALIST_REGISTRY = "R3_SPECIALIST_REGISTRY"
    R4_COORDINATION = "R4_COORDINATION"
    R5_DELEGATION = "R5_DELEGATION"
    R6_LONG_RUNNING = "R6_LONG_RUNNING"
    K3_COST_TRACKING = "K3_COST_TRACKING"
    P7_MONITORING = "P7_MONITORING"
    P8_ALERTING = "P8_ALERTING"
    P11_RATE_LIMIT = "P11_RATE_LIMIT"
    O7_QUOTA = "O7_QUOTA"
    M_BUDGET_CONTROL = "M_BUDGET_CONTROL"
    N_GOVERNANCE = "N_GOVERNANCE"
    AUTONOMOUS_LOOP = "AUTONOMOUS_LOOP"
    EXTERNAL = "EXTERNAL"


class SignalSeverity(str, Enum):
    """
    Severidad asignada a la señal observada.
    """
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class SignalCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class DegradationReasonCode(str, Enum):
    """
    Códigos explícitos y estructurados de motivo de degradación o riesgo.
    """
    STALE_HEARTBEAT = "STALE_HEARTBEAT"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    MISSION_STALLED = "MISSION_STALLED"
    REPEATED_TECHNICAL_FAILURES = "REPEATED_TECHNICAL_FAILURES"
    COORDINATION_CONFLICT_DETECTED = "COORDINATION_CONFLICT_DETECTED"
    REPEATED_DELEGATION_NEAR_BOUND = "REPEATED_DELEGATION_NEAR_BOUND"
    REPEATED_REPLAN_NEAR_BOUND = "REPEATED_REPLAN_NEAR_BOUND"
    REQUIRED_CHILD_MISSION_FAILED = "REQUIRED_CHILD_MISSION_FAILED"
    SPECIALIST_AGENT_UNAVAILABLE = "SPECIALIST_AGENT_UNAVAILABLE"
    BUDGET_HARD_LIMIT_REACHED = "BUDGET_HARD_LIMIT_REACHED"
    BUDGET_PRESSURE_HIGH = "BUDGET_PRESSURE_HIGH"
    COST_SPIKE_DETECTED = "COST_SPIKE_DETECTED"
    COST_DATA_INVALID_OR_UNKNOWN = "COST_DATA_INVALID_OR_UNKNOWN"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMIT_SATURATED = "RATE_LIMIT_SATURATED"
    LATENCY_SPIKE = "LATENCY_SPIKE"
    EMERGENCY_STOP_TRIGGERED = "EMERGENCY_STOP_TRIGGERED"
    POLICY_DENIED_VIOLATION = "POLICY_DENIED_VIOLATION"
    INSUFFICIENT_SIGNALS_UNKNOWN = "INSUFFICIENT_SIGNALS_UNKNOWN"
    INCOMPLETE_SIGNAL_DATA = "INCOMPLETE_SIGNAL_DATA"
    MISSING_REQUIRED_SIGNALS = "MISSING_REQUIRED_SIGNALS"
    MIXED_CURRENCY_DATA = "MIXED_CURRENCY_DATA"
    NONE_HEALTHY = "NONE_HEALTHY"


class SelfMonitoringAction(str, Enum):
    """
    Acciones de control interno permitidas según la condición evaluada.
    """
    NONE = "NONE"
    CONTINUE_WITH_WARNING = "CONTINUE_WITH_WARNING"
    PAUSE = "PAUSE"
    BLOCK = "BLOCK"
    REQUEST_REPLAN = "REQUEST_REPLAN"
    REQUEST_DELEGATION = "REQUEST_DELEGATION"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class HealthSignal:
    """
    Señal individual normalizada y estructurada para evaluación de salud.
    """
    signal_id: str
    signal_type: SignalType
    source: SignalSource
    severity: SignalSeverity
    observed_at: datetime
    confidence: float
    value: Any = None
    state_description: str = ""
    evidence_reference: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tenant_id: Optional[str] = None
    mission_id: Optional[str] = None
    completeness: SignalCompleteness = SignalCompleteness.UNKNOWN
    missing_fields: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.signal_id, "signal_id")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware (UTC)")
        if self.tenant_id is not None:
            validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.mission_id is not None:
            validate_safe_identifier(self.mission_id, "mission_id")
        if not isinstance(self.completeness, SignalCompleteness):
            object.__setattr__(self, "completeness", SignalCompleteness(self.completeness))
        missing_fields = tuple(str(name).strip() for name in self.missing_fields if str(name).strip())
        if self.completeness == SignalCompleteness.COMPLETE and missing_fields:
            raise ValueError("COMPLETE signals cannot declare missing_fields")
        object.__setattr__(self, "missing_fields", missing_fields)
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))


@dataclass(frozen=True)
class SelfMonitoringDecision:
    """
    Decisión inmutable y acotada de control interno generada por el evaluador de salud.
    """
    action: SelfMonitoringAction
    health_status: MissionHealthStatus
    reason_codes: Tuple[DegradationReasonCode, ...]
    rationale: str
    evaluated_at: datetime
    suggested_target: Optional[str] = None
    suggested_payload: Mapping[str, Any] = field(default_factory=dict)
    requires_escalation: bool = False
    escalation_severity: Optional[str] = None
    is_bounded: bool = True

    def __post_init__(self):
        if not isinstance(self.reason_codes, tuple):
            object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC)")
        object.__setattr__(self, "suggested_payload", deep_freeze(sanitize_security_data(self.suggested_payload)))


@dataclass(frozen=True)
class MissionHealthSnapshot:
    """
    Snapshot inmutable del estado de salud de una misión en un punto en el tiempo.
    """
    snapshot_id: str
    mission_id: str
    tenant_id: str
    health_status: MissionHealthStatus
    signals: Tuple[HealthSignal, ...]
    reason_codes: Tuple[DegradationReasonCode, ...]
    captured_at: datetime
    is_stalled: bool = False
    is_stale_heartbeat: bool = False
    is_budget_exceeded: bool = False
    is_emergency_stopped: bool = False
    is_policy_denied: bool = False
    active_worker_id: Optional[str] = None
    progress_percentage: Optional[float] = None
    checksum: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.snapshot_id, "snapshot_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if not isinstance(self.signals, tuple):
            object.__setattr__(self, "signals", tuple(self.signals))
        if not isinstance(self.reason_codes, tuple):
            object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware (UTC)")
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))

        if not self.checksum:
            payload = {
                "snapshot_id": self.snapshot_id,
                "mission_id": self.mission_id,
                "tenant_id": self.tenant_id,
                "health_status": self.health_status.value,
                "reason_codes": [r.value for r in self.reason_codes],
                "captured_at": self.captured_at.isoformat(),
                "is_stalled": self.is_stalled,
                "is_stale_heartbeat": self.is_stale_heartbeat,
                "is_budget_exceeded": self.is_budget_exceeded,
                "is_emergency_stopped": self.is_emergency_stopped,
                "is_policy_denied": self.is_policy_denied,
            }
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
            object.__setattr__(self, "checksum", digest)


@dataclass(frozen=True)
class HealthAssessment:
    """
    Agregado inmutable que encapsula la evaluación completa de salud operacional.
    """
    assessment_id: str
    mission_id: str
    tenant_id: str
    status: MissionHealthStatus
    snapshot: MissionHealthSnapshot
    decision: SelfMonitoringDecision
    evaluated_at: datetime
    previous_status: Optional[MissionHealthStatus] = None
    remediation_counter: int = 0
    remediation_limit_reached: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.assessment_id, "assessment_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware (UTC)")
        if self.remediation_counter < 0:
            raise ValueError("remediation_counter cannot be negative")
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))


@dataclass(frozen=True)
class SelfMonitoringPolicy:
    """
    Parámetros de configuración deterministas para las reglas de auto-monitoreo.
    """
    stale_heartbeat_threshold_seconds: float = 30.0
    stall_timeout_seconds: float = 120.0
    max_remediation_actions_per_mission: int = 5
    max_replan_requests: int = 3
    max_delegation_requests: int = 3
    failure_rate_threshold: float = 0.5
    min_failure_samples: int = 3
    budget_warning_threshold_ratio: float = 0.85
    cost_spike_multiplier_threshold: float = 2.0
    debounce_consecutive_observations: int = 1
    required_signal_types: Tuple[SignalType, ...] = field(default_factory=tuple)

    def __post_init__(self):
        required = tuple(
            item if isinstance(item, SignalType) else SignalType(item)
            for item in self.required_signal_types
        )
        object.__setattr__(self, "required_signal_types", required)
        if self.stale_heartbeat_threshold_seconds <= 0:
            raise ValueError("stale_heartbeat_threshold_seconds must be positive")
        if self.stall_timeout_seconds <= 0:
            raise ValueError("stall_timeout_seconds must be positive")
        if self.max_remediation_actions_per_mission < 0:
            raise ValueError("max_remediation_actions_per_mission cannot be negative")
        if not (0.0 < self.failure_rate_threshold <= 1.0):
            raise ValueError("failure_rate_threshold must be between 0.0 and 1.0")
        if self.min_failure_samples < 1:
            raise ValueError("min_failure_samples must be at least 1")
