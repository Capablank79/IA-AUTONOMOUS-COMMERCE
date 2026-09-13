"""
Pruebas unitarias para Rate-limit Management (Hito P.11 — Production / Operations).

Cubre los 16 requisitos mínimos de la especificación:
1. allow below limit
2. deny at limit
3. reset after window
4. retry-after correct
5. burst behavior
6. tenant isolation
7. user isolation
8. provider scope
9. model scope
10. unlimited explicit
11. missing policy != implicit unlimited
12. concurrency boundary
13. environment isolation
14. quota != rate-limit
15. no provider call when denied
16. no Gate O/Hito Q implementation
"""

from datetime import datetime, timezone, timedelta
import threading
from typing import Optional, List
import pytest

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.reliability.ports import ClockPort
from src.domain.tenant.models import TenantContext
from src.domain.quota_management.models import QuotaPolicy, QuotaRule, QuotaType, QuotaWindowType
from src.domain.rate_limit.models import (
    RateLimitScope,
    RateLimitStatus,
    RateLimitWindowUnit,
    RateLimitRule,
    RateLimitPolicy,
    RateLimitRequest,
    RateLimitDecision,
    build_canonical_rate_limit_key,
)
from src.domain.rate_limit.ports import (
    RateLimitPolicyRepositoryPort,
    RateLimitStateStorePort,
    RateLimitTelemetryPort,
)
from src.infrastructure.persistence.data.json.rate_limit_repository import (
    InMemoryRateLimitPolicyRepository,
    InMemoryRateLimitStateStore,
)
from src.application.rate_limit.rate_limit_service import RateLimitService


class FakeClock(ClockPort):
    """Clock inyectable para pruebas deterministas sin time.sleep."""
    def __init__(self, initial_time: Optional[datetime] = None) -> None:
        self._current_time = initial_time or datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)


class DummyTelemetry(RateLimitTelemetryPort):
    def __init__(self) -> None:
        self.emitted_facts: List[RateLimitDecision] = []

    def emit_rate_limit_fact(self, decision: RateLimitDecision, request: RateLimitRequest) -> None:
        self.emitted_facts.append(decision)


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def policy_repo():
    return InMemoryRateLimitPolicyRepository()


@pytest.fixture
def state_store():
    return InMemoryRateLimitStateStore()


@pytest.fixture
def telemetry():
    return DummyTelemetry()


@pytest.fixture
def rate_limit_service(policy_repo, state_store, telemetry, fake_clock):
    return RateLimitService(
        policy_repository=policy_repo,
        state_store=state_store,
        telemetry=telemetry,
        clock=fake_clock,
        environment=ApplicationEnvironment.PRODUCTION,
        fail_closed_on_missing_policy=True,
    )


def test_1_allow_below_limit(rate_limit_service, policy_repo, fake_clock):
    """1. allow below limit: Permite peticiones mientras existan tokens disponibles."""
    rule = RateLimitRule(
        rule_id="tenant_rpm_60",
        scope=RateLimitScope.TENANT,
        limit_rate=60,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=60,
    )
    policy = RateLimitPolicy(
        policy_id="pol_tenant_1",
        tenant_id="tenant_alpha",
        rules=(rule,),
    )
    policy_repo.save_policy(policy)

    req = RateLimitRequest(
        tenant_id="tenant_alpha",
        cost_units=1,
        requested_at=fake_clock.now(),
    )

    decision = rate_limit_service.check_and_consume(req)
    assert decision.status == RateLimitStatus.ALLOW
    assert decision.remaining == 59
    assert decision.retry_after_seconds == 0
    assert decision.http_status_code == 200


