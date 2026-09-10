"""
Modelos de dominio para Quota Management SaaS (Hito O.7 — Quota Management).

Define:
- QuotaStatus: Estados canónicos de decisión (ALLOW, LIMIT_REACHED, RATE_LIMITED, APPROVAL_REQUIRED, UNKNOWN, ERROR).
- QuotaType: Tipos canónicos de límites y presupuestos (MAX_REQUESTS, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, MAX_TOTAL_TOKENS, MAX_COST, REQUESTS_PER_MINUTE, REQUESTS_PER_HOUR, TOKENS_PER_PERIOD).
- QuotaScope: Ámbitos jerárquicos de aplicación (TENANT, USER, MODEL, PROVIDER).
- QuotaWindowType: Granularidad temporal de ventana determinista (MINUTE, HOUR, DAY, MONTH, CUSTOM, UNLIMITED).
- QuotaWindow: Intervalo de ventana temporal UTC determinista [start_time, end_time).
- QuotaRule: Regla inmutable de cuota con límite numérico o Decimal.
- QuotaPolicy: Política inmutable tenant-scoped con colección de reglas deterministas.
- QuotaRequest: Solicitud de pre-flight check y estimación de consumo de IA.
- QuotaDecision: Decisión determinista y auditable de gobernanza de cuotas.
- QuotaReservationStatus: Estados de reserva atómica (RESERVED, CONSUMED, RELEASED, EXPIRED).
- QuotaReservation: Reserva de capacidad en vuelo para prevención estricta de TOCTOU / sobreconsumo concurrente.
- Excepciones de dominio para Quota Management.

Principios O.7:
1. Responde a: "¿Puede este tenant/usuario seguir consumiendo IA en este momento según sus límites configurados?".
2. No Magic Unlimited: La ausencia de política o métricas no computables resulta en UNKNOWN / DENY según configuración fail-safe. Si una cuota es ilimitada, debe ser una política explícita.
3. Fuente de verdad histórica: O.6 Usage Metering es la única fuente de facts históricos. O.7 consulta agregados de O.6 y mantiene en memoria/reserva solo capacidad en vuelo.
4. Alta precisión financiera: Monedas y límites de coste con Decimal, nunca float.
5. Rate limit local vs upstream: RATE_LIMITED de O.7 refleja límites de política local SaaS, completamente separado de provider 429.
6. Aislamiento por tenant estricto: Las políticas, reservas y consumos de Tenant A nunca afectan o aplican a Tenant B.
7. Cero planes/billing (O.8/O.9): Políticas configurables directamente en O.7 sin intermediación de suscripciones o pasarelas de pago.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List
import uuid

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.reliability.ports import ClockPort


class QuotaManagementError(Exception):
    """Excepción base para errores del dominio de Quota Management."""
    pass


class QuotaPolicyNotFoundError(QuotaManagementError):
    """Se lanza cuando no existe política configurada para el tenant y no hay fallback explícito."""
    pass


class QuotaPolicyIntegrityError(QuotaManagementError):
    """Se lanza cuando se detecta corrupción o inconsistencia en una QuotaPolicy."""
    pass


class QuotaReservationConflictError(QuotaManagementError):
    """Se lanza ante conflicto o inconsistencia de reserva en vuelo."""
    pass


class QuotaReservationNotFoundError(QuotaManagementError):
    """Se lanza cuando una reserva a conciliar o liberar no existe."""
    pass


class QuotaStatus(str, Enum):
    """Estados canónicos de decisión de cuota."""
    ALLOW = "ALLOW"
    LIMIT_REACHED = "LIMIT_REACHED"
    RATE_LIMITED = "RATE_LIMITED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class QuotaType(str, Enum):
    """Tipos canónicos de límites y presupuestos gobernados por O.7."""
    MAX_REQUESTS = "MAX_REQUESTS"
    MAX_INPUT_TOKENS = "MAX_INPUT_TOKENS"
    MAX_OUTPUT_TOKENS = "MAX_OUTPUT_TOKENS"
    MAX_TOTAL_TOKENS = "MAX_TOTAL_TOKENS"
    MAX_COST = "MAX_COST"
    REQUESTS_PER_MINUTE = "REQUESTS_PER_MINUTE"
    REQUESTS_PER_HOUR = "REQUESTS_PER_HOUR"
    TOKENS_PER_PERIOD = "TOKENS_PER_PERIOD"


class QuotaScope(str, Enum):
    """Ámbitos de aplicación de cuotas."""
    TENANT = "TENANT"
    USER = "USER"
    MODEL = "MODEL"
    PROVIDER = "PROVIDER"


class QuotaWindowType(str, Enum):
    """Tipos de ventanas temporales para cuotas."""
    MINUTE = "MINUTE"
    HOUR = "HOUR"
    DAY = "DAY"
    MONTH = "MONTH"
    CUSTOM = "CUSTOM"
    UNLIMITED = "UNLIMITED"


class QuotaReservationStatus(str, Enum):
    """Estados del ciclo de vida de una reserva de cuota."""
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class QuotaWindow:
    """
    Rango temporal UTC determinista e inmutable para la evaluación de una regla de cuota.
    Boundary canónico: [start_time, end_time) semi-abierto.
    """
    window_type: QuotaWindowType
    start_time: datetime
    end_time: datetime

    def __post_init__(self):
        if self.start_time.tzinfo is None:
            object.__setattr__(self, "start_time", self.start_time.replace(tzinfo=timezone.utc))
        if self.end_time.tzinfo is None:
            object.__setattr__(self, "end_time", self.end_time.replace(tzinfo=timezone.utc))

        if self.start_time > self.end_time:
            raise ValueError(f"QuotaWindow start_time {self.start_time} cannot be after end_time {self.end_time}.")

    @classmethod
    def from_clock(
        cls,
        window_type: QuotaWindowType,
        now: datetime,
        custom_duration_seconds: Optional[int] = None,
    ) -> "QuotaWindow":
        """Calcula una ventana determinista UTC alineada al reloj provisto."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        if window_type == QuotaWindowType.UNLIMITED:
            start = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            end = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
            return cls(window_type=window_type, start_time=start, end_time=end)

        if window_type == QuotaWindowType.MINUTE:
            start = now.replace(second=0, microsecond=0)
            end = start + timedelta(minutes=1)
            return cls(window_type=window_type, start_time=start, end_time=end)

        if window_type == QuotaWindowType.HOUR:
            start = now.replace(minute=0, second=0, microsecond=0)
            end = start + timedelta(hours=1)
            return cls(window_type=window_type, start_time=start, end_time=end)

        if window_type == QuotaWindowType.DAY:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            return cls(window_type=window_type, start_time=start, end_time=end)

        if window_type == QuotaWindowType.MONTH:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # Siguiente mes
            if start.month == 12:
                end = datetime(start.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            else:
                end = datetime(start.year, start.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            return cls(window_type=window_type, start_time=start, end_time=end)

        if window_type == QuotaWindowType.CUSTOM:
            dur = custom_duration_seconds or 3600
            start = now
            end = now + timedelta(seconds=dur)
            return cls(window_type=window_type, start_time=start, end_time=end)

        raise ValueError(f"Unsupported QuotaWindowType: {window_type}")

    def contains(self, dt: datetime) -> bool:
        """Verifica si dt cae dentro del intervalo semi-abierto [start_time, end_time)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return self.start_time <= dt < self.end_time


@dataclass(frozen=True)
class QuotaRule:
    """
    Regla individual e inmutable de cuota dentro de una política.
    """
    rule_id: str
    quota_type: QuotaType
    limit_value: Union[int, Decimal]
    scope: QuotaScope = QuotaScope.TENANT
    window_type: QuotaWindowType = QuotaWindowType.DAY
    target_identifier: Optional[str] = None  # user_id / model_id / provider si aplica
    is_hard_limit: bool = True
    custom_window_seconds: Optional[int] = None
    allow_cache_hit_bypass_token_budget: bool = True

    def __post_init__(self):
        validate_safe_identifier(self.rule_id, "rule_id")
        if self.target_identifier:
            validate_safe_identifier(self.target_identifier, "target_identifier")

        if self.quota_type == QuotaType.MAX_COST:
            if not isinstance(self.limit_value, Decimal):
                object.__setattr__(self, "limit_value", Decimal(str(self.limit_value)))
            if self.limit_value < Decimal("0.00"):
                raise ValueError("Limit value for MAX_COST cannot be negative.")
        else:
            if not isinstance(self.limit_value, int):
                object.__setattr__(self, "limit_value", int(self.limit_value))
            if self.limit_value < 0:
                raise ValueError(f"Limit value for {self.quota_type} cannot be negative.")


@dataclass(frozen=True)
class QuotaPolicy:
    """
    Política completa de cuotas acotada estrictamente a un Tenant.
    """
    policy_id: str
    tenant_id: str
    rules: Tuple[QuotaRule, ...]
    policy_version: str = "1.0.0"
    is_unlimited: bool = False
    description: Optional[str] = None
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.policy_id, "policy_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")

        if isinstance(self.rules, (list, Sequence)):
            object.__setattr__(self, "rules", tuple(self.rules))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise QuotaPolicyIntegrityError(
                f"QuotaPolicy checksum mismatch: provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        rules_repr = [
            f"{r.rule_id}:{r.quota_type.value}:{r.scope.value}:{r.target_identifier}:{r.window_type.value}:{r.limit_value}:{r.is_hard_limit}"
            for r in sorted(self.rules, key=lambda x: x.rule_id)
        ]
        raw = f"{self.policy_id}|{self.tenant_id}|{self.policy_version}|{self.is_unlimited}|{','.join(rules_repr)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()


@dataclass(frozen=True)
class QuotaRequest:
    """
    Petición de evaluación pre-flight para validar si una llamada de inferencia puede proceder.
    """
    tenant_id: str
    identity_id: Optional[str] = None
    model_id: Optional[str] = None
    provider: Optional[str] = None
    task_type: Optional[str] = None
    estimated_input_tokens: Optional[int] = None
    estimated_output_tokens: Optional[int] = None
    estimated_total_tokens: Optional[int] = None
    estimated_cost: Optional[Decimal] = None
    is_cache_hit_predicted: bool = False
    is_cache_hit: bool = False
    correlation_id: Optional[str] = None
    request_timestamp: Optional[datetime] = None

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.identity_id:
            validate_safe_identifier(self.identity_id, "identity_id")
        if self.estimated_cost is not None and not isinstance(self.estimated_cost, Decimal):
            object.__setattr__(self, "estimated_cost", Decimal(str(self.estimated_cost)))
        if self.request_timestamp is None:
            object.__setattr__(self, "request_timestamp", datetime.now(timezone.utc))
        elif self.request_timestamp.tzinfo is None:
            object.__setattr__(self, "request_timestamp", self.request_timestamp.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class RuleEvaluationDetail:
    """
    Detalle de evaluación de una regla específica.
    """
    rule_id: str
    quota_type: QuotaType
    scope: QuotaScope
    limit_value: Union[int, Decimal]
    current_usage: Union[int, Decimal]
    reserved_inflight: Union[int, Decimal]
    requested_estimate: Union[int, Decimal]
    remaining_capacity: Union[int, Decimal]
    window: QuotaWindow
    is_passed: bool
    reason_code: Optional[str] = None


@dataclass(frozen=True)
class QuotaDecision:
    """
    Decisión inmutable y auditable de gobernanza de cuotas.
    """
    decision_id: str
    status: QuotaStatus
    tenant_id: str
    identity_id: Optional[str]
    policy_id: Optional[str]
    policy_version: Optional[str]
    reason_codes: Tuple[str, ...]
    rule_evaluations: Tuple[RuleEvaluationDetail, ...]
    evaluated_at: datetime
    correlation_id: Optional[str] = None
    reservation_id: Optional[str] = None
    rationale: Optional[str] = None
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.decision_id, "decision_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.identity_id:
            validate_safe_identifier(self.identity_id, "identity_id")

        if isinstance(self.reason_codes, (list, Sequence)):
            object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if isinstance(self.rule_evaluations, (list, Sequence)):
            object.__setattr__(self, "rule_evaluations", tuple(self.rule_evaluations))

        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)

    def _compute_checksum(self) -> str:
        eval_repr = [
            f"{e.rule_id}:{e.quota_type.value}:{e.scope.value}:{e.is_passed}:{e.current_usage}:{e.limit_value}"
            for e in self.rule_evaluations
        ]
        raw = f"{self.decision_id}|{self.status.value}|{self.tenant_id}|{self.identity_id}|{self.policy_id}|{','.join(self.reason_codes)}|{','.join(eval_repr)}|{self.evaluated_at.isoformat()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def is_allowed(self) -> bool:
        return self.status == QuotaStatus.ALLOW

    @property
    def remaining_capacity(self) -> Mapping[str, Union[int, Decimal]]:
        """Devuelve un mapa {quota_type: remaining} para inspección rápida."""
        res = {}
        for r in self.rule_evaluations:
            res[r.quota_type.value] = r.remaining_capacity
        return MappingProxyType(res)


@dataclass(frozen=True)
class QuotaReservation:
    """
    Reserva inmutable de capacidad en vuelo para evitar TOCTOU y sobreconsumo concurrente.
    """
    reservation_id: str
    tenant_id: str
    identity_id: Optional[str]
    model_id: Optional[str]
    provider: Optional[str]
    estimated_requests: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_total_tokens: int
    estimated_cost: Decimal
    created_at: datetime
    expires_at: datetime
    status: QuotaReservationStatus
    correlation_id: Optional[str] = None
    source_decision_id: Optional[str] = None
    reconciled_at: Optional[datetime] = None
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.reservation_id, "reservation_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.identity_id:
            validate_safe_identifier(self.identity_id, "identity_id")

        if not isinstance(self.estimated_cost, Decimal):
            object.__setattr__(self, "estimated_cost", Decimal(str(self.estimated_cost)))

        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if self.expires_at.tzinfo is None:
            object.__setattr__(self, "expires_at", self.expires_at.replace(tzinfo=timezone.utc))
        if self.reconciled_at is not None and self.reconciled_at.tzinfo is None:
            object.__setattr__(self, "reconciled_at", self.reconciled_at.replace(tzinfo=timezone.utc))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise QuotaReservationConflictError(
                f"QuotaReservation checksum mismatch: provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        raw = f"{self.reservation_id}|{self.tenant_id}|{self.identity_id}|{self.estimated_requests}|{self.estimated_total_tokens}|{self.estimated_cost}|{self.created_at.isoformat()}|{self.expires_at.isoformat()}|{self.status.value}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_expired(self, current_time: datetime) -> bool:
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        return current_time >= self.expires_at

    def with_status(
        self,
        new_status: QuotaReservationStatus,
        reconciled_at: Optional[datetime] = None,
        actual_tokens: Optional[int] = None,
        actual_cost: Optional[Decimal] = None,
    ) -> "QuotaReservation":
        """Genera una nueva instancia inmutable con estado actualizado."""
        now = reconciled_at or datetime.now(timezone.utc)
        tot_toks = actual_tokens if actual_tokens is not None else self.estimated_total_tokens
        cost = actual_cost if actual_cost is not None else self.estimated_cost
        return QuotaReservation(
            reservation_id=self.reservation_id,
            tenant_id=self.tenant_id,
            identity_id=self.identity_id,
            model_id=self.model_id,
            provider=self.provider,
            estimated_requests=self.estimated_requests,
            estimated_input_tokens=self.estimated_input_tokens,
            estimated_output_tokens=self.estimated_output_tokens,
            estimated_total_tokens=tot_toks,
            estimated_cost=cost,
            created_at=self.created_at,
            expires_at=self.expires_at,
            status=new_status,
            correlation_id=self.correlation_id,
            source_decision_id=self.source_decision_id,
            reconciled_at=now,
        )
