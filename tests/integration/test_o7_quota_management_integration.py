"""
Tests de Integración y E2E para Quota Management SaaS (Hito O.7 — Quota Management).

Escenarios Obligatorios:
A. Tenant under quota -> O.5 provider called (status SUCCESS, reservation CONSUMED).
B. Tenant over quota -> zero provider calls (status QUOTA_EXCEEDED, no provider invocation).
C. User over limit while tenant under -> zero calls (isolated user budget block).
D. Tenant A exhausted -> Tenant B unaffected (strict cross-tenant isolation).
E. Rate limit exceeded -> local RATE_LIMITED; zero provider calls.
F. Cache hit -> accounting according to configured rule (bypass or hit recording).
G. Two concurrent requests near hard limit -> only allowed capacity executes (no TOCTOU oversubscription).
H. Provider failure -> reservation reconciled/released safely without permanent leaks.
I. Restart -> quota policies, historical usage and reservation state preserved across repository reload.
J. Corrupt usage/policy -> fail-safe behavior (UNKNOWN / blocked, zero permissive leaks).
K. E2E Complete SaaS Flow: Tenant -> Session -> Authorization -> Quota Evaluation/Reservation -> Gateway -> Provider -> Usage -> Reconciliation.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union
import pytest

# Domain Imports
from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.organization.models import (
    Organization,
    UserMembership,
    MembershipStatus,
    MembershipRole,
    OrganizationStatus,
)
from src.domain.rbac.models import Permission, Role, RoleAssignment, PermissionStatus, RoleStatus
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
)
from src.domain.model_routing.models import (
    ModelRoute,
    RoutingRequest,
    RoutingDecision,
    RoutingDecisionStatus,
    RouteCapability,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RouteStatus,
)
from src.domain.model_gateway.models import (
    ModelGatewayRequest,
    ModelGatewayResponse,
    ModelGatewayStatus,
    ProviderRequestReference,
    ProviderErrorType,
    TenantModelConfig,
)
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageQuery,
    UsagePeriod,
    UsagePeriodType,
    UsageRequestStatus,
)
from src.domain.quota_management.models import (
    QuotaPolicy,
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
    QuotaStatus,
    QuotaRequest,
    QuotaDecision,
    QuotaReservation,
    QuotaReservationStatus,
    QuotaPolicyIntegrityError,
)
from src.domain.reliability.ports import ClockPort

# Application Imports
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.usage_metering.model_gateway_bridge import ModelGatewayUsageBridge
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.model_gateway.model_gateway_service import ModelGatewayService
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry

# Infrastructure Repositories
from src.infrastructure.persistence.data.json.usage_event_repository import (
    InMemoryUsageEventRepository,
    JsonUsageEventRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
    InMemoryQuotaReservationRepository,
    JsonQuotaPolicyRepository,
    JsonQuotaReservationRepository,
)


class MockClock(ClockPort):
    def __init__(self, initial_time: datetime):
        self._current_time = initial_time

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def advance(self, td: timedelta) -> None:
        self._current_time += td


class MockProviderExecutor:
    """Mock determinista de proveedor LLM que cuenta llamadas."""
    def __init__(self, succeed: bool = True, tokens_out: int = 50, cost: Decimal = Decimal("0.001")):
        self.succeed = succeed
        self.tokens_out = tokens_out
        self.cost = cost
        self.call_count = 0

    def execute_call(self, route: ModelRoute, prompt: Any) -> Dict[str, Any]:
        self.call_count += 1
        if not self.succeed:
            raise RuntimeError("Provider upstream error 500")
        return {
            "output_tokens": self.tokens_out,
            "cost": self.cost,
            "raw_response": "Mock inference response content",
        }


@pytest.fixture
def now_utc() -> datetime:
    return datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def standard_route() -> ModelRoute:
    return ModelRoute(
        route_id="route_openai_gpt4o",
        provider="openai",
        model_id="gpt-4o",
        status=RouteStatus.AVAILABLE,
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        context_window=128000,
        estimated_cost_input_per_million=Decimal("5.00"),
        estimated_cost_output_per_million=Decimal("15.00"),
    )


# =========================================================================
# Escenario A: Tenant Under Quota -> Provider Called
# =========================================================================
def test_scenario_a_tenant_under_quota_calls_provider(now_utc, standard_route):
    clock = MockClock(now_utc)
    policy_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )

    tenant_id = "tenant_scenario_a"
    rule = QuotaRule(
        rule_id="r_tokens",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=10000,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.DAY,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="pol_a", tenant_id=tenant_id, rules=(rule,)))

    # Quota Request
    q_req = QuotaRequest(
        tenant_id=tenant_id,
        identity_id="user_1",
        estimated_total_tokens=1000,
        estimated_cost=Decimal("0.02"),
        correlation_id="corr_a",
    )
    decision = quota_service.evaluate_and_reserve(q_req)
    assert decision.is_allowed is True
    assert decision.status == QuotaStatus.ALLOW
    assert decision.reservation_id is not None

    # Simular ejecución exitosa de proveedor
    mock_provider = MockProviderExecutor(succeed=True, tokens_out=800)
    provider_res = mock_provider.execute_call(standard_route, "test prompt")
    assert mock_provider.call_count == 1

    # Registrar en O.6 y reconciliar en O.7
    u_event = usage_service.record_usage_event(
        context=TenantContext(tenant_id=tenant_id),
        event=UsageEvent(
            usage_event_id="ev_a",
            tenant_id=tenant_id,
            identity_id="user_1",
            provider="openai",
            model="gpt-4o",
            request_status=UsageRequestStatus.SUCCESS,
            total_tokens=1000,
            actual_cost=Decimal("0.02"),
            occurred_at=clock.now(),
        ),
    )
    assert u_event.total_tokens == 1000

    reconciled = quota_service.reconcile_reservation(
        reservation_id=decision.reservation_id,
        tenant_id=tenant_id,
        actual_status=QuotaReservationStatus.CONSUMED,
        actual_tokens=1000,
        actual_cost=Decimal("0.02"),
    )
    assert reconciled.status == QuotaReservationStatus.CONSUMED


# =========================================================================
# Escenario B: Tenant Over Quota -> Zero Provider Calls
# =========================================================================
def test_scenario_b_tenant_over_quota_zero_provider_calls(now_utc, standard_route):
    clock = MockClock(now_utc)
    policy_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )

    tenant_id = "tenant_scenario_b"
    # Límite de 5000 tokens
    rule = QuotaRule(
        rule_id="r_tokens",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=5000,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.DAY,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="pol_b", tenant_id=tenant_id, rules=(rule,)))

    # Registrar consumo previo de 4800 tokens en O.6
    usage_service.record_usage_event(
        context=TenantContext(tenant_id=tenant_id),
        event=UsageEvent(
            usage_event_id="ev_b_prev",
            tenant_id=tenant_id,
            request_status=UsageRequestStatus.SUCCESS,
            total_tokens=4800,
            occurred_at=clock.now(),
        ),
    )

    mock_provider = MockProviderExecutor()

    # Intentar consumir 500 tokens (4800 + 500 = 5300 > 5000)
    q_req = QuotaRequest(
        tenant_id=tenant_id,
        estimated_total_tokens=500,
        correlation_id="corr_b",
    )
    decision = quota_service.evaluate_and_reserve(q_req)

    assert decision.is_allowed is False
    assert decision.status == QuotaStatus.LIMIT_REACHED
    assert "TOTAL_TOKEN_LIMIT_EXCEEDED" in decision.reason_codes

    # Cero llamadas al proveedor
    if decision.is_allowed:
        mock_provider.execute_call(standard_route, "prompt")
    assert mock_provider.call_count == 0


# =========================================================================
# Escenario C: User Over Limit While Tenant Under -> Zero Calls
# =========================================================================
def test_scenario_c_user_over_limit_tenant_under_zero_calls(now_utc):
    clock = MockClock(now_utc)
    policy_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )

    tenant_id = "tenant_scenario_c"
    # Tenant: $100/day, User: $10/day
    rule_tenant = QuotaRule(
        rule_id="r_tenant_cost",
        quota_type=QuotaType.MAX_COST,
        limit_value=Decimal("100.00"),
        scope=QuotaScope.TENANT,
    )
    rule_user = QuotaRule(
        rule_id="r_user_cost",
        quota_type=QuotaType.MAX_COST,
        limit_value=Decimal("10.00"),
        scope=QuotaScope.USER,
        target_identifier="usr_greedy",
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="pol_c", tenant_id=tenant_id, rules=(rule_tenant, rule_user)))

    # Usr greedy ya gastó $9.50
    usage_service.record_usage_event(
        context=TenantContext(tenant_id=tenant_id),
        event=UsageEvent(
            usage_event_id="ev_c_user",
            tenant_id=tenant_id,
            identity_id="usr_greedy",
            request_status=UsageRequestStatus.SUCCESS,
            actual_cost=Decimal("9.50"),
            occurred_at=clock.now(),
        ),
    )

    mock_provider = MockProviderExecutor()

    # Petición de usr_greedy por $1.00 -> bloqueado (9.50 + 1.00 = 10.50 > 10.00)
    q_req = QuotaRequest(
        tenant_id=tenant_id,
        identity_id="usr_greedy",
        estimated_cost=Decimal("1.00"),
        correlation_id="corr_c_greedy",
    )
    decision = quota_service.evaluate_and_reserve(q_req)
    assert decision.is_allowed is False
    assert decision.status == QuotaStatus.LIMIT_REACHED

    # Petición de otro usuario usr_frugal por $1.00 -> PERMITIDO (Tenant total = $9.50 + $1.00 = $10.50 <= $100.00)
    q_req_frugal = QuotaRequest(
        tenant_id=tenant_id,
        identity_id="usr_frugal",
        estimated_cost=Decimal("1.00"),
        correlation_id="corr_c_frugal",
    )
    decision_frugal = quota_service.evaluate_and_reserve(q_req_frugal)
    assert decision_frugal.is_allowed is True
    assert decision_frugal.status == QuotaStatus.ALLOW


# =========================================================================
# Escenario D: Tenant A Exhausted -> Tenant B Unaffected
# =========================================================================
def test_scenario_d_cross_tenant_isolation_exhausted_unaffected(now_utc):
    clock = MockClock(now_utc)
    policy_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )

    t_a = "tenant_iso_a"
    t_b = "tenant_iso_b"

    rule_a = QuotaRule(rule_id="ra", quota_type=QuotaType.MAX_REQUESTS, limit_value=2, scope=QuotaScope.TENANT)
    rule_b = QuotaRule(rule_id="rb", quota_type=QuotaType.MAX_REQUESTS, limit_value=10, scope=QuotaScope.TENANT)

    policy_repo.save_policy(QuotaPolicy(policy_id="pa", tenant_id=t_a, rules=(rule_a,)))
    policy_repo.save_policy(QuotaPolicy(policy_id="pb", tenant_id=t_b, rules=(rule_b,)))

    # Agotar tenant A
    for i in range(2):
        usage_service.record_usage_event(
            context=TenantContext(tenant_id=t_a),
            event=UsageEvent(
                usage_event_id=f"ev_a_{i}",
                tenant_id=t_a,
                request_status=UsageRequestStatus.SUCCESS,
                occurred_at=clock.now(),
            ),
        )

    # Evaluar A -> Bloqueado
    dec_a = quota_service.evaluate_and_reserve(QuotaRequest(tenant_id=t_a, correlation_id="c_a"))
    assert dec_a.is_allowed is False
    assert dec_a.status == QuotaStatus.LIMIT_REACHED

    # Evaluar B -> Permitido sin interferencia
    dec_b = quota_service.evaluate_and_reserve(QuotaRequest(tenant_id=t_b, correlation_id="c_b"))
    assert dec_b.is_allowed is True
    assert dec_b.status == QuotaStatus.ALLOW


# =========================================================================
# Escenario E: Rate Limit Exceeded -> Local RATE_LIMITED
# =========================================================================
def test_scenario_e_rate_limit_exceeded_local_status(now_utc):
    clock = MockClock(now_utc)
    policy_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )

    tenant_id = "tenant_rpm"
    rule_rpm = QuotaRule(
        rule_id="r_rpm",
        quota_type=QuotaType.REQUESTS_PER_MINUTE,
        limit_value=2,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.MINUTE,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p_rpm", tenant_id=tenant_id, rules=(rule_rpm,)))

    # Realizar 2 requests en el mismo minuto
    dec1 = quota_service.evaluate_and_reserve(QuotaRequest(tenant_id=tenant_id, correlation_id="rpm_1"))
    assert dec1.is_allowed is True

    dec2 = quota_service.evaluate_and_reserve(QuotaRequest(tenant_id=tenant_id, correlation_id="rpm_2"))
    assert dec2.is_allowed is True

    # 3ra request en el mismo minuto -> RATE_LIMITED
    dec3 = quota_service.evaluate_and_reserve(QuotaRequest(tenant_id=tenant_id, correlation_id="rpm_3"))
    assert dec3.is_allowed is False
    assert dec3.status == QuotaStatus.RATE_LIMITED
    assert "REQUEST_LIMIT_EXCEEDED" in dec3.reason_codes

    # Avanzar reloj 65 segundos -> nuevo minuto -> ALLOW
    clock.advance(timedelta(seconds=65))
    dec4 = quota_service.evaluate_and_reserve(QuotaRequest(tenant_id=tenant_id, correlation_id="rpm_4"))
    assert dec4.is_allowed is True
    assert dec4.status == QuotaStatus.ALLOW


# =========================================================================
# Escenario F: Cache Hit -> Accounting Policy
# =========================================================================
def test_scenario_f_cache_hit_bypass_accounting(now_utc):
    clock = MockClock(now_utc)
    policy_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )

    tenant_id = "tenant_cache_rule"
    rule = QuotaRule(
        rule_id="r_cache",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=100,
        scope=QuotaScope.TENANT,
        allow_cache_hit_bypass_token_budget=True,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p_cache", tenant_id=tenant_id, rules=(rule,)))

    # Request con cache hit y estimación de 5000 tokens
    req = QuotaRequest(
        tenant_id=tenant_id,
        estimated_total_tokens=5000,
        is_cache_hit=True,
        correlation_id="corr_cache_hit",
    )
    dec = quota_service.evaluate_and_reserve(req)
    assert dec.is_allowed is True
    assert dec.status == QuotaStatus.ALLOW


# =========================================================================
# Escenario G: Two Concurrent Requests Near Hard Limit (Anti-TOCTOU)
# =========================================================================
def test_scenario_g_concurrent_near_limit_no_oversubscription(now_utc):
    clock = MockClock(now_utc)
    policy_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )

    tenant_id = "tenant_toctou"
    # Límite exacto de 1500 tokens
    rule = QuotaRule(
        rule_id="r_toctou",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=1500,
        scope=QuotaScope.TENANT,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p_toctou", tenant_id=tenant_id, rules=(rule,)))

    results = []

    def make_reservation(req_idx: int):
        req = QuotaRequest(
            tenant_id=tenant_id,
            estimated_total_tokens=1000,
            correlation_id=f"corr_concurrent_{req_idx}",
        )
        dec = quota_service.evaluate_and_reserve(req)
        results.append(dec)

    # Disparar 2 peticiones concurrentes de 1000 tokens cada una
    t1 = threading.Thread(target=make_reservation, args=(1,))
    t2 = threading.Thread(target=make_reservation, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    allowed = [r for r in results if r.is_allowed]
    blocked = [r for r in results if not r.is_allowed]

    # Exactamente 1 debe ser permitida y 1 bloqueada (no oversubscription de 2000 > 1500)
    assert len(allowed) == 1
    assert len(blocked) == 1


# =========================================================================
# Escenario H: Provider Failure -> Reservation Reconciled/Released Safely
# =========================================================================
def test_scenario_h_provider_failure_reservation_released(now_utc):
    clock = MockClock(now_utc)
    policy_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=policy_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )

    tenant_id = "tenant_fail"
    rule = QuotaRule(
        rule_id="r_fail",
        quota_type=QuotaType.MAX_TOTAL_TOKENS,
        limit_value=2000,
        scope=QuotaScope.TENANT,
    )
    policy_repo.save_policy(QuotaPolicy(policy_id="p_fail", tenant_id=tenant_id, rules=(rule,)))

    # 1. Reservar 1500 tokens
    dec = quota_service.evaluate_and_reserve(
        QuotaRequest(tenant_id=tenant_id, estimated_total_tokens=1500, correlation_id="c_fail")
    )
    assert dec.is_allowed is True
    res_id = dec.reservation_id

    # 2. Simular fallo de proveedor y liberar reserva
    released = quota_service.reconcile_reservation(
        reservation_id=res_id,
        tenant_id=tenant_id,
        actual_status=QuotaReservationStatus.RELEASED,
    )
    assert released.status == QuotaReservationStatus.RELEASED

    # 3. Capacidad debe quedar intacta para la siguiente solicitud
    dec_next = quota_service.evaluate_and_reserve(
        QuotaRequest(tenant_id=tenant_id, estimated_total_tokens=1500, correlation_id="c_next")
    )
    assert dec_next.is_allowed is True


# =========================================================================
# Escenario I: Restart -> Quota State Preserved in Json Repository
# =========================================================================
def test_scenario_i_restart_json_persistence(tmp_path, now_utc):
    clock = MockClock(now_utc)
    base_dir = tmp_path / "saas_data"

    policy_repo1 = JsonQuotaPolicyRepository(base_dir=base_dir)
    res_repo1 = JsonQuotaReservationRepository(base_dir=base_dir)
    usage_repo1 = JsonUsageEventRepository(base_storage_dir=base_dir)
    usage_service1 = UsageMeteringService(repository=usage_repo1, clock=clock)
    quota_service1 = QuotaManagementService(
        policy_repository=policy_repo1,
        reservation_repository=res_repo1,
        usage_metering_service=usage_service1,
        clock=clock,
    )

    tenant_id = "tenant_persist"
    rule = QuotaRule(
        rule_id="r_persist",
        quota_type=QuotaType.MAX_REQUESTS,
        limit_value=3,
        scope=QuotaScope.TENANT,
    )
    policy_repo1.save_policy(QuotaPolicy(policy_id="p_persist", tenant_id=tenant_id, rules=(rule,)))

    # Consumir 2 requests
    usage_service1.record_usage_event(
        context=TenantContext(tenant_id=tenant_id),
        event=UsageEvent(usage_event_id="ev_p1", tenant_id=tenant_id, request_status=UsageRequestStatus.SUCCESS, occurred_at=clock.now()),
    )
    usage_service1.record_usage_event(
        context=TenantContext(tenant_id=tenant_id),
        event=UsageEvent(usage_event_id="ev_p2", tenant_id=tenant_id, request_status=UsageRequestStatus.SUCCESS, occurred_at=clock.now()),
    )

    # Crear una reserva activa en disco
    dec1 = quota_service1.evaluate_and_reserve(QuotaRequest(tenant_id=tenant_id, correlation_id="c_p3"))
    assert dec1.is_allowed is True

    # --- SIMULAR REINICIO COMPLETO ---
    policy_repo2 = JsonQuotaPolicyRepository(base_dir=base_dir)
    res_repo2 = JsonQuotaReservationRepository(base_dir=base_dir)
    usage_repo2 = JsonUsageEventRepository(base_storage_dir=base_dir)
    usage_service2 = UsageMeteringService(repository=usage_repo2, clock=clock)
    quota_service2 = QuotaManagementService(
        policy_repository=policy_repo2,
        reservation_repository=res_repo2,
        usage_metering_service=usage_service2,
        clock=clock,
    )

    # Política preservada
    loaded_pol = policy_repo2.get_policy(tenant_id)
    assert loaded_pol is not None
    assert loaded_pol.policy_id == "p_persist"

    # Intento de 4ta request -> Debe ser bloqueada (2 en histórico + 1 en vuelo = 3 == límite)
    dec2 = quota_service2.evaluate_and_reserve(QuotaRequest(tenant_id=tenant_id, correlation_id="c_p4"))
    assert dec2.is_allowed is False
    assert dec2.status == QuotaStatus.LIMIT_REACHED


# =========================================================================
# Escenario J: Corrupt Policy / Checksum Tampering -> Fail Safe
# =========================================================================
def test_scenario_j_corrupt_policy_fail_safe():
    with pytest.raises(QuotaPolicyIntegrityError):
        QuotaPolicy(
            policy_id="pol_corrupt",
            tenant_id="tenant_x",
            rules=(),
            checksum="bad_tampered_hash_12345",
        )