def test_2_deny_at_limit(rate_limit_service, policy_repo, fake_clock):
    """2. deny at limit: Rechaza con DENY cuando se agotan los tokens."""
    rule = RateLimitRule(
        rule_id="tenant_rpm_2",
        scope=RateLimitScope.TENANT,
        limit_rate=2,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=2,
    )
    policy = RateLimitPolicy(
        policy_id="pol_tenant_2",
        tenant_id="tenant_beta",
        rules=(rule,),
    )
    policy_repo.save_policy(policy)

    req = RateLimitRequest(
        tenant_id="tenant_beta",
        cost_units=1,
        requested_at=fake_clock.now(),
    )

    # 1st: ALLOW (remaining 1)
    d1 = rate_limit_service.check_and_consume(req)
    assert d1.status == RateLimitStatus.ALLOW

    # 2nd: ALLOW (remaining 0)
    d2 = rate_limit_service.check_and_consume(req)
    assert d2.status == RateLimitStatus.ALLOW

    # 3rd: DENY (exhausted)
    d3 = rate_limit_service.check_and_consume(req)
    assert d3.status == RateLimitStatus.DENY
    assert d3.http_status_code == 429
    assert d3.remaining == 0
    assert d3.retry_after_seconds > 0


def test_3_reset_after_window(rate_limit_service, policy_repo, fake_clock):
    """3. reset after window: Vuelve a permitir tras el paso del tiempo y recarga del bucket."""
    rule = RateLimitRule(
        rule_id="tenant_rpm_1",
        scope=RateLimitScope.TENANT,
        limit_rate=60,  # 1 token per second
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=60,
    )
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="pol_tenant_3",
        tenant_id="tenant_gamma",
        rules=(rule,),
    ))

    # Consume all 60 tokens
    req_60 = RateLimitRequest(tenant_id="tenant_gamma", cost_units=60, requested_at=fake_clock.now())
    d1 = rate_limit_service.check_and_consume(req_60)
    assert d1.status == RateLimitStatus.ALLOW

    # Immediately: DENY (0 tokens left)
    req_1 = RateLimitRequest(tenant_id="tenant_gamma", cost_units=1, requested_at=fake_clock.now())
    d2 = rate_limit_service.check_and_consume(req_1)
    assert d2.status == RateLimitStatus.DENY

    # Advance clock by 1 second (1 token refilled)
    fake_clock.advance(1.0)
    req_later = RateLimitRequest(tenant_id="tenant_gamma", cost_units=1, requested_at=fake_clock.now())

    d3 = rate_limit_service.check_and_consume(req_later)
    assert d3.status == RateLimitStatus.ALLOW


def test_4_retry_after_correct(rate_limit_service, policy_repo, fake_clock):
    """4. retry-after correct: Calcula de forma determinista y entera los segundos necesarios."""
    # 60 req/min = 1 req/sec. Deficit of 5 tokens requires 5 seconds.
    rule = RateLimitRule(
        rule_id="tenant_rpm_60",
        scope=RateLimitScope.TENANT,
        limit_rate=60,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=60,
    )
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="pol_tenant_4",
        tenant_id="tenant_delta",
        rules=(rule,),
    ))

    # Consume all 60 tokens
    req_60 = RateLimitRequest(tenant_id="tenant_delta", cost_units=60, requested_at=fake_clock.now())
    d1 = rate_limit_service.check_and_consume(req_60)
    assert d1.status == RateLimitStatus.ALLOW

    # Request 5 tokens with 0 available -> deficit = 5 -> retry_after = 5 sec
    req_5 = RateLimitRequest(tenant_id="tenant_delta", cost_units=5, requested_at=fake_clock.now())
    d2 = rate_limit_service.check_and_consume(req_5)
    assert d2.status == RateLimitStatus.DENY
    assert d2.retry_after_seconds == 5
    headers = d2.to_http_headers()
    assert headers["Retry-After"] == "5"


def test_5_burst_behavior(rate_limit_service, policy_repo, fake_clock):
    """5. burst behavior: Permite ráfagas mayores al steady rate si burst_capacity lo especifica."""
    # Steady rate: 10 req/minute (0.166 req/s), pero burst allowance de 25
    rule = RateLimitRule(
        rule_id="tenant_burst_rule",
        scope=RateLimitScope.TENANT,
        limit_rate=10,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=25,
    )
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="pol_tenant_5",
        tenant_id="tenant_burst",
        rules=(rule,),
    ))

    # Should allow consuming 20 tokens in a single burst
    req = RateLimitRequest(tenant_id="tenant_burst", cost_units=20, requested_at=fake_clock.now())
    d = rate_limit_service.check_and_consume(req)
    assert d.status == RateLimitStatus.ALLOW
    assert d.remaining == 5


