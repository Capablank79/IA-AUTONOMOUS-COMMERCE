"""
Servicio de Aplicación para Quota Management SaaS (Hito O.7 — Quota Management).

Responsabilidades:
- Implementar QuotaManagementServicePort.
- Evaluar reglas de cuota de forma determinista y auditable contra los agregados de consumo histórico provistos por O.6 Usage Metering y la capacidad en vuelo reservada.
- Prevenir condiciones de carrera y sobreconsumo concurrente (TOCTOU) mediante reservas atómicas (QuotaReservation).
- Soportar tipos de cuota: MAX_REQUESTS, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, MAX_TOTAL_TOKENS, MAX_COST (Decimal), REQUESTS_PER_MINUTE, REQUESTS_PER_HOUR, TOKENS_PER_PERIOD.
- Jerarquía y scopes: TENANT, USER, MODEL, PROVIDER (todas las reglas aplicables deben cumplirse; precedencia segura: fail-closed).
- Ventanas temporales deterministas (MINUTE, HOUR, DAY, MONTH, CUSTOM, UNLIMITED) utilizando ClockPort K.7.
- Manejo explícito de Cache Hits según configuración de regla (evitar bloquear injustificadamente o consumir cuota de tokens cuando se provee cache hit policy).
- Manejo de UNKNOWN usage o token estimate: fail-safe policy (UNKNOWN / BLOCK si la estimación es requerida pero indefinida).
- Idempotencia estricta por correlation_id para reservas en vuelo.
- Integración desacoplada con Audit Trail K.1 y Agent Trace K.2.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import threading
from typing import Optional, List, Dict, Any, Mapping, Tuple
import uuid

from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.domain.usage_metering.models import (
    UsageQuery,
    UsagePeriod,
    UsagePeriodType,
    UsageAggregate,
)
from src.domain.usage_metering.ports import UsageMeteringServicePort
from src.domain.quota_management.models import (
    QuotaPolicy,
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
    QuotaWindow,
    QuotaRequest,
    QuotaDecision,
    QuotaStatus,
    QuotaReservation,
    QuotaReservationStatus,
    RuleEvaluationDetail,
    QuotaPolicyNotFoundError,
    QuotaReservationConflictError,
    QuotaReservationNotFoundError,
)
from src.domain.quota_management.ports import (
    QuotaPolicyRepositoryPort,
    QuotaReservationRepositoryPort,
    QuotaManagementServicePort,
    QuotaAuditPort,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort


class QuotaManagementService(QuotaManagementServicePort):
    """
    Servicio central de gobernanza de presupuestos de IA y rate limiting SaaS.
    """

    def __init__(
        self,
        policy_repository: QuotaPolicyRepositoryPort,
        reservation_repository: QuotaReservationRepositoryPort,
        usage_metering_service: UsageMeteringServicePort,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        agent_trace_repository: Optional[AgentTraceRepositoryPort] = None,
        default_reservation_ttl_seconds: int = 120,
        fail_closed_on_missing_policy: bool = True,
    ):
        self.policy_repository = policy_repository
        self.reservation_repository = reservation_repository
        self.usage_metering_service = usage_metering_service
        self.clock = clock
        self.audit_repository = audit_repository
        self.agent_trace_repository = agent_trace_repository
        self.default_reservation_ttl_seconds = default_reservation_ttl_seconds
        self.fail_closed_on_missing_policy = fail_closed_on_missing_policy
        self._lock = threading.RLock()

    def _get_now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def _emit_audit(
        self,
        record_type: AuditRecordType,
        tenant_id: str,
        actor_id: Optional[str],
        action: str,
        status: str,
        details: Mapping[str, Any],
        correlation_id: Optional[str] = None,
    ) -> None:
        if self.audit_repository is None:
            return
        try:
            rec = AuditRecord(
                audit_id=f"aud_qta_{uuid.uuid4().hex[:12]}",
                occurred_at=self._get_now(),
                actor=AuditActor(
                    actor_type=AuditActorType.USER if actor_id else AuditActorType.SYSTEM,
                    actor_id=actor_id or tenant_id,
                ),
                record_type=record_type,
                subject_type="QUOTA_MANAGEMENT",
                subject_id=tenant_id,
                action_or_operation=action,
                status=status,
                correlation_id=correlation_id or f"corr_{uuid.uuid4().hex[:8]}",
                metadata=dict(details),
            )
            self.audit_repository.append(rec)
        except Exception:
            pass

    def evaluate_and_reserve(
        self,
        request: QuotaRequest,
        context: Optional[TenantContext] = None,
    ) -> QuotaDecision:
        """
        Evalúa las cuotas aplicables y, si ALLOW, crea una reserva en vuelo atómica.
        Thread-safe para evitar condiciones de carrera (TOCTOU).
        """
        if context is not None:
            CrossTenantGuard.ensure_tenant_context(context)
            CrossTenantGuard.assert_same_tenant(context, request.tenant_id, operation_name="evaluate_and_reserve")

        now = self._get_now()
        decision_id = f"qdec_{uuid.uuid4().hex[:16]}"
        correlation_id = request.correlation_id or f"qcorr_{uuid.uuid4().hex[:12]}"

        with self._lock:
            # Idempotencia: Verificar si ya existe una reserva activa con este correlation_id
            if request.correlation_id:
                existing_res = self.reservation_repository.get_by_correlation_id(request.tenant_id, request.correlation_id)
                if existing_res and existing_res.status == QuotaReservationStatus.RESERVED and not existing_res.is_expired(now):
                    return QuotaDecision(
                        decision_id=decision_id,
                        status=QuotaStatus.ALLOW,
                        tenant_id=request.tenant_id,
                        identity_id=request.identity_id,
                        policy_id="IDEMPOTENT_REPLAY",
                        policy_version="1.0.0",
                        reason_codes=("IDEMPOTENT_REPLAY_ACTIVE_RESERVATION",),
                        rule_evaluations=(),
                        evaluated_at=now,
                        correlation_id=correlation_id,
                        reservation_id=existing_res.reservation_id,
                        rationale="Idempotent replay: active reservation already held for this correlation_id.",
                    )

            # 1. Obtener política de cuotas del tenant
            policy = self.policy_repository.get_policy(request.tenant_id)
            if policy is None:
                if self.fail_closed_on_missing_policy:
                    self._emit_audit(
                        record_type=AuditRecordType.LIMIT_EXCEEDED,
                        tenant_id=request.tenant_id,
                        actor_id=request.identity_id,
                        action="QUOTA_EVALUATION",
                        status="UNKNOWN_DENY",
                        details={"reason": "MISSING_POLICY_FAIL_CLOSED"},
                        correlation_id=correlation_id,
                    )
                    return QuotaDecision(
                        decision_id=decision_id,
                        status=QuotaStatus.UNKNOWN,
                        tenant_id=request.tenant_id,
                        identity_id=request.identity_id,
                        policy_id=None,
                        policy_version=None,
                        reason_codes=("MISSING_POLICY_FAIL_CLOSED",),
                        rule_evaluations=(),
                        evaluated_at=now,
                        correlation_id=correlation_id,
                        rationale=f"No quota policy configured for tenant {request.tenant_id} (fail-closed).",
                    )
                else:
                    # Explícito ALLOW si no se exige fail-closed
                    return QuotaDecision(
                        decision_id=decision_id,
                        status=QuotaStatus.ALLOW,
                        tenant_id=request.tenant_id,
                        identity_id=request.identity_id,
                        policy_id="UNCONFIGURED",
                        policy_version="1.0.0",
                        reason_codes=("POLICY_UNCONFIGURED_ALLOW",),
                        rule_evaluations=(),
                        evaluated_at=now,
                        correlation_id=correlation_id,
                        rationale="No quota policy configured, allowed by configuration.",
                    )

            # Si la política es explícitamente ilimitada
            if policy.is_unlimited:
                return QuotaDecision(
                    decision_id=decision_id,
                    status=QuotaStatus.ALLOW,
                    tenant_id=request.tenant_id,
                    identity_id=request.identity_id,
                    policy_id=policy.policy_id,
                    policy_version=policy.policy_version,
                    reason_codes=("EXPLICIT_UNLIMITED_POLICY",),
                    rule_evaluations=(),
                    evaluated_at=now,
                    correlation_id=correlation_id,
                    rationale="Explicit unlimited quota policy configured for tenant.",
                )

            # 2. Evaluar reglas aplicables
            rule_evaluations: List[RuleEvaluationDetail] = []
            blocking_reason_codes: List[str] = []
            final_status = QuotaStatus.ALLOW

            # Obtener reservas en vuelo activas para el tenant
            active_reservations = self.reservation_repository.list_active_reservations(
                tenant_id=request.tenant_id,
                current_time=now,
            )

            for rule in policy.rules:
                # Filtrar regla según scope
                if not self._is_rule_applicable(rule, request):
                    continue

                try:
                    eval_detail, is_rule_passed, reason = self._evaluate_single_rule(
                        rule=rule,
                        request=request,
                        active_reservations=active_reservations,
                        current_time=now,
                    )
                except Exception as e:
                    # Fail-safe on usage metering failure: UNKNOWN / DENY
                    eval_detail = RuleEvaluationDetail(
                        rule_id=rule.rule_id,
                        quota_type=rule.quota_type,
                        scope=rule.scope,
                        limit_value=rule.limit_value,
                        current_usage=0,
                        reserved_inflight=0,
                        requested_estimate=0,
                        remaining_capacity=0,
                        window=QuotaWindow.from_clock(rule.window_type, now, rule.custom_window_seconds),
                        is_passed=False,
                        reason_code="METERING_OUTAGE_FAIL_SAFE",
                    )
                    is_rule_passed = False
                    reason = "METERING_OUTAGE_FAIL_SAFE"

                rule_evaluations.append(eval_detail)

                if not is_rule_passed:
                    blocking_reason_codes.append(reason)
                    if reason == "METERING_OUTAGE_FAIL_SAFE":
                        final_status = QuotaStatus.UNKNOWN
                    elif rule.quota_type in (QuotaType.REQUESTS_PER_MINUTE, QuotaType.REQUESTS_PER_HOUR):
                        final_status = QuotaStatus.RATE_LIMITED
                    elif final_status not in (QuotaStatus.RATE_LIMITED, QuotaStatus.UNKNOWN):
                        final_status = QuotaStatus.LIMIT_REACHED

            # 3. Si ALLOW, generar y persistir QuotaReservation
            reservation_id: Optional[str] = None
            if final_status == QuotaStatus.ALLOW:
                res_id = f"qres_{uuid.uuid4().hex[:16]}"
                est_reqs = 1
                est_in_toks = request.estimated_input_tokens or 0
                est_out_toks = request.estimated_output_tokens or 0
                est_tot_toks = request.estimated_total_tokens or (est_in_toks + est_out_toks)
                est_cost = request.estimated_cost or Decimal("0.00")

                reservation = QuotaReservation(
                    reservation_id=res_id,
                    tenant_id=request.tenant_id,
                    identity_id=request.identity_id,
                    model_id=request.model_id,
                    provider=request.provider,
                    estimated_requests=est_reqs,
                    estimated_input_tokens=est_in_toks,
                    estimated_output_tokens=est_out_toks,
                    estimated_total_tokens=est_tot_toks,
                    estimated_cost=est_cost,
                    created_at=now,
                    expires_at=now + timedelta(seconds=self.default_reservation_ttl_seconds),
                    status=QuotaReservationStatus.RESERVED,
                    correlation_id=correlation_id,
                    source_decision_id=decision_id,
                )
                self.reservation_repository.save_reservation(reservation)
                reservation_id = res_id

                self._emit_audit(
                    record_type=AuditRecordType.ACTION_EXECUTED,
                    tenant_id=request.tenant_id,
                    actor_id=request.identity_id,
                    action="QUOTA_RESERVED",
                    status="ALLOW",
                    details={
                        "reservation_id": reservation_id,
                        "estimated_tokens": est_tot_toks,
                        "estimated_cost": str(est_cost),
                    },
                    correlation_id=correlation_id,
                )
            else:
                self._emit_audit(
                    record_type=AuditRecordType.LIMIT_EXCEEDED,
                    tenant_id=request.tenant_id,
                    actor_id=request.identity_id,
                    action="QUOTA_BLOCKED",
                    status=final_status.value,
                    details={
                        "reason_codes": blocking_reason_codes,
                    },
                    correlation_id=correlation_id,
                )

            return QuotaDecision(
                decision_id=decision_id,
                status=final_status,
                tenant_id=request.tenant_id,
                identity_id=request.identity_id,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                reason_codes=tuple(blocking_reason_codes) if blocking_reason_codes else ("QUOTA_ALLOW",),
                rule_evaluations=tuple(rule_evaluations),
                evaluated_at=now,
                correlation_id=correlation_id,
                reservation_id=reservation_id,
                rationale="Quota evaluated successfully." if final_status == QuotaStatus.ALLOW else f"Quota exceeded: {', '.join(blocking_reason_codes)}",
            )

    def check_quota_only(
        self,
        request: QuotaRequest,
        context: Optional[TenantContext] = None,
    ) -> QuotaDecision:
        """
        Evalúa las cuotas aplicables en modo sólo lectura (sin crear QuotaReservation).
        """
        if context is not None:
            CrossTenantGuard.ensure_tenant_context(context)
            CrossTenantGuard.assert_same_tenant(context, request.tenant_id, operation_name="check_quota_only")

        now = self._get_now()
        decision_id = f"qdec_ro_{uuid.uuid4().hex[:12]}"
        correlation_id = request.correlation_id or f"qcorr_{uuid.uuid4().hex[:12]}"

        policy = self.policy_repository.get_policy(request.tenant_id)
        if policy is None:
            if self.fail_closed_on_missing_policy:
                return QuotaDecision(
                    decision_id=decision_id,
                    status=QuotaStatus.UNKNOWN,
                    tenant_id=request.tenant_id,
                    identity_id=request.identity_id,
                    policy_id=None,
                    policy_version=None,
                    reason_codes=("MISSING_POLICY_FAIL_CLOSED",),
                    rule_evaluations=(),
                    evaluated_at=now,
                    correlation_id=correlation_id,
                    rationale=f"No quota policy configured for tenant {request.tenant_id}.",
                )
            else:
                return QuotaDecision(
                    decision_id=decision_id,
                    status=QuotaStatus.ALLOW,
                    tenant_id=request.tenant_id,
                    identity_id=request.identity_id,
                    policy_id="UNCONFIGURED",
                    policy_version="1.0.0",
                    reason_codes=("POLICY_UNCONFIGURED_ALLOW",),
                    rule_evaluations=(),
                    evaluated_at=now,
                    correlation_id=correlation_id,
                    rationale="No quota policy configured, allowed by configuration.",
                )

        if policy.is_unlimited:
            return QuotaDecision(
                decision_id=decision_id,
                status=QuotaStatus.ALLOW,
                tenant_id=request.tenant_id,
                identity_id=request.identity_id,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                reason_codes=("EXPLICIT_UNLIMITED_POLICY",),
                rule_evaluations=(),
                evaluated_at=now,
                correlation_id=correlation_id,
                rationale="Explicit unlimited quota policy configured for tenant.",
            )

        rule_evaluations: List[RuleEvaluationDetail] = []
        blocking_reason_codes: List[str] = []
        final_status = QuotaStatus.ALLOW

        active_reservations = self.reservation_repository.list_active_reservations(
            tenant_id=request.tenant_id,
            current_time=now,
        )

        for rule in policy.rules:
            if not self._is_rule_applicable(rule, request):
                continue

            eval_detail, is_rule_passed, reason = self._evaluate_single_rule(
                rule=rule,
                request=request,
                active_reservations=active_reservations,
                current_time=now,
            )
            rule_evaluations.append(eval_detail)

            if not is_rule_passed:
                blocking_reason_codes.append(reason)
                if rule.quota_type in (QuotaType.REQUESTS_PER_MINUTE, QuotaType.REQUESTS_PER_HOUR):
                    final_status = QuotaStatus.RATE_LIMITED
                elif final_status != QuotaStatus.RATE_LIMITED:
                    final_status = QuotaStatus.LIMIT_REACHED

        return QuotaDecision(
            decision_id=decision_id,
            status=final_status,
            tenant_id=request.tenant_id,
            identity_id=request.identity_id,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            reason_codes=tuple(blocking_reason_codes) if blocking_reason_codes else ("QUOTA_ALLOW",),
            rule_evaluations=tuple(rule_evaluations),
            evaluated_at=now,
            correlation_id=correlation_id,
            reservation_id=None,
            rationale="Quota evaluated (read-only)." if final_status == QuotaStatus.ALLOW else f"Quota exceeded: {', '.join(blocking_reason_codes)}",
        )

    def reconcile_reservation(
        self,
        reservation_id: str,
        tenant_id: str,
        actual_status: QuotaReservationStatus,
        actual_tokens: Optional[int] = None,
        actual_cost: Optional[Any] = None,
    ) -> Optional[QuotaReservation]:
        """
        Reconcilia o libera una reserva una vez concluida la inferencia o fallado el pipeline.
        """
        now = self._get_now()
        with self._lock:
            res = self.reservation_repository.get_reservation(reservation_id)
            if res is None:
                return None
            if res.tenant_id != tenant_id:
                raise CrossTenantAccessError(f"Cross-tenant reservation access attempt: {res.tenant_id} != {tenant_id}")

            actual_cost_dec = Decimal(str(actual_cost)) if actual_cost is not None else None
            updated_res = res.with_status(
                new_status=actual_status,
                reconciled_at=now,
                actual_tokens=actual_tokens,
                actual_cost=actual_cost_dec,
            )
            self.reservation_repository.save_reservation(updated_res)

            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=tenant_id,
                actor_id=res.identity_id,
                action="QUOTA_RECONCILED",
                status=actual_status.value,
                details={
                    "reservation_id": reservation_id,
                    "final_status": actual_status.value,
                    "actual_tokens": actual_tokens,
                    "actual_cost": str(actual_cost) if actual_cost is not None else None,
                },
                correlation_id=res.correlation_id,
            )
            return updated_res

    def _is_rule_applicable(self, rule: QuotaRule, request: QuotaRequest) -> bool:
        """Determina si una regla de cuota aplica a la petición en curso."""
        if rule.scope == QuotaScope.TENANT:
            return True
        if rule.scope == QuotaScope.USER:
            if not request.identity_id:
                # Si la regla es a nivel de usuario y la petición no tiene identidad,
                # aplica si no hay target_identifier específico o si coincide con anonymous
                return rule.target_identifier is None
            if rule.target_identifier:
                return rule.target_identifier == request.identity_id
            return True
        if rule.scope == QuotaScope.MODEL:
            if not request.model_id:
                return True
            if rule.target_identifier:
                return rule.target_identifier == request.model_id
            return True
        if rule.scope == QuotaScope.PROVIDER:
            if not request.provider:
                return True
            if rule.target_identifier:
                return rule.target_identifier == request.provider
            return True
        return False

    def _evaluate_single_rule(
        self,
        rule: QuotaRule,
        request: QuotaRequest,
        active_reservations: List[QuotaReservation],
        current_time: datetime,
    ) -> Tuple[RuleEvaluationDetail, bool, str]:
        """
        Evalúa una regla de cuota individual calculando consumo histórico (O.6) + en vuelo (O.7).
        """
        # 1. Determinar ventana temporal UTC determinista
        window = QuotaWindow.from_clock(
            window_type=rule.window_type,
            now=current_time,
            custom_duration_seconds=rule.custom_window_seconds,
        )

        # 2. Consultar uso histórico a O.6 Usage Metering
        # Crear contexto de tenant para O.6
        t_ctx = TenantContext(tenant_id=request.tenant_id)
        u_period = UsagePeriod(
            start_time=window.start_time,
            end_time=window.end_time,
            period_type=UsagePeriodType(rule.window_type.value) if rule.window_type.value in ("HOUR", "DAY", "MONTH") else UsagePeriodType.CUSTOM,
        )
        u_query = UsageQuery(
            tenant_id=request.tenant_id,
            identity_id=request.identity_id if rule.scope == QuotaScope.USER else None,
            model=request.model_id if rule.scope == QuotaScope.MODEL else None,
            provider=request.provider if rule.scope == QuotaScope.PROVIDER else None,
            period=u_period,
        )
        usage_agg: UsageAggregate = self.usage_metering_service.aggregate_usage(t_ctx, u_query)

        # 3. Filtrar reservas activas en vuelo que caigan en el scope y ventana de la regla
        relevant_reservations = [
            r for r in active_reservations
            if window.contains(r.created_at) and self._is_reservation_in_scope(r, rule, request)
        ]

        # 4. Calcular métricas actuales según el tipo de cuota
        if rule.quota_type in (QuotaType.MAX_REQUESTS, QuotaType.REQUESTS_PER_MINUTE, QuotaType.REQUESTS_PER_HOUR):
            hist_usage = usage_agg.total_requests
            inflight_usage = sum(r.estimated_requests for r in relevant_reservations)
            requested_est = 1
            limit = int(rule.limit_value)

            total_projected = hist_usage + inflight_usage + requested_est
            remaining = max(0, limit - (hist_usage + inflight_usage))
            passed = (total_projected <= limit) if rule.is_hard_limit else True
            reason = "REQUEST_LIMIT_EXCEEDED" if not passed else "OK"

            detail = RuleEvaluationDetail(
                rule_id=rule.rule_id,
                quota_type=rule.quota_type,
                scope=rule.scope,
                limit_value=limit,
                current_usage=hist_usage,
                reserved_inflight=inflight_usage,
                requested_estimate=requested_est,
                remaining_capacity=remaining,
                window=window,
                is_passed=passed,
                reason_code=reason if not passed else None,
            )
            return detail, passed, reason

        elif rule.quota_type == QuotaType.MAX_INPUT_TOKENS:
            hist_usage = usage_agg.total_input_tokens
            inflight_usage = sum(r.estimated_input_tokens for r in relevant_reservations)
            # Manejo de cache hit predictivo
            if (request.is_cache_hit_predicted or request.is_cache_hit) and rule.allow_cache_hit_bypass_token_budget:
                requested_est = 0
            else:
                requested_est = request.estimated_input_tokens if request.estimated_input_tokens is not None else 0

            limit = int(rule.limit_value)
            total_projected = hist_usage + inflight_usage + requested_est
            remaining = max(0, limit - (hist_usage + inflight_usage))
            passed = (total_projected <= limit) if rule.is_hard_limit else True
            reason = "INPUT_TOKEN_LIMIT_EXCEEDED" if not passed else "OK"

            detail = RuleEvaluationDetail(
                rule_id=rule.rule_id,
                quota_type=rule.quota_type,
                scope=rule.scope,
                limit_value=limit,
                current_usage=hist_usage,
                reserved_inflight=inflight_usage,
                requested_estimate=requested_est,
                remaining_capacity=remaining,
                window=window,
                is_passed=passed,
                reason_code=reason if not passed else None,
            )
            return detail, passed, reason

        elif rule.quota_type == QuotaType.MAX_OUTPUT_TOKENS:
            hist_usage = usage_agg.total_output_tokens
            inflight_usage = sum(r.estimated_output_tokens for r in relevant_reservations)
            requested_est = request.estimated_output_tokens if request.estimated_output_tokens is not None else 0
            limit = int(rule.limit_value)
            total_projected = hist_usage + inflight_usage + requested_est
            remaining = max(0, limit - (hist_usage + inflight_usage))
            passed = (total_projected <= limit) if rule.is_hard_limit else True
            reason = "OUTPUT_TOKEN_LIMIT_EXCEEDED" if not passed else "OK"

            detail = RuleEvaluationDetail(
                rule_id=rule.rule_id,
                quota_type=rule.quota_type,
                scope=rule.scope,
                limit_value=limit,
                current_usage=hist_usage,
                reserved_inflight=inflight_usage,
                requested_estimate=requested_est,
                remaining_capacity=remaining,
                window=window,
                is_passed=passed,
                reason_code=reason if not passed else None,
            )
            return detail, passed, reason

        elif rule.quota_type in (QuotaType.MAX_TOTAL_TOKENS, QuotaType.TOKENS_PER_PERIOD):
            hist_usage = usage_agg.total_tokens
            inflight_usage = sum(r.estimated_total_tokens for r in relevant_reservations)
            if (request.is_cache_hit_predicted or request.is_cache_hit) and rule.allow_cache_hit_bypass_token_budget:
                requested_est = 0
            else:
                if request.estimated_total_tokens is not None:
                    requested_est = request.estimated_total_tokens
                elif request.estimated_input_tokens is not None or request.estimated_output_tokens is not None:
                    requested_est = (request.estimated_input_tokens or 0) + (request.estimated_output_tokens or 0)
                else:
                    requested_est = 0

            limit = int(rule.limit_value)
            total_projected = hist_usage + inflight_usage + requested_est
            remaining = max(0, limit - (hist_usage + inflight_usage))
            passed = (total_projected <= limit) if rule.is_hard_limit else True
            reason = "TOTAL_TOKEN_LIMIT_EXCEEDED" if not passed else "OK"

            detail = RuleEvaluationDetail(
                rule_id=rule.rule_id,
                quota_type=rule.quota_type,
                scope=rule.scope,
                limit_value=limit,
                current_usage=hist_usage,
                reserved_inflight=inflight_usage,
                requested_estimate=requested_est,
                remaining_capacity=remaining,
                window=window,
                is_passed=passed,
                reason_code=reason if not passed else None,
            )
            return detail, passed, reason

        elif rule.quota_type == QuotaType.MAX_COST:
            # Reusar actual_cost si existe, o estimated_cost para facts históricos
            hist_usage: Decimal = usage_agg.total_actual_cost or usage_agg.total_estimated_cost
            inflight_usage: Decimal = sum((r.estimated_cost for r in relevant_reservations), Decimal("0.00"))

            if (request.is_cache_hit_predicted or request.is_cache_hit) and rule.allow_cache_hit_bypass_token_budget:
                requested_est = Decimal("0.00")
            else:
                requested_est = request.estimated_cost or Decimal("0.00")

            limit: Decimal = Decimal(str(rule.limit_value))
            total_projected = hist_usage + inflight_usage + requested_est
            remaining = max(Decimal("0.00"), limit - (hist_usage + inflight_usage))
            passed = (total_projected <= limit) if rule.is_hard_limit else True
            reason = "COST_BUDGET_EXCEEDED" if not passed else "OK"

            detail = RuleEvaluationDetail(
                rule_id=rule.rule_id,
                quota_type=rule.quota_type,
                scope=rule.scope,
                limit_value=limit,
                current_usage=hist_usage,
                reserved_inflight=inflight_usage,
                requested_estimate=requested_est,
                remaining_capacity=remaining,
                window=window,
                is_passed=passed,
                reason_code=reason if not passed else None,
            )
            return detail, passed, reason

        else:
            raise ValueError(f"Unsupported QuotaType: {rule.quota_type}")

    def _is_reservation_in_scope(self, res: QuotaReservation, rule: QuotaRule, req: QuotaRequest) -> bool:
        if rule.scope == QuotaScope.TENANT:
            return True
        if rule.scope == QuotaScope.USER:
            if rule.target_identifier:
                return res.identity_id == rule.target_identifier
            return res.identity_id == req.identity_id
        if rule.scope == QuotaScope.MODEL:
            if rule.target_identifier:
                return res.model_id == rule.target_identifier
            return res.model_id == req.model_id
        if rule.scope == QuotaScope.PROVIDER:
            if rule.target_identifier:
                return res.provider == rule.target_identifier
            return res.provider == req.provider
        return True
