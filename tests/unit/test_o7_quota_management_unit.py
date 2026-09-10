"""
Pruebas Unitarias para O.7 — Quota Management
(Tenant / User AI Budgets & Rate Limiting)

Requisitos mínimos cubiertos:
1. tenant quota allow
2. tenant quota exceeded
3. user quota exceeded
4. tenant/user combined rules
5. requests-per-minute
6. token limit
7. cost limit Decimal
8. missing policy != unlimited
9. UNKNOWN usage != zero
10. cross-tenant isolation
11. cache-hit policy
12. reservation lifecycle
13. concurrent reservation safety
14. duplicate reservation idempotency
15. explicit unlimited policy
16. no O.8 plan logic
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import threading
import pytest

from src.domain.quota_management.models import (
    QuotaPolicy,
    QuotaRule,
    QuotaScope,
    QuotaType,
    QuotaWindowType,
    QuotaStatus,
    QuotaRequest,
    QuotaDecision,
    QuotaReservation,
    QuotaReservationStatus,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
    InMemoryQuotaReservationRepository,
)
from src.application.quota_management.quota_management_service import (
    QuotaManagementService,
)
from src.domain.usage_metering.models import (
    UsageAggregate,
    DimensionUsageSummary,
    UsageEvent,
    UsageRequestStatus,
    UsagePeriod,
)
from src.domain.usage_metering.ports import UsageMeteringServicePort
from src.domain.reliability.ports import ClockPort
from src.domain.tenant.models import TenantContext


class MockClock(ClockPort):
    def __init__(self, initial_time: datetime):
        self._current_time = initial_time

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def set_time(self, new_time: datetime) -> None:
        self._current_time = new_time

    def advance(self, duration: timedelta) -> None:
        self._current_time += duration


class MockUsageMeteringService(UsageMeteringServicePort):
    def __init__(self, aggregates=None, events=None, raise_error=False):
        self.aggregates = aggregates or {}
        self.events = events or []
        self.raise_error = raise_error
        self.record_called_with = []

    def record_usage_event(self, context, event: UsageEvent) -> UsageEvent:
        self.record_called_with.append(event)
        self.events.append(event)
        return event

    def get_event(self, context, usage_event_id: str):
        for ev in self.events:
            if ev.usage_event_id == usage_event_id:
                return ev
        return None

    def aggregate_usage(self, context, query) -> UsageAggregate:
        if self.raise_error:
            raise RuntimeError("Usage database unreachable")

        # Generar clave de lookup
        key = (query.tenant_id, query.identity_id)
        if key in self.aggregates:
            return self.aggregates[key]

        tenant_key = (query.tenant_id, None)
        if tenant_key in self.aggregates and query.identity_id is None:
            return self.aggregates[tenant_key]

        return UsageAggregate(
            tenant_id=query.tenant_id,
            period=UsagePeriod(
                start_time=query.period.start_time if query.period else datetime(2026, 1, 1, tzinfo=timezone.utc),
                end_time=query.period.end_time if query.period else datetime(2026, 1, 2, tzinfo=timezone.utc),
            ),
            total_requests=0,
            successful_requests=0,
            failed_requests=0,
            cached_requests=0,
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            unknown_token_events_count=0,
            total_estimated_cost=Decimal("0.00"),
            total_actual_cost=Decimal("0.00"),
            unknown_actual_cost_events_count=0,
        )


@pytest.fixture
def mock_clock():
    return MockClock(datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def policy_repo():
    return InMemoryQuotaPolicyRepository()


@pytest.fixture
def reservation_repo():
    return InMemoryQuotaReservationRepository()


@pytest.fixture
def usage_service():
    return MockUsageMeteringService()


@pytest.fixture
def quota_service(policy_repo, reservation_repo, usage_service, mock_clock):
    return QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=reservation_repo,
        usage_metering_service=usage_service,
        clock=mock_clock,
    )


# ----------------------------------------------------------------------
# 1. Tenant Quota Allow
# ----------------------------------------------------------------------
def test_tenant_quota_allow(quota_service, policy_repo, mock_clock):
    tenant_id = "tenant_alpha"
    rule = QuotaRule(
        rule_id="r1",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=100000,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.DAY,
    )
    policy = QuotaPolicy(
        policy_id="p1",
        tenant_id=tenant_id,
        rules=(rule,),
    )
    policy_repo.save_policy(policy)

    req = QuotaRequest(
        tenant_id=tenant_id,
        estimated_total_tokens=1500,
        estimated_cost=Decimal("0.05"),
        correlation_id="corr_1",
    )
    decision = quota_service.evaluate_and_reserve(req)

    assert decision.is_allowed is True
    assert decision.status == QuotaStatus.ALLOW
    assert decision.reservation_id is not None
    # Límite 100000, consumo previo 0, capacidad restante antes de reserva = 100000
    assert decision.remaining_capacity["MAX_TOTAL_TOKENS"] == 100000


# ----------------------------------------------------------------------
# 2. Tenant Quota Exceeded
# ----------------------------------------------------------------------
def test_tenant_quota_exceeded(quota_service, policy_repo, usage_service, mock_clock):
    tenant_id = "tenant_alpha"
    rule = QuotaRule(
        rule_id="r1",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=10000,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.DAY,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule,)))

    # Histórico de O.6: 9500 tokens consumidos
    usage_service.aggregates[(tenant_id, None)] = UsageAggregate(
        tenant_id=tenant_id,
        period=UsagePeriod(
            start_time=mock_clock.now() - timedelta(hours=1),
            end_time=mock_clock.now() + timedelta(hours=1),
        ),
        total_requests=10,
        successful_requests=10,
        failed_requests=0,
        cached_requests=0,
        total_input_tokens=5000,
        total_output_tokens=4500,
        total_tokens=9500,
        unknown_token_events_count=0,
        total_estimated_cost=Decimal("0.50"),
        total_actual_cost=Decimal("0.50"),
        unknown_actual_cost_events_count=0,
    )

    # Intento de 1000 tokens (9500 + 1000 = 10500 > 10000)
    req = QuotaRequest(
        tenant_id=tenant_id,
        estimated_total_tokens=1000,
        correlation_id="corr_2",
    )
    decision = quota_service.evaluate_and_reserve(req)

    assert decision.is_allowed is False
    assert decision.status == QuotaStatus.LIMIT_REACHED
    assert decision.reservation_id is None
    assert any("EXCEEDED" in rc for rc in decision.reason_codes)


# ----------------------------------------------------------------------
# 3. User Quota Exceeded
# ----------------------------------------------------------------------
def test_user_quota_exceeded(quota_service, policy_repo, usage_service, mock_clock):
    tenant_id = "tenant_alpha"
    user_id = "user_42"
    rule_user = QuotaRule(
        rule_id="r_user",
        quota_type=QuotaType.MAX_REQUESTS,
        limit_value=5,
        scope=QuotaScope.USER,
        window_type=QuotaWindowType.DAY,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule_user,)))

    # Histórico del usuario: 5 requests
    usage_service.aggregates[(tenant_id, user_id)] = UsageAggregate(
        tenant_id=tenant_id,
        period=UsagePeriod(
            start_time=mock_clock.now() - timedelta(hours=1),
            end_time=mock_clock.now() + timedelta(hours=1),
        ),
        total_requests=5,
        successful_requests=5,
        failed_requests=0,
        cached_requests=0,
        total_input_tokens=100,
        total_output_tokens=100,
        total_tokens=200,
        unknown_token_events_count=0,
        total_estimated_cost=Decimal("0.01"),
        total_actual_cost=Decimal("0.01"),
        unknown_actual_cost_events_count=0,
    )

    req = QuotaRequest(
        tenant_id=tenant_id,
        identity_id=user_id,
        estimated_total_tokens=50,
        correlation_id="corr_3",
    )
    decision = quota_service.evaluate_and_reserve(req)

    assert decision.is_allowed is False
    assert decision.status == QuotaStatus.LIMIT_REACHED
    assert any("EXCEEDED" in rc for rc in decision.reason_codes)


# ----------------------------------------------------------------------
# 4. Tenant/User Combined Rules (Hierarchical Enforcement)
# ----------------------------------------------------------------------
def test_tenant_user_combined_rules(quota_service, policy_repo, usage_service, mock_clock):
    tenant_id = "tenant_corp"
    user_id = "user_alice"

    # Tenant limit: $100 / day; User limit: $20 / day
    rule_tenant = QuotaRule(
        rule_id="r_t",
        quota_type=QuotaType.MAX_COST,
        limit_value=Decimal("100.00"),
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.DAY,
    )
    rule_user = QuotaRule(
        rule_id="r_u",
        quota_type=QuotaType.MAX_COST,
        limit_value=Decimal("20.00"),
        scope=QuotaScope.USER,
        window_type=QuotaWindowType.DAY,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule_tenant, rule_user)))

    # Tenant consumed $50; User consumed $19
    usage_service.aggregates[(tenant_id, None)] = UsageAggregate(
        tenant_id=tenant_id,
        period=UsagePeriod(
            start_time=mock_clock.now() - timedelta(hours=1),
            end_time=mock_clock.now() + timedelta(hours=1),
        ),
        total_requests=10, successful_requests=10, failed_requests=0, cached_requests=0,
        total_input_tokens=0, total_output_tokens=0, total_tokens=0, unknown_token_events_count=0,
        total_estimated_cost=Decimal("50.00"), total_actual_cost=Decimal("50.00"), unknown_actual_cost_events_count=0,
    )
    usage_service.aggregates[(tenant_id, user_id)] = UsageAggregate(
        tenant_id=tenant_id,
        period=UsagePeriod(
            start_time=mock_clock.now() - timedelta(hours=1),
            end_time=mock_clock.now() + timedelta(hours=1),
        ),
        total_requests=5, successful_requests=5, failed_requests=0, cached_requests=0,
        total_input_tokens=0, total_output_tokens=0, total_tokens=0, unknown_token_events_count=0,
        total_estimated_cost=Decimal("19.00"), total_actual_cost=Decimal("19.00"), unknown_actual_cost_events_count=0,
    )

    # Request estimate $2.00 -> Tenant OK ($52 < $100), but User Exceeded ($21 > $20)
    req = QuotaRequest(
        tenant_id=tenant_id,
        identity_id=user_id,
        estimated_cost=Decimal("2.00"),
        correlation_id="corr_4",
    )
    decision = quota_service.evaluate_and_reserve(req)

    assert decision.is_allowed is False
    assert decision.status == QuotaStatus.LIMIT_REACHED
    assert any("EXCEEDED" in rc for rc in decision.reason_codes)


# ----------------------------------------------------------------------
# 5. Requests-per-minute Rate Limiting
# ----------------------------------------------------------------------
def test_requests_per_minute_rate_limiting(quota_service, policy_repo, usage_service, mock_clock):
    tenant_id = "tenant_fast"
    rule_rpm = QuotaRule(
        rule_id="r_rpm",
        quota_type=QuotaType.REQUESTS_PER_MINUTE,
        limit_value=3,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.MINUTE,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule_rpm,)))

    # Histórico de O.6: 3 requests en el último minuto
    usage_service.aggregates[(tenant_id, None)] = UsageAggregate(
        tenant_id=tenant_id,
        period=UsagePeriod(
            start_time=mock_clock.now() - timedelta(seconds=30),
            end_time=mock_clock.now() + timedelta(seconds=30),
        ),
        total_requests=3,
        successful_requests=3,
        failed_requests=0,
        cached_requests=0,
        total_input_tokens=100,
        total_output_tokens=100,
        total_tokens=200,
        unknown_token_events_count=0,
        total_estimated_cost=Decimal("0.01"),
        total_actual_cost=Decimal("0.01"),
        unknown_actual_cost_events_count=0,
    )

    req = QuotaRequest(tenant_id=tenant_id, correlation_id="corr_5")
    decision = quota_service.evaluate_and_reserve(req)

    assert decision.is_allowed is False
    assert decision.status == QuotaStatus.RATE_LIMITED
    assert "REQUEST_LIMIT_EXCEEDED" in decision.reason_codes


# ----------------------------------------------------------------------
# 6. Token Limit (Input, Output, Total)
# ----------------------------------------------------------------------
def test_token_limits(quota_service, policy_repo, usage_service, mock_clock):
    tenant_id = "tenant_tokens"
    rule_in = QuotaRule(
        rule_id="r_in",
        quota_type=QuotaType.MAX_INPUT_TOKENS,
        limit_value=1000,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.HOUR,
    )
    rule_out = QuotaRule(
        rule_id="r_out",
        quota_type=QuotaType.MAX_OUTPUT_TOKENS,
        limit_value=500,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.HOUR,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule_in, rule_out)))

    # 1. Petición dentro de límites
    req1 = QuotaRequest(
        tenant_id=tenant_id,
        estimated_input_tokens=800,
        estimated_output_tokens=400,
        estimated_total_tokens=1200,
        correlation_id="corr_6_1",
    )
    dec1 = quota_service.evaluate_and_reserve(req1)
    assert dec1.is_allowed is True

    # 2. Petición que excede output tokens
    req2 = QuotaRequest(
        tenant_id=tenant_id,
        estimated_input_tokens=100,
        estimated_output_tokens=600,  # 600 > 500
        estimated_total_tokens=700,
        correlation_id="corr_6_2",
    )
    dec2 = quota_service.evaluate_and_reserve(req2)
    assert dec2.is_allowed is False
    assert dec2.status == QuotaStatus.LIMIT_REACHED


# ----------------------------------------------------------------------
# 7. Cost Limit with High-Precision Decimal
# ----------------------------------------------------------------------
def test_cost_limit_decimal(quota_service, policy_repo, usage_service, mock_clock):
    tenant_id = "tenant_fin"
    rule_cost = QuotaRule(
        rule_id="r_cst",
        quota_type=QuotaType.MAX_COST,
        limit_value=Decimal("12.5050"),
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.DAY,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule_cost,)))

    usage_service.aggregates[(tenant_id, None)] = UsageAggregate(
        tenant_id=tenant_id,
        period=UsagePeriod(
            start_time=mock_clock.now() - timedelta(hours=1),
            end_time=mock_clock.now() + timedelta(hours=1),
        ),
        total_requests=1, successful_requests=1, failed_requests=0, cached_requests=0,
        total_input_tokens=0, total_output_tokens=0, total_tokens=0, unknown_token_events_count=0,
        total_estimated_cost=Decimal("12.5000"), total_actual_cost=Decimal("12.5000"), unknown_actual_cost_events_count=0,
    )

    # Margen disponible = 0.0050
    # Request por 0.0040 -> ALLOW
    req_ok = QuotaRequest(tenant_id=tenant_id, estimated_cost=Decimal("0.0040"), correlation_id="c_ok")
    dec_ok = quota_service.check_quota_only(req_ok)
    assert dec_ok.is_allowed is True

    # Request por 0.0060 -> BLOCK
    req_block = QuotaRequest(tenant_id=tenant_id, estimated_cost=Decimal("0.0060"), correlation_id="c_block")
    dec_block = quota_service.check_quota_only(req_block)
    assert dec_block.is_allowed is False


# ----------------------------------------------------------------------
# 8. Missing Policy != Unlimited (Fail-Safe / Default Deny)
# ----------------------------------------------------------------------
def test_missing_policy_default_deny(quota_service):
    tenant_id = "tenant_unknown"
    req = QuotaRequest(tenant_id=tenant_id, correlation_id="corr_8")

    decision = quota_service.evaluate_and_reserve(req)

    assert decision.is_allowed is False
    assert decision.status == QuotaStatus.UNKNOWN
    assert any("MISSING_POLICY" in rc for rc in decision.reason_codes)


# ----------------------------------------------------------------------
# 9. UNKNOWN Usage != Zero (Fail-Safe on Metering Outage)
# ----------------------------------------------------------------------
def test_unknown_usage_metering_error_fail_safe(policy_repo, reservation_repo, mock_clock):
    tenant_id = "tenant_err"
    rule = QuotaRule(
        rule_id="r1",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=10000,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.DAY,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule,)))

    # Metering service que lanza error
    broken_usage = MockUsageMeteringService(raise_error=True)
    svc = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=reservation_repo,
        usage_metering_service=broken_usage,
        clock=mock_clock,
    )

    req = QuotaRequest(tenant_id=tenant_id, estimated_total_tokens=500, correlation_id="corr_9")
    decision = svc.evaluate_and_reserve(req)

    assert decision.is_allowed is False
    assert decision.status == QuotaStatus.UNKNOWN
    assert "METERING_OUTAGE_FAIL_SAFE" in decision.reason_codes


# ----------------------------------------------------------------------
# 10. Cross-Tenant Isolation
# ----------------------------------------------------------------------
def test_cross_tenant_isolation(quota_service, policy_repo, reservation_repo, usage_service, mock_clock):
    tenant_a = "tenant_a"
    tenant_b = "tenant_b"

    # Tenant A tiene límite bajo y está agotado
    policy_repo.save_policy(QuotaPolicy(
        policy_id="p_a",
        tenant_id=tenant_a,
        rules=(QuotaRule(rule_id="r_a", quota_type=QuotaType.MAX_REQUESTS, limit_value=2, scope=QuotaScope.TENANT),),
    ))
    # Tenant B tiene límite holgado
    policy_repo.save_policy(QuotaPolicy(
        policy_id="p_b",
        tenant_id=tenant_b,
        rules=(QuotaRule(rule_id="r_b", quota_type=QuotaType.MAX_REQUESTS, limit_value=100, scope=QuotaScope.TENANT),),
    ))

    # Tenant A ya agotó sus 2 requests
    usage_service.aggregates[(tenant_a, None)] = UsageAggregate(
        tenant_id=tenant_a,
        period=UsagePeriod(
            start_time=mock_clock.now() - timedelta(hours=1),
            end_time=mock_clock.now() + timedelta(hours=1),
        ),
        total_requests=2, successful_requests=2, failed_requests=0, cached_requests=0,
        total_input_tokens=0, total_output_tokens=0, total_tokens=0, unknown_token_events_count=0,
        total_estimated_cost=Decimal("0.00"), total_actual_cost=Decimal("0.00"), unknown_actual_cost_events_count=0,
    )

    # Intentos
    dec_a = quota_service.evaluate_and_reserve(QuotaRequest(tenant_id=tenant_a, correlation_id="c_a"))
    dec_b = quota_service.evaluate_and_reserve(QuotaRequest(tenant_id=tenant_b, correlation_id="c_b"))

    assert dec_a.is_allowed is False
    assert dec_a.status == QuotaStatus.LIMIT_REACHED

    assert dec_b.is_allowed is True
    assert dec_b.status == QuotaStatus.ALLOW


# ----------------------------------------------------------------------
# 11. Cache-Hit Policy
# ----------------------------------------------------------------------
def test_cache_hit_policy_bypass(quota_service, policy_repo):
    tenant_id = "tenant_cache"
    rule = QuotaRule(
        rule_id="r_c",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=1000,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.DAY,
        allow_cache_hit_bypass_token_budget=True,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p_c", tenant_id=tenant_id, rules=(rule,)))

    # Request con flag is_cache_hit
    req = QuotaRequest(
        tenant_id=tenant_id,
        estimated_total_tokens=5000,  # Superaría el límite si no fuera cache hit
        is_cache_hit=True,
        correlation_id="c_hit",
    )
    decision = quota_service.evaluate_and_reserve(req)

    assert decision.is_allowed is True
    assert decision.status == QuotaStatus.ALLOW


# ----------------------------------------------------------------------
# 12. Reservation Lifecycle (RESERVED -> CONSUMED / RELEASED)
# ----------------------------------------------------------------------
def test_reservation_lifecycle(quota_service, policy_repo, reservation_repo):
    tenant_id = "tenant_life"
    rule = QuotaRule(rule_id="r1", quota_type=QuotaType.MAX_TOTAL_TOKENS, limit_value=5000, scope=QuotaScope.TENANT)
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule,)))

    # 1. Crear reserva
    req = QuotaRequest(tenant_id=tenant_id, estimated_total_tokens=1000, correlation_id="c_res")
    dec = quota_service.evaluate_and_reserve(req)
    res_id = dec.reservation_id
    assert res_id is not None

    res = reservation_repo.get_reservation(res_id)
    assert res.status == QuotaReservationStatus.RESERVED

    # 2. Reconciliar como CONSUMED
    updated = quota_service.reconcile_reservation(
        reservation_id=res_id,
        tenant_id=tenant_id,
        actual_status=QuotaReservationStatus.CONSUMED,
        actual_tokens=950,
        actual_cost=Decimal("0.02"),
    )
    assert updated.status == QuotaReservationStatus.CONSUMED
    assert updated.estimated_total_tokens == 950

    # 3. Liberar otra reserva como RELEASED
    req2 = QuotaRequest(tenant_id=tenant_id, estimated_total_tokens=500, correlation_id="c_res2")
    dec2 = quota_service.evaluate_and_reserve(req2)
    res_id2 = dec2.reservation_id
    released = quota_service.reconcile_reservation(
        reservation_id=res_id2,
        tenant_id=tenant_id,
        actual_status=QuotaReservationStatus.RELEASED,
    )
    assert released.status == QuotaReservationStatus.RELEASED


# ----------------------------------------------------------------------
# 13. Concurrent Reservation Safety (In-Flight Accounting / Anti-TOCTOU)
# ----------------------------------------------------------------------
def test_concurrent_reservation_anti_toctou(quota_service, policy_repo):
    tenant_id = "tenant_conc"
    # Límite de 3000 tokens en total
    rule = QuotaRule(rule_id="r1", quota_type=QuotaType.MAX_TOTAL_TOKENS, limit_value=3000, scope=QuotaScope.TENANT)
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule,)))

    # Ejecutar múltiples reservas concurrentes de 1000 tokens cada una
    results = []
    errors = []

    def make_request(idx):
        try:
            req = QuotaRequest(
                tenant_id=tenant_id,
                estimated_total_tokens=1000,
                correlation_id=f"corr_conc_{idx}",
            )
            dec = quota_service.evaluate_and_reserve(req)
            results.append(dec)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=make_request, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(results) == 6

    allowed = [r for r in results if r.is_allowed]
    blocked = [r for r in results if not r.is_allowed]

    # Exactamente 3 deben ser permitidas (3 * 1000 = 3000) y 3 bloqueadas
    assert len(allowed) == 3
    assert len(blocked) == 3


# ----------------------------------------------------------------------
# 14. Duplicate Reservation Idempotency
# ----------------------------------------------------------------------
def test_duplicate_reservation_idempotency(quota_service, policy_repo):
    tenant_id = "tenant_idempotent"
    rule = QuotaRule(rule_id="r1", quota_type=QuotaType.MAX_REQUESTS, limit_value=5, scope=QuotaScope.TENANT)
    policy_repo.save_policy(QuotaPolicy(policy_id="p1", tenant_id=tenant_id, rules=(rule,)))

    req1 = QuotaRequest(tenant_id=tenant_id, correlation_id="idem_corr_1", estimated_total_tokens=100)
    dec1 = quota_service.evaluate_and_reserve(req1)

    # Repetir exactamente la misma petición con el mismo correlation_id
    dec2 = quota_service.evaluate_and_reserve(req1)

    assert dec1.is_allowed is True
    assert dec2.is_allowed is True
    assert dec1.reservation_id == dec2.reservation_id
    assert "IDEMPOTENT_REPLAY_ACTIVE_RESERVATION" in dec2.reason_codes


# ----------------------------------------------------------------------
# 15. Explicit Unlimited Policy
# ----------------------------------------------------------------------
def test_explicit_unlimited_policy(quota_service, policy_repo):
    tenant_id = "tenant_unlimited"
    policy_repo.save_policy(QuotaPolicy(
        policy_id="p_unlimited",
        tenant_id=tenant_id,
        rules=(),
        is_unlimited=True,
    ))

    req = QuotaRequest(
        tenant_id=tenant_id,
        estimated_total_tokens=999999999,
        estimated_cost=Decimal("50000.00"),
        correlation_id="corr_unlim",
    )
    decision = quota_service.evaluate_and_reserve(req)

    assert decision.is_allowed is True
    assert decision.status == QuotaStatus.ALLOW
    assert any("UNLIMITED" in rc for rc in decision.reason_codes)


# ----------------------------------------------------------------------
# 16. No O.8 Plan Logic Leakage
# ----------------------------------------------------------------------
def test_no_o8_plan_logic_leakage(policy_repo, reservation_repo, quota_service):
    """
    Verifica que QuotaPolicy y QuotaManagementService son gobernadas directamente
    sin referencias a planes O.8, suscripciones o pasarelas de pago.
    """
    policy = QuotaPolicy(
        policy_id="direct_policy",
        tenant_id="tenant_pure",
        rules=(
            QuotaRule(
                rule_id="direct_rule",
                quota_type=QuotaType.MAX_REQUESTS,
                limit_value=10,
                scope=QuotaScope.TENANT,
            ),
        ),
    )
    policy_repo.save_policy(policy)

    # Comprobar que no existen campos de planes o billing en el modelo
    assert not hasattr(policy, "plan_id")
    assert not hasattr(policy, "subscription_id")
    assert not hasattr(policy, "billing_tier")