def test_6_tenant_isolation(rate_limit_service, policy_repo, fake_clock):
    """6. tenant isolation: Tenant A agotando su límite NO afecta a Tenant B."""
    rule_a = RateLimitRule(rule_id="rule_a", scope=RateLimitScope.TENANT, limit_rate=1, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=1)
    rule_b = RateLimitRule(rule_id="rule_b", scope=RateLimitScope.TENANT, limit_rate=10, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=10)

    policy_repo.save_policy(RateLimitPolicy(policy_id="p_a", tenant_id="tenant_A", rules=(rule_a,)))
    policy_repo.save_policy(RateLimitPolicy(policy_id="p_b", tenant_id="tenant_B", rules=(rule_b,)))

    req_a = RateLimitRequest(tenant_id="tenant_A", cost_units=1, requested_at=fake_clock.now())
    req_b = RateLimitRequest(tenant_id="tenant_B", cost_units=1, requested_at=fake_clock.now())

    # Tenant A consumes and exhausts
    d_a1 = rate_limit_service.check_and_consume(req_a)
    assert d_a1.status == RateLimitStatus.ALLOW

    d_a2 = rate_limit_service.check_and_consume(req_a)
    assert d_a2.status == RateLimitStatus.DENY

    # Tenant B is completely unaffected
    d_b1 = rate_limit_service.check_and_consume(req_b)
    assert d_b1.status == RateLimitStatus.ALLOW
    assert d_b1.remaining == 9


def test_7_user_isolation(rate_limit_service, policy_repo, fake_clock):
    """7. user isolation: Un usuario ruidoso dentro del tenant no bloquea a otros usuarios si hay user rule."""
    tenant_rule = RateLimitRule(rule_id="t_rule", scope=RateLimitScope.TENANT, limit_rate=100, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=100)
    user_rule = RateLimitRule(rule_id="u_rule", scope=RateLimitScope.USER, limit_rate=2, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=2)

    policy_repo.save_policy(RateLimitPolicy(
        policy_id="p_users",
        tenant_id="tenant_corp",
        rules=(tenant_rule, user_rule),
    ))

    req_u1 = RateLimitRequest(tenant_id="tenant_corp", identity_id="user_1", cost_units=1, requested_at=fake_clock.now())
    req_u2 = RateLimitRequest(tenant_id="tenant_corp", identity_id="user_2", cost_units=1, requested_at=fake_clock.now())

    # User 1 exhausts their 2 requests
    assert rate_limit_service.check_and_consume(req_u1).status == RateLimitStatus.ALLOW
    assert rate_limit_service.check_and_consume(req_u1).status == RateLimitStatus.ALLOW
    assert rate_limit_service.check_and_consume(req_u1).status == RateLimitStatus.DENY

    # User 2 still has full capacity
    d_u2 = rate_limit_service.check_and_consume(req_u2)
    assert d_u2.status == RateLimitStatus.ALLOW


def test_8_provider_scope(rate_limit_service, policy_repo, fake_clock):
    """8. provider scope: Protege el upstream provider globalmente entre tenants."""
    provider_rule = RateLimitRule(
        rule_id="openai_upstream_limit",
        scope=RateLimitScope.PROVIDER,
        target_identifier="openai",
        limit_rate=2,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=2,
    )
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="p_global",
        tenant_id=None,
        rules=(provider_rule,),
    ))

    # Tenant 1 makes 1 call to OpenAI
    req_t1 = RateLimitRequest(tenant_id="t1", provider="openai", cost_units=1, requested_at=fake_clock.now())
    assert rate_limit_service.check_and_consume(req_t1).status == RateLimitStatus.ALLOW

    # Tenant 2 makes 1 call to OpenAI
    req_t2 = RateLimitRequest(tenant_id="t2", provider="openai", cost_units=1, requested_at=fake_clock.now())
    assert rate_limit_service.check_and_consume(req_t2).status == RateLimitStatus.ALLOW

    # Tenant 3 attempts call to OpenAI -> Denied by provider limit
    req_t3 = RateLimitRequest(tenant_id="t3", provider="openai", cost_units=1, requested_at=fake_clock.now())
    d_t3 = rate_limit_service.check_and_consume(req_t3)
    assert d_t3.status == RateLimitStatus.DENY
    assert d_t3.scope_violated == RateLimitScope.PROVIDER


