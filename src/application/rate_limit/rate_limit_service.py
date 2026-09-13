"""
Servicio de Aplicación para Rate-limit Management (Hito P.11 — Production / Operations).

Responsabilidades:
1. Implementar RateLimitServicePort.
2. Token Bucket determinista y explicable con capacidad de ráfaga (burst) y tasa de recarga por segundo.
3. Precedencia estricta:
   - Platform Hard Limit -> Provider Hard Limit -> Plan/Policy Limit -> Tenant Override más restrictivo.
4. Claves canónicas inequívocas: `env:{env}:tenant:{tenant}:scope:{scope}:target:{target}:rule:{rule_id}`
5. Aislamiento absoluto por Environment (DEV, STAGING, PROD) y Tenant.
6. Soporte de Scopes:
   - TENANT: Tasa agregada para el tenant.
   - USER: Fairness interna de usuario dentro del tenant.
   - PROVIDER: Protección de upstream (e.g. max 100 req/min a OpenAI). Denegación previa a cualquier llamada.
   - MODEL: Límites por modelo específico (e.g. gpt-4 vs gpt-3.5).
   - ENDPOINT: Límites por ruta o endpoint específico.
7. Cálculo determinista de `Retry-After` (en segundos enteros > 0) y metadatos estándar (limit, remaining, reset_at).
8. Fail-safe ante reglas faltantes o corruptas (fail-closed por defecto en producción).
9. Integración desacoplada con Monitoreo (P.7), Alerting (P.8) y Auditoría (K.1).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
import math
import threading
from typing import Optional, List, Dict, Any, Mapping, Tuple, Sequence, Union
import uuid

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.monitoring.models import resolve_environment
from src.domain.reliability.ports import ClockPort
from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.rate_limit.models import (
    RateLimitScope,
    RateLimitStatus,
    RateLimitWindowUnit,
    RateLimitRule,
    RateLimitPolicy,
    RateLimitRequest,
    RateLimitDecision,
    RuleEvaluationResult,
    RateLimitError,
    RateLimitPolicyNotFoundError,
    RateLimitPolicyIntegrityError,
    build_canonical_rate_limit_key,
)
from src.domain.rate_limit.ports import (
    RateLimitPolicyRepositoryPort,
    RateLimitStateStorePort,
    RateLimitTelemetryPort,
    RateLimitServicePort,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.security.models import sanitize_security_data

logger = logging.getLogger("RateLimitService")


class RateLimitService(RateLimitServicePort):
    """
    Servicio de orquestación y evaluación de Rate Limiting en runtime (P.11).
    """

    def __init__(
        self,
        policy_repository: RateLimitPolicyRepositoryPort,
        state_store: RateLimitStateStorePort,
        telemetry: Optional[RateLimitTelemetryPort] = None,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        environment: Union[ApplicationEnvironment, str] = ApplicationEnvironment.PRODUCTION,
        fail_closed_on_missing_policy: bool = True,
    ) -> None:
        self.policy_repository = policy_repository
        self.state_store = state_store
        self.telemetry = telemetry
        self.clock = clock
        self.audit_repository = audit_repository
        self.environment = resolve_environment(environment)
        self.fail_closed_on_missing_policy = fail_closed_on_missing_policy
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        if self.clock is not None:
            ts = self.clock.now()
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            return ts
        return datetime.now(timezone.utc)

    def _emit_audit_denial(
        self,
        request: RateLimitRequest,
        decision: RateLimitDecision,
    ) -> None:
        if not self.audit_repository:
            return
        try:
            actor = AuditActor(
                actor_type=AuditActorType.SYSTEM if not request.identity_id else AuditActorType.USER,
                actor_id=request.identity_id or "system",
                details={"tenant_id": request.tenant_id},
            )
            audit_id = f"aud_rl_{uuid.uuid4().hex[:12]}"
            details = {
                "rule_id": decision.rule_id_violated,
                "scope": decision.scope_violated.value if decision.scope_violated else None,
                "retry_after_seconds": decision.retry_after_seconds,
                "provider": request.provider,
                "model_id": request.model_id,
                "reason_codes": list(decision.reason_codes),
            }
            record = AuditRecord(
                audit_id=audit_id,
                record_type=AuditRecordType.OPERATION_DENIED,
                occurred_at=decision.evaluated_at or self._now(),
                actor=actor,
                subject_type="RATE_LIMIT",
                subject_id=request.tenant_id or "global",
                action_or_operation="RATE_LIMIT_CHECK",
                status="DENIED",
                correlation_id=request.correlation_id or decision.correlation_id,
                metadata=sanitize_security_data(details),
            )
            self.audit_repository.save(record)
        except Exception as e:
            logger.warning(f"Failed to emit audit record for rate limit denial: {e}")

    def _is_rule_applicable(self, rule: RateLimitRule, request: RateLimitRequest) -> bool:
        """Determina si una regla aplica al request dado."""
        if rule.scope == RateLimitScope.TENANT:
            # Regla de tenant aplica si el request tiene tenant_id
            return bool(request.tenant_id)
        elif rule.scope == RateLimitScope.USER:
            # Regla de user aplica si el request tiene identity_id y coincide con target_identifier si está fijado
            if not request.identity_id:
                return False
            return rule.target_identifier is None or rule.target_identifier == request.identity_id
        elif rule.scope == RateLimitScope.PROVIDER:
            if not request.provider:
                return False
            return rule.target_identifier is None or rule.target_identifier == request.provider
        elif rule.scope == RateLimitScope.MODEL:
            if not request.model_id:
                return False
            return rule.target_identifier is None or rule.target_identifier == request.model_id
        elif rule.scope == RateLimitScope.ENDPOINT:
            if not request.endpoint:
                return False
            return rule.target_identifier is None or rule.target_identifier == request.endpoint
        return False

    def _resolve_applicable_rules(self, request: RateLimitRequest) -> List[RateLimitRule]:
        """
        Resuelve y ordena las reglas aplicables siguiendo el orden de precedencia:
        1. Reglas de Plataforma / Globales (hard limits)
        2. Reglas de Proveedor / Upstream
        3. Reglas de Tenant Policy
        4. Reglas de Usuario
        """
        all_rules: List[RateLimitRule] = []

        # 1. Política Global / Platform
        global_policy = self.policy_repository.get_policy(tenant_id=None)
        if global_policy and not global_policy.is_unlimited:
            for r in global_policy.rules:
                if self._is_rule_applicable(r, request):
                    all_rules.append(r)

        # 2. Política de Tenant
        if request.tenant_id:
            tenant_policy = self.policy_repository.get_policy(tenant_id=request.tenant_id)
            if tenant_policy:
                if not tenant_policy.is_unlimited:
                    for r in tenant_policy.rules:
                        if self._is_rule_applicable(r, request):
                            all_rules.append(r)
            elif self.fail_closed_on_missing_policy and not global_policy:
                # Si no hay política de tenant y se exige fail closed
                return []

        # Ordenar por especificidad / precedencia:
        # Platform hard limits -> Provider -> Model -> Tenant -> User -> Endpoint
        def scope_order(s: RateLimitScope) -> int:
            ordering = {
                RateLimitScope.PROVIDER: 1,
                RateLimitScope.MODEL: 2,
                RateLimitScope.TENANT: 3,
                RateLimitScope.USER: 4,
                RateLimitScope.ENDPOINT: 5,
            }
            return ordering.get(s, 10)

        all_rules.sort(key=lambda r: scope_order(r.scope))
        return all_rules

    def check_and_consume(
        self,
        request: RateLimitRequest,
        context: Optional[TenantContext] = None,
    ) -> RateLimitDecision:
        if context and request.tenant_id:
            CrossTenantGuard.validate_access(context, request.tenant_id)

        now = request.requested_at or self._now()
        env = resolve_environment(request.environment or self.environment)
        corr_id = request.correlation_id or f"rl_{uuid.uuid4().hex[:12]}"

        # 1. Verificar si existe política global o tenant
        global_policy = self.policy_repository.get_policy(tenant_id=None)
        tenant_policy = self.policy_repository.get_policy(tenant_id=request.tenant_id) if request.tenant_id else None

        # Si el tenant o global es explícitamente ilimitado
        if (tenant_policy and tenant_policy.is_unlimited) or (not tenant_policy and global_policy and global_policy.is_unlimited):
            reset_at = now + timedelta(seconds=60)
            decision = RateLimitDecision(
                status=RateLimitStatus.ALLOW,
                limit=999999,
                remaining=999999,
                reset_at=reset_at,
                retry_after_seconds=0,
                reason_codes=("EXPLICIT_UNLIMITED",),
                evaluated_at=now,
                correlation_id=corr_id,
                rationale="Explicit unlimited rate limit policy.",
            )
            if self.telemetry:
                self.telemetry.emit_rate_limit_fact(decision, request)
            return decision

        applicable_rules = self._resolve_applicable_rules(request)

        # Missing policy handling
        if not applicable_rules:
            if not global_policy and not tenant_policy:
                if self.fail_closed_on_missing_policy:
                    reset_at = now + timedelta(seconds=60)
                    decision = RateLimitDecision(
                        status=RateLimitStatus.DENY,
                        limit=0,
                        remaining=0,
                        reset_at=reset_at,
                        retry_after_seconds=60,
                        reason_codes=("MISSING_POLICY_FAIL_CLOSED",),
                        evaluated_at=now,
                        correlation_id=corr_id,
                        rationale="Missing rate limit policy under fail-closed configuration.",
                    )
                    self._emit_audit_denial(request, decision)
                    if self.telemetry:
                        self.telemetry.emit_rate_limit_fact(decision, request)
                    return decision
                else:
                    reset_at = now + timedelta(seconds=60)
                    decision = RateLimitDecision(
                        status=RateLimitStatus.ALLOW,
                        limit=999999,
                        remaining=999999,
                        reset_at=reset_at,
                        retry_after_seconds=0,
                        reason_codes=("UNCONFIGURED_POLICY_ALLOW",),
                        evaluated_at=now,
                        correlation_id=corr_id,
                        rationale="No rate limit rules configured, allowed by policy.",
                    )
                    if self.telemetry:
                        self.telemetry.emit_rate_limit_fact(decision, request)
                    return decision

        # 2. Evaluación atómica de todas las reglas
        # Para evitar sobreconsumo parcial si falla una regla posterior en la cadena,
        # primero evaluamos si todas las reglas tienen tokens suficientes.
        rule_evaluations: List[RuleEvaluationResult] = []
        keys_to_consume: List[Tuple[str, RateLimitRule]] = []
        is_all_allowed = True
        first_blocking_eval: Optional[RuleEvaluationResult] = None
        max_retry_after = 0
        min_remaining = 999999999
        min_limit = 999999999

        with self._lock:
            for rule in applicable_rules:
                target_id = request.identity_id if rule.scope == RateLimitScope.USER else (
                    request.provider if rule.scope == RateLimitScope.PROVIDER else (
                        request.model_id if rule.scope == RateLimitScope.MODEL else (
                            request.endpoint if rule.scope == RateLimitScope.ENDPOINT else (
                                rule.target_identifier or "all"
                            )
                        )
                    )
                )
                canon_key = build_canonical_rate_limit_key(
                    environment=env,
                    tenant_id=request.tenant_id if rule.scope != RateLimitScope.PROVIDER else None,
                    scope=rule.scope,
                    target_identifier=target_id,
                    rule_id=rule.rule_id,
                )

                # Simular o calcular tokens disponibles
                existing_state = self.state_store.get_state(canon_key)
                if existing_state is None:
                    curr_tok = float(rule.effective_burst)
                else:
                    elapsed = max(0.0, (now - existing_state.last_refill_at).total_seconds())
                    refill = elapsed * rule.refill_rate_per_second
                    curr_tok = min(float(rule.effective_burst), existing_state.tokens + refill)

                rem_int = int(math.floor(curr_tok))
                min_remaining = min(min_remaining, max(0, rem_int - request.cost_units))
                min_limit = min(min_limit, rule.limit_rate)

                if curr_tok < float(request.cost_units):
                    is_all_allowed = False
                    deficit = float(request.cost_units) - curr_tok
                    rule_retry = int(math.ceil(deficit / rule.refill_rate_per_second)) if rule.refill_rate_per_second > 0 else 60
                    rule_retry = max(1, rule_retry)
                    max_retry_after = max(max_retry_after, rule_retry)

                    eval_res = RuleEvaluationResult(
                        rule_id=rule.rule_id,
                        scope=rule.scope,
                        target_identifier=target_id,
                        limit_rate=rule.limit_rate,
                        burst_capacity=rule.effective_burst,
                        current_tokens=curr_tok,
                        remaining=rem_int,
                        is_passed=False,
                        retry_after_seconds=rule_retry,
                        reason_code=f"RATE_LIMIT_EXCEEDED_{rule.scope.value}",
                    )
                    rule_evaluations.append(eval_res)
                    if first_blocking_eval is None:
                        first_blocking_eval = eval_res
                else:
                    eval_res = RuleEvaluationResult(
                        rule_id=rule.rule_id,
                        scope=rule.scope,
                        target_identifier=target_id,
                        limit_rate=rule.limit_rate,
                        burst_capacity=rule.effective_burst,
                        current_tokens=curr_tok,
                        remaining=rem_int,
                        is_passed=True,
                        retry_after_seconds=0,
                        reason_code="RULE_PASSED",
                    )
                    rule_evaluations.append(eval_res)
                    keys_to_consume.append((canon_key, rule))

            # Si todas pasan, consumir atómicamente de todos los buckets
            if is_all_allowed:
                for canon_key, rule in keys_to_consume:
                    self.state_store.consume_token(
                        key=canon_key,
                        cost_units=request.cost_units,
                        refill_rate_per_sec=rule.refill_rate_per_second,
                        burst_capacity=rule.effective_burst,
                        now=now,
                    )
                reset_at = now + timedelta(seconds=60)
                decision = RateLimitDecision(
                    status=RateLimitStatus.ALLOW,
                    limit=min_limit if min_limit != 999999999 else 60,
                    remaining=max(0, min_remaining),
                    reset_at=reset_at,
                    retry_after_seconds=0,
                    reason_codes=("RATE_LIMIT_ALLOW",),
                    rule_evaluations=tuple(rule_evaluations),
                    evaluated_at=now,
                    correlation_id=corr_id,
                    rationale="All rate limit rules satisfied.",
                )
            else:
                reset_at = now + timedelta(seconds=max_retry_after)
                decision = RateLimitDecision(
                    status=RateLimitStatus.DENY,
                    limit=min_limit if min_limit != 999999999 else 0,
                    remaining=0,
                    reset_at=reset_at,
                    retry_after_seconds=max_retry_after,
                    scope_violated=first_blocking_eval.scope if first_blocking_eval else None,
                    rule_id_violated=first_blocking_eval.rule_id if first_blocking_eval else None,
                    reason_codes=(first_blocking_eval.reason_code,) if first_blocking_eval else ("RATE_LIMIT_DENIED",),
                    rule_evaluations=tuple(rule_evaluations),
                    evaluated_at=now,
                    correlation_id=corr_id,
                    rationale=f"Rate limit exceeded for scope {first_blocking_eval.scope.value if first_blocking_eval else 'UNKNOWN'}.",
                )
                self._emit_audit_denial(request, decision)

        if self.telemetry:
            self.telemetry.emit_rate_limit_fact(decision, request)

        return decision

    def check_only(
        self,
        request: RateLimitRequest,
        context: Optional[TenantContext] = None,
    ) -> RateLimitDecision:
        """Inspección idempotente de rate limit sin consumir tokens."""
        if context and request.tenant_id:
            CrossTenantGuard.validate_access(context, request.tenant_id)

        now = request.requested_at or self._now()
        env = resolve_environment(request.environment or self.environment)
        corr_id = request.correlation_id or f"rl_ro_{uuid.uuid4().hex[:12]}"

        global_policy = self.policy_repository.get_policy(tenant_id=None)
        tenant_policy = self.policy_repository.get_policy(tenant_id=request.tenant_id) if request.tenant_id else None

        if (tenant_policy and tenant_policy.is_unlimited) or (not tenant_policy and global_policy and global_policy.is_unlimited):
            return RateLimitDecision(
                status=RateLimitStatus.ALLOW,
                limit=999999,
                remaining=999999,
                reset_at=now + timedelta(seconds=60),
                retry_after_seconds=0,
                reason_codes=("EXPLICIT_UNLIMITED",),
                evaluated_at=now,
                correlation_id=corr_id,
                rationale="Explicit unlimited rate limit policy.",
            )

        applicable_rules = self._resolve_applicable_rules(request)
        if not applicable_rules:
            if not global_policy and not tenant_policy:
                status = RateLimitStatus.DENY if self.fail_closed_on_missing_policy else RateLimitStatus.ALLOW
                return RateLimitDecision(
                    status=status,
                    limit=0 if status == RateLimitStatus.DENY else 999999,
                    remaining=0 if status == RateLimitStatus.DENY else 999999,
                    reset_at=now + timedelta(seconds=60),
                    retry_after_seconds=60 if status == RateLimitStatus.DENY else 0,
                    reason_codes=("MISSING_POLICY_FAIL_CLOSED" if status == RateLimitStatus.DENY else "UNCONFIGURED_POLICY_ALLOW",),
                    evaluated_at=now,
                    correlation_id=corr_id,
                    rationale="Missing policy inspection.",
                )

        rule_evaluations: List[RuleEvaluationResult] = []
        is_all_allowed = True
        first_blocking: Optional[RuleEvaluationResult] = None
        max_retry_after = 0
        min_remaining = 999999999
        min_limit = 999999999

        for rule in applicable_rules:
            target_id = request.identity_id if rule.scope == RateLimitScope.USER else (
                request.provider if rule.scope == RateLimitScope.PROVIDER else (
                    request.model_id if rule.scope == RateLimitScope.MODEL else (
                        request.endpoint if rule.scope == RateLimitScope.ENDPOINT else (
                            rule.target_identifier or "all"
                        )
                    )
                )
            )
            canon_key = build_canonical_rate_limit_key(
                environment=env,
                tenant_id=request.tenant_id if rule.scope != RateLimitScope.PROVIDER else None,
                scope=rule.scope,
                target_identifier=target_id,
                rule_id=rule.rule_id,
            )
            existing_state = self.state_store.get_state(canon_key)
            if existing_state is None:
                curr_tok = float(rule.effective_burst)
            else:
                elapsed = max(0.0, (now - existing_state.last_refill_at).total_seconds())
                refill = elapsed * rule.refill_rate_per_second
                curr_tok = min(float(rule.effective_burst), existing_state.tokens + refill)

            rem_int = int(math.floor(curr_tok))
            min_remaining = min(min_remaining, max(0, rem_int - request.cost_units))
            min_limit = min(min_limit, rule.limit_rate)

            if curr_tok < float(request.cost_units):
                is_all_allowed = False
                deficit = float(request.cost_units) - curr_tok
                rule_retry = int(math.ceil(deficit / rule.refill_rate_per_second)) if rule.refill_rate_per_second > 0 else 60
                rule_retry = max(1, rule_retry)
                max_retry_after = max(max_retry_after, rule_retry)

                eval_res = RuleEvaluationResult(
                    rule_id=rule.rule_id,
                    scope=rule.scope,
                    target_identifier=target_id,
                    limit_rate=rule.limit_rate,
                    burst_capacity=rule.effective_burst,
                    current_tokens=curr_tok,
                    remaining=rem_int,
                    is_passed=False,
                    retry_after_seconds=rule_retry,
                    reason_code=f"RATE_LIMIT_EXCEEDED_{rule.scope.value}",
                )
                rule_evaluations.append(eval_res)
                if first_blocking is None:
                    first_blocking = eval_res
            else:
                eval_res = RuleEvaluationResult(
                    rule_id=rule.rule_id,
                    scope=rule.scope,
                    target_identifier=target_id,
                    limit_rate=rule.limit_rate,
                    burst_capacity=rule.effective_burst,
                    current_tokens=curr_tok,
                    remaining=rem_int,
                    is_passed=True,
                    retry_after_seconds=0,
                    reason_code="RULE_PASSED",
                )
                rule_evaluations.append(eval_res)

        if is_all_allowed:
            return RateLimitDecision(
                status=RateLimitStatus.ALLOW,
                limit=min_limit if min_limit != 999999999 else 60,
                remaining=max(0, min_remaining),
                reset_at=now + timedelta(seconds=60),
                retry_after_seconds=0,
                reason_codes=("RATE_LIMIT_ALLOW",),
                rule_evaluations=tuple(rule_evaluations),
                evaluated_at=now,
                correlation_id=corr_id,
                rationale="All rate limit rules satisfied (read-only check).",
            )
        else:
            return RateLimitDecision(
                status=RateLimitStatus.DENY,
                limit=min_limit if min_limit != 999999999 else 0,
                remaining=0,
                reset_at=now + timedelta(seconds=max_retry_after),
                retry_after_seconds=max_retry_after,
                scope_violated=first_blocking.scope if first_blocking else None,
                rule_id_violated=first_blocking.rule_id if first_blocking else None,
                reason_codes=(first_blocking.reason_code,) if first_blocking else ("RATE_LIMIT_DENIED",),
                rule_evaluations=tuple(rule_evaluations),
                evaluated_at=now,
                correlation_id=corr_id,
                rationale=f"Rate limit exceeded for scope {first_blocking.scope.value if first_blocking else 'UNKNOWN'} (read-only check).",
            )
