"""
Servicio de Aplicación para Confiabilidad y Ejecución Resiliente (Reliability Engine - Hito K.7).

Coordina:
- Clasificación determinista de fallos según taxonomía de dominio (FailureCategory, FailureRecoverability).
- Aplicación estricta de políticas de reintento acotadas (RetryPolicy, backoff exponencial, respeto a Retry-After).
- Detección y rechazo de reintentos ciegos en mutaciones/side-effects con timeout o incertidumbre (reconciliation obligatoria).
- Control de idempotencia fuerte: misma clave + mismo payload -> resultado cacheado; misma clave + payload distinto -> CONFLICT.
- Aislamiento de fallos en dependencias degradadas mediante Circuit Breaker.
- Emisión auditable hacia K.1 (Audit Trail) y K.2 (Agent Trace) sin alterar el flujo principal ante fallos secundarios de traza.
- Preservación explícita de estados no concluyentes: UNKNOWN != SUCCESS y UNKNOWN != FAILURE confirmado.
- Prevención total de bucles infinitos y retry storms.
"""

import threading
import uuid
import logging
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Optional, Callable, Dict, Any, Tuple

from src.domain.reliability.models import (
    FailureCategory,
    FailureRecoverability,
    classify_failure,
    SystemHealthState,
    CircuitState,
    CircuitBreakerConfig,
    RetryPolicy,
    RecoveryDecision,
    ReliabilityResult,
)
from src.domain.reliability.ports import (
    ClockPort,
    CircuitBreakerPort,
    IdempotencyStorePort,
    ReliabilityEnginePort,
)
from src.infrastructure.reliability.reliability_infrastructure import (
    SystemClock,
    InMemoryCircuitBreaker,
    InMemoryIdempotencyStore,
)
from src.domain.audit.models import AuditRecordType, AuditActorType, AuditRecord, AuditActor
from src.application.audit.audit_trail_service import AuditTrailService
from src.domain.agent_trace.models import StepType, TraceStatus
from src.application.agent_trace.agent_trace_service import AgentTraceService

logger = logging.getLogger(__name__)