def test_9_model_scope(rate_limit_service, policy_repo, fake_clock):
    """9. model scope: Límites separados por modelo."""
    m_rule = RateLimitRule(
        rule_id="gpt4_expensive_limit",
        scope=RateLimitScope.MODEL,
        target_identifier="gpt-4",
        limit_rate=1,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=1,
    )
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="p_models",
        tenant_id="tenant_model_test",
        rules=(m_rule,),
    ))

    # Request for gpt-4
    req_gpt4 = RateLimitRequest(tenant_id="tenant_model_test", model_id="gpt-4", cost_units=1, requested_at=fake_clock.now())
    assert rate_limit_service.check_and_consume(req_gpt4).status == RateLimitStatus.ALLOW
    assert rate_limit_service.check_and_consume(req_gpt4).status == RateLimitStatus.DENY

    # Request for gpt-3.5 is not matched by gpt-4 rule
    req_gpt35 = RateLimitRequest(tenant_id="tenant_model_test", model_id="gpt-3.5-turbo", cost_units=1, requested_at=fake_clock.now())
    # No matching rule for gpt-3.5 in tenant policy -> ALLOW since tenant policy exists
    d_gpt35 = rate_limit_service.check_and_consume(req_gpt35)
    assert d_gpt35.status == RateLimitStatus.ALLOW


def test_10_unlimited_explicit(rate_limit_service, policy_repo, fake_clock):
    """10. unlimited explicit: Política explícitamente ilimitada permite sin restricción."""
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="p_unlimited",
        tenant_id="tenant_enterprise",
        is_unlimited=True,
    ))

    req = RateLimitRequest(tenant_id="tenant_enterprise", cost_units=1000, requested_at=fake_clock.now())
    for _ in range(50):
        decision = rate_limit_service.check_and_consume(req)
        assert decision.status == RateLimitStatus.ALLOW
        assert "EXPLICIT_UNLIMITED" in decision.reason_codes


def test_11_missing_policy_not_implicit_unlimited(rate_limit_service, fake_clock):
    """11. missing policy != implicit unlimited: Falla seguro (DENY) si no hay política configurada."""
    req = RateLimitRequest(tenant_id="unknown_tenant", cost_units=1, requested_at=fake_clock.now())
    decision = rate_limit_service.check_and_consume(req)
    assert decision.status == RateLimitStatus.DENY
    assert "MISSING_POLICY_FAIL_CLOSED" in decision.reason_codes
    assert decision.retry_after_seconds > 0


def test_12_concurrency_boundary(rate_limit_service, policy_repo, fake_clock):
    """12. concurrency boundary: Evita TOCTOU y sobreconsumo ante peticiones concurrentes."""
    # Exactamente 5 tokens disponibles
    rule = RateLimitRule(
        rule_id="tenant_concurrent_test",
        scope=RateLimitScope.TENANT,
        limit_rate=5,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=5,
    )
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="p_conc",
        tenant_id="tenant_concurrent",
        rules=(rule,),
    ))

    req = RateLimitRequest(tenant_id="tenant_concurrent", cost_units=1, requested_at=fake_clock.now())
    results: List[RateLimitStatus] = []
    lock = threading.Lock()

    def worker():
        dec = rate_limit_service.check_and_consume(req)
        with lock:
            results.append(dec.status)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(RateLimitStatus.ALLOW) == 5
    assert results.count(RateLimitStatus.DENY) == 5


def test_13_environment_isolation(rate_limit_service, policy_repo, fake_clock):
    """13. environment isolation: Tráfico en DEV no consume contadores de PROD."""
    rule = RateLimitRule(
        rule_id="env_iso_rule",
        scope=RateLimitScope.TENANT,
        limit_rate=2,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=2,
    )
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="p_iso",
        tenant_id="tenant_iso",
        rules=(rule,),
    ))

    # Consumir los 2 tokens en DEV
    req_dev = RateLimitRequest(
        tenant_id="tenant_iso",
        cost_units=2,
        environment=ApplicationEnvironment.DEVELOPMENT,
        requested_at=fake_clock.now(),
    )
    d_dev = rate_limit_service.check_and_consume(req_dev)
    assert d_dev.status == RateLimitStatus.ALLOW

    # DEV ahora está agotado
    d_dev2 = rate_limit_service.check_and_consume(req_dev)
    assert d_dev2.status == RateLimitStatus.DENY

    # PROD sigue teniendo sus 2 tokens intactos
    req_prod = RateLimitRequest(
        tenant_id="tenant_iso",
        cost_units=2,
        environment=ApplicationEnvironment.PRODUCTION,
        requested_at=fake_clock.now(),
    )
    d_prod = rate_limit_service.check_and_consume(req_prod)
    assert d_prod.status == RateLimitStatus.ALLOW


def test_14_quota_not_rate_limit():
    """14. quota != rate-limit: Demuestra la diferencia conceptual entre cuota comercial y throttling."""
    # Quota O.7: Límite comercial acumulado en una ventana de negocio (e.g. 10.000 req/mes)
    commercial_quota = QuotaRule(
        rule_id="quota_10k_monthly",
        quota_type=QuotaType.MAX_REQUESTS,
        limit_value=10000,
        window_type=QuotaWindowType.MONTH,
    )
    # Rate Limit P.11: Velocidad instantánea en caliente (e.g. 60 req/minuto con ráfaga)
    runtime_rate_limit = RateLimitRule(
        rule_id="rl_60_rpm",
        scope=RateLimitScope.TENANT,
        limit_rate=60,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=100,
    )

    assert commercial_quota.limit_value == 10000
    assert runtime_rate_limit.limit_rate == 60
    assert runtime_rate_limit.effective_burst == 100
    # No comparten el mismo tipo de objeto ni propósito


def test_15_no_provider_call_when_denied(rate_limit_service, policy_repo, fake_clock):
    """15. no provider call when denied: Pre-flight check previene side-effects."""
    rule = RateLimitRule(
        rule_id="strict_rl",
        scope=RateLimitScope.TENANT,
        limit_rate=1,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=1,
    )
    policy_repo.save_policy(RateLimitPolicy(
        policy_id="p_strict",
        tenant_id="tenant_preflight",
        rules=(rule,),
    ))

    provider_call_count = 0

    def mock_provider_inference():
        nonlocal provider_call_count
        provider_call_count += 1
        return {"output": "hello"}

    req = RateLimitRequest(tenant_id="tenant_preflight", cost_units=1, requested_at=fake_clock.now())

    # 1st request: ALLOW -> provider called
    decision1 = rate_limit_service.check_and_consume(req)
    if decision1.is_allowed:
        mock_provider_inference()
    assert provider_call_count == 1

    # 2nd request: DENY -> provider NOT called
    decision2 = rate_limit_service.check_and_consume(req)
    if decision2.is_allowed:
        mock_provider_inference()
    assert provider_call_count == 1  # Still 1, zero upstream calls


def test_16_no_gate_o_hito_q_implementation():
    """16. no Gate O/Hito Q implementation: Verifica que no existan módulos prematuros de Gate O ni Hito Q."""
    import importlib.util
    assert importlib.util.find_spec("src.application.gate_o") is None
    assert importlib.util.find_spec("src.application.hito_q") is None