class ReliabilityEngine(ReliabilityEnginePort):
    """
    Motor central de ejecución resiliente y confiable.
    """

    def __init__(
        self,
        circuit_breaker: Optional[CircuitBreakerPort] = None,
        idempotency_store: Optional[IdempotencyStorePort] = None,
        clock: Optional[ClockPort] = None,
        audit_trail_service: Optional[AuditTrailService] = None,
        agent_trace_service: Optional[AgentTraceService] = None,
        default_retry_policy: Optional[RetryPolicy] = None,
    ):
        self.clock = clock or SystemClock()
        self.circuit_breaker = circuit_breaker or InMemoryCircuitBreaker(clock=self.clock)
        self.idempotency_store = idempotency_store or InMemoryIdempotencyStore()
        self.audit_trail_service = audit_trail_service
        self.agent_trace_service = agent_trace_service
        self.default_retry_policy = default_retry_policy or RetryPolicy()
        self._execution_lock = threading.Lock()

    def _map_exception_to_category(self, exc: Exception) -> Tuple[FailureCategory, Optional[float]]:
        """
        Mapea deterministamente una excepción de Python / API a su FailureCategory
        y extrae el retry-after si existe.
        """
        exc_str = str(exc).lower()
        exc_type = type(exc).__name__.lower()
        retry_after = getattr(exc, "retry_after", None)

        if hasattr(exc, "status_code"):
            status_code = getattr(exc, "status_code")
            if status_code == 429:
                return FailureCategory.RATE_LIMIT, retry_after
            if status_code in (401, 403):
                return FailureCategory.AUTHORIZATION, None
            if status_code in (400, 422):
                return FailureCategory.VALIDATION, None
            if status_code == 409:
                return FailureCategory.CONFLICT, None
            if status_code in (502, 503, 504):
                return FailureCategory.DEPENDENCY_UNAVAILABLE, retry_after

        if "timeout" in exc_str or "timed out" in exc_str or "timeout" in exc_type:
            return FailureCategory.TIMEOUT, None
        if "rate limit" in exc_str or "429" in exc_str or "too many requests" in exc_str:
            return FailureCategory.RATE_LIMIT, retry_after
        if "unauthorized" in exc_str or "forbidden" in exc_str or "401" in exc_str or "403" in exc_str or "permission" in exc_str:
            return FailureCategory.AUTHORIZATION, None
        if "validation" in exc_str or "valueerror" in exc_type or "schema" in exc_str or "invalid" in exc_str:
            return FailureCategory.VALIDATION, None
        if "conflict" in exc_str or "already exists" in exc_str or "409" in exc_str:
            return FailureCategory.CONFLICT, None
        if "connection" in exc_str or "unavailable" in exc_str or "503" in exc_str or "network" in exc_str:
            return FailureCategory.DEPENDENCY_UNAVAILABLE, retry_after
        if "corruption" in exc_str or "corrupted" in exc_str or "checksum mismatch" in exc_str:
            return FailureCategory.CORRUPTION, None
        if "cancelled" in exc_str or "aborted" in exc_str:
            return FailureCategory.CANCELLED, None
        if "unknown" in exc_str or "uncertain" in exc_str:
            return FailureCategory.UNKNOWN, None

        return FailureCategory.TRANSIENT, None

    def execute_with_reliability(
        self,
        operation_id: str,
        operation_func: Callable[[], Any],
        is_side_effect: bool,
        retry_policy: Optional[RetryPolicy] = None,
        service_name: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        payload: Optional[Any] = None,
        reconcile_func: Optional[Callable[[], Optional[Any]]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ReliabilityResult:
        with self._execution_lock:
            return self._execute_internal(
                operation_id=operation_id,
                operation_func=operation_func,
                is_side_effect=is_side_effect,
                retry_policy=retry_policy,
                service_name=service_name,
                idempotency_key=idempotency_key,
                payload=payload,
                reconcile_func=reconcile_func,
                correlation_id=correlation_id,
                causation_id=causation_id,
                metadata=metadata,
            )

    def _execute_internal(
        self,
        operation_id: str,
        operation_func: Callable[[], Any],
        is_side_effect: bool,
        retry_policy: Optional[RetryPolicy] = None,
        service_name: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        payload: Optional[Any] = None,
        reconcile_func: Optional[Callable[[], Optional[Any]]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ReliabilityResult:
        policy = retry_policy or self.default_retry_policy
        corr_id = correlation_id or f"corr_{operation_id}"
        meta = metadata or {}
        decisions: list[RecoveryDecision] = []

        # 1. Chequeo de Idempotencia estricta
        payload_hash = self.idempotency_store.compute_payload_hash(payload) if idempotency_key else ""
        if idempotency_key:
            existing_record = self.idempotency_store.get(idempotency_key)
            if existing_record:
                # Comprobar si el payload difiere -> Conflicto de Idempotencia
                if existing_record.get("payload_hash") != payload_hash:
                    conflict_decision = RecoveryDecision(
                        decision_id=f"rec_dec_{uuid.uuid4().hex[:10]}",
                        operation_id=operation_id,
                        failure_category=FailureCategory.CONFLICT,
                        recoverability=FailureRecoverability.NON_RETRYABLE,
                        attempt=1,
                        max_attempts=policy.max_attempts,
                        retry_allowed=False,
                        reconciliation_required=False,
                        delay_seconds=0.0,
                        status="IDEMPOTENCY_PAYLOAD_CONFLICT",
                        reason="Idempotency key reused with differing payload. Silent overwrite prohibited.",
                        correlation_id=corr_id,
                        causation_id=causation_id,
                        created_at=self.clock.now(),
                    )
                    return ReliabilityResult(
                        operation_id=operation_id,
                        is_success=False,
                        status="IDEMPOTENCY_CONFLICT",
                        failure_category=FailureCategory.CONFLICT,
                        recoverability=FailureRecoverability.NON_RETRYABLE,
                        attempts_executed=0,
                        error_message="Idempotency key collision with conflicting payload",
                        recovery_decisions=(conflict_decision,),
                        correlation_id=corr_id,
                        causation_id=causation_id,
                        metadata=meta,
                    )
                
                # Mismo payload -> Retornar resultado existente sin ejecutar de nuevo
                return ReliabilityResult(
                    operation_id=operation_id,
                    is_success=existing_record.get("status") == "SUCCESS",
                    status=existing_record.get("status", "SUCCESS"),
                    output=existing_record.get("result"),
                    attempts_executed=0,
                    correlation_id=corr_id,
                    causation_id=causation_id,
                    metadata=meta,
                )

        # 2. Chequeo de Circuit Breaker
        svc = service_name or "default_service"
        if not self.circuit_breaker.allow_request(svc):
            circuit_decision = RecoveryDecision(
                decision_id=f"rec_dec_{uuid.uuid4().hex[:10]}",
                operation_id=operation_id,
                failure_category=FailureCategory.DEPENDENCY_UNAVAILABLE,
                recoverability=FailureRecoverability.NON_RETRYABLE,
                attempt=1,
                max_attempts=policy.max_attempts,
                retry_allowed=False,
                reconciliation_required=False,
                delay_seconds=0.0,
                status="CIRCUIT_BREAKER_OPEN",
                reason=f"Circuit Breaker is OPEN for service '{svc}'. Short-circuiting execution.",
                correlation_id=corr_id,
                causation_id=causation_id,
                created_at=self.clock.now(),
            )
            return ReliabilityResult(
                operation_id=operation_id,
                is_success=False,
                status="CIRCUIT_OPEN",
                failure_category=FailureCategory.DEPENDENCY_UNAVAILABLE,
                recoverability=FailureRecoverability.NON_RETRYABLE,
                attempts_executed=0,
                degraded=True,
                error_message=f"Circuit breaker is OPEN for {svc}",
                recovery_decisions=(circuit_decision,),
                correlation_id=corr_id,
                causation_id=causation_id,
                metadata=meta,
            )

        # 3. Bucle de ejecución con políticas de reintento acotadas
        attempt = 1
        while attempt <= policy.max_attempts:
            try:
                # Registrar paso observable si Agent Trace está conectado
                if self.agent_trace_service:
                    try:
                        self.agent_trace_service.record_step(
                            component_name="ReliabilityEngine",
                            execution_id=corr_id,
                            step_type=StepType.TOOL_CALL if is_side_effect else StepType.SERVICE_CALL,
                            operation=f"execute_{operation_id}",
                            step_number=attempt,
                            input_reference=f"attempt_{attempt}",
                            status=TraceStatus.STARTED,
                        )
                    except Exception as trace_err:
                        logger.warning(f"Non-critical agent trace error: {trace_err}")

                result = operation_func()

                # Notificar éxito al Circuit Breaker
                self.circuit_breaker.record_success(svc)

                # Persistir en idempotency store si exitoso
                if idempotency_key:
                    self.idempotency_store.save(
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                        result=result if isinstance(result, (dict, list, str, int, float, bool)) else {"repr": str(result)},
                        status="SUCCESS",
                    )

                # Registrar auditoría si aplica
                if self.audit_trail_service:
                    try:
                        record = AuditRecord(
                            audit_id=f"aud-rel-{uuid.uuid4().hex[:12]}",
                            record_type=AuditRecordType.RESULT_RECORDED,
                            occurred_at=self.clock.now(),
                            actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="reliability_engine"),
                            subject_type="RELIABILITY_OPERATION",
                            subject_id=operation_id,
                            action_or_operation=f"execute_{operation_id}",
                            status="SUCCESS",
                            correlation_id=corr_id,
                            causation_id=causation_id,
                            mission_id=meta.get("mission_id"),
                        )
                        self.audit_trail_service.audit_repository.append(record)
                    except Exception as audit_err:
                        logger.warning(f"Non-critical audit error: {audit_err}")

                return ReliabilityResult(
                    operation_id=operation_id,
                    is_success=True,
                    status="SUCCESS",
                    output=result,
                    attempts_executed=attempt,
                    recovery_decisions=tuple(decisions),
                    correlation_id=corr_id,
                    causation_id=causation_id,
                    metadata=meta,
                )

            except Exception as exc:
                cat, retry_after = self._map_exception_to_category(exc)
                recoverability = classify_failure(cat, is_side_effect=is_side_effect)

                # Notificar fallo al Circuit Breaker
                self.circuit_breaker.record_failure(svc, cat, str(exc))

                # Caso Side Effect con Timeout o UNKNOWN: Se requiere reconciliación antes de retry
                if is_side_effect and recoverability == FailureRecoverability.RECONCILIATION_REQUIRED:
                    recon_decision = RecoveryDecision(
                        decision_id=f"rec_dec_{uuid.uuid4().hex[:10]}",
                        operation_id=operation_id,
                        failure_category=cat,
                        recoverability=recoverability,
                        attempt=attempt,
                        max_attempts=policy.max_attempts,
                        retry_allowed=False,
                        reconciliation_required=True,
                        delay_seconds=0.0,
                        status="RECONCILIATION_TRIGGERED",
                        reason=f"Side effect operation produced {cat.value}. Must reconcile external state before attempting retry.",
                        correlation_id=corr_id,
                        causation_id=causation_id,
                        created_at=self.clock.now(),
                        evidence={"exception": str(exc)},
                    )
                    decisions.append(recon_decision)

                    if reconcile_func:
                        try:
                            reconciled_output = reconcile_func()
                            if reconciled_output is not None:
                                # Reconciliación demostró que el efecto ya ocurrió exitosamente
                                if idempotency_key:
                                    self.idempotency_store.save(
                                        idempotency_key=idempotency_key,
                                        payload_hash=payload_hash,
                                        result=reconciled_output if isinstance(reconciled_output, (dict, list, str, int, float, bool)) else {"repr": str(reconciled_output)},
                                        status="SUCCESS",
                                    )

                                # Registrar auditoría de reconciliación
                                if self.audit_trail_service:
                                    try:
                                        record = AuditRecord(
                                            audit_id=f"aud-rel-{uuid.uuid4().hex[:12]}",
                                            record_type=AuditRecordType.RESULT_RECORDED,
                                            occurred_at=self.clock.now(),
                                            actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="reliability_engine"),
                                            subject_type="RELIABILITY_OPERATION",
                                            subject_id=operation_id,
                                            action_or_operation=f"execute_{operation_id}",
                                            status="SUCCESS",
                                            correlation_id=corr_id,
                                            causation_id=causation_id,
                                            mission_id=meta.get("mission_id"),
                                        )
                                        self.audit_trail_service.audit_repository.append(record)
                                    except Exception as audit_err:
                                        logger.warning(f"Non-critical audit error: {audit_err}")

                                return ReliabilityResult(
                                    operation_id=operation_id,
                                    is_success=True,
                                    status="RECONCILED",
                                    output=reconciled_output,
                                    failure_category=cat,
                                    recoverability=recoverability,
                                    attempts_executed=attempt,
                                    reconciled=True,
                                    recovery_decisions=tuple(decisions),
                                    correlation_id=corr_id,
                                    causation_id=causation_id,
                                    metadata=meta,
                                )
                        except Exception as recon_err:
                            logger.warning(f"Reconciliation check failed: {recon_err}")

                    # Si no hay reconciliador o este no pudo confirmar éxito, preservar UNKNOWN / TIMEOUT sin reintento ciego
                    return ReliabilityResult(
                        operation_id=operation_id,
                        is_success=False,
                        status="UNKNOWN" if cat == FailureCategory.UNKNOWN else "TIMEOUT",
                        failure_category=cat,
                        recoverability=recoverability,
                        attempts_executed=attempt,
                        error_message=f"Operation side effect unconfirmed ({cat.value}). Duplicate prevented.",
                        recovery_decisions=tuple(decisions),
                        correlation_id=corr_id,
                        causation_id=causation_id,
                        metadata=meta,
                    )

                # Determinar si es retryable
                can_retry = (
                    recoverability == FailureRecoverability.RETRYABLE
                    and policy.is_retryable(cat, is_side_effect=is_side_effect)
                    and attempt < policy.max_attempts
                )

                delay = policy.compute_delay(attempt, retry_after) if can_retry else 0.0

                decision = RecoveryDecision(
                    decision_id=f"rec_dec_{uuid.uuid4().hex[:10]}",
                    operation_id=operation_id,
                    failure_category=cat,
                    recoverability=recoverability,
                    attempt=attempt,
                    max_attempts=policy.max_attempts,
                    retry_allowed=can_retry,
                    reconciliation_required=recoverability == FailureRecoverability.RECONCILIATION_REQUIRED,
                    delay_seconds=delay,
                    status="RETRY_SCHEDULED" if can_retry else "NON_RETRYABLE_STOP",
                    reason=f"Failure classified as {cat.value} ({recoverability.value}).",
                    correlation_id=corr_id,
                    causation_id=causation_id,
                    created_at=self.clock.now(),
                    evidence={"exception": str(exc), "retry_after": retry_after},
                )
                decisions.append(decision)

                if can_retry:
                    self.clock.sleep(delay)
                    attempt += 1
                    continue
                else:
                    # No retryable o intentos agotados
                    if attempt >= policy.max_attempts and (len(decisions) > 1 or attempt > 1 or policy.max_attempts > 1):
                        final_status = "RETRY_EXHAUSTED"
                    else:
                        final_status = "FAILED"
                        if cat == FailureCategory.UNKNOWN:
                            final_status = "UNKNOWN"
                        elif cat == FailureCategory.TIMEOUT:
                            final_status = "TIMEOUT"
                        elif cat == FailureCategory.RATE_LIMIT:
                            final_status = "RATE_LIMITED"
                        elif cat == FailureCategory.AUTHORIZATION:
                            final_status = "AUTHORIZATION_FAILED"
                        elif cat == FailureCategory.CONFLICT:
                            final_status = "CONFLICT"

                    return ReliabilityResult(
                        operation_id=operation_id,
                        is_success=False,
                        status=final_status,
                        failure_category=cat,
                        recoverability=recoverability,
                        attempts_executed=attempt,
                        error_message=str(exc),
                        recovery_decisions=tuple(decisions),
                        correlation_id=corr_id,
                        causation_id=causation_id,
                        metadata=meta,
                    )

        # Si agotó intentos
        return ReliabilityResult(
            operation_id=operation_id,
            is_success=False,
            status="RETRY_EXHAUSTED",
            failure_category=FailureCategory.TRANSIENT,
            recoverability=FailureRecoverability.RETRYABLE,
            attempts_executed=policy.max_attempts,
            error_message="Retry attempts exhausted without success",
            recovery_decisions=tuple(decisions),
            correlation_id=corr_id,
            causation_id=causation_id,
            metadata=meta,
        )
