"""
Pruebas Unitarias para Usage Metering SaaS (Hito O.6 — Usage Metering).

Requisitos Canónicos Cubiertos:
1. usage event immutable
2. tenant required
3. identity reference canonical
4. request counted once
5. duplicate fact idempotent
6. conflicting duplicate rejected
7. token totals valid
8. UNKNOWN tokens != zero
9. estimated vs actual cost distinct
10. cache hit recorded correctly
11. failed request recorded
12. tenant isolation
13. aggregation by model
14. aggregation by user / identity
15. period boundaries deterministic
16. no O.7 quota enforcement
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest

from src.domain.tenant.models import TenantContext, CrossTenantAccessError, TenantSecurityViolationError
from src.domain.caching.models import CacheLookupStatus
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageQuery,
    UsagePeriod,
    UsagePeriodType,
    UsageRequestStatus,
    UsageEventConflictError,
    UsageEventIntegrityError,
)
from src.domain.usage_metering.ports import UsageEventRepositoryPort
from src.infrastructure.persistence.data.json.usage_event_repository import (
    InMemoryUsageEventRepository,
    JsonUsageEventRepository,
)
from src.application.usage_metering.usage_metering_service import UsageMeteringService


@pytest.fixture
def now_utc() -> datetime:
    return datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def tenant_a_context() -> TenantContext:
    return TenantContext(tenant_id="tenant-alpha")


@pytest.fixture
def tenant_b_context() -> TenantContext:
    return TenantContext(tenant_id="tenant-beta")


@pytest.fixture
def in_memory_service() -> UsageMeteringService:
    repo = InMemoryUsageEventRepository()
    return UsageMeteringService(repository=repo)


# =========================================================================
# 1. usage event immutable
# =========================================================================
def test_usage_event_is_strictly_immutable(now_utc):
    event = UsageEvent(
        usage_event_id="use_evt_001",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=100,
        output_tokens=50,
        estimated_cost=Decimal("0.005"),
    )
    with pytest.raises(Exception):
        event.input_tokens = 200  # Frozen instance error

    with pytest.raises(Exception):
        event.details["injected"] = "malicious"


# =========================================================================
# 2. tenant required
# =========================================================================
def test_tenant_id_is_strictly_required(now_utc):
    with pytest.raises(Exception):
        UsageEvent(
            usage_event_id="use_evt_002",
            tenant_id="",  # Empty tenant_id fails safe identifier
            occurred_at=now_utc,
            request_status=UsageRequestStatus.SUCCESS,
        )

    with pytest.raises(Exception):
        UsageEvent(
            usage_event_id="use_evt_002",
            tenant_id="../invalid/path",
            occurred_at=now_utc,
            request_status=UsageRequestStatus.SUCCESS,
        )


# =========================================================================
# 3. identity reference canonical
# =========================================================================
def test_identity_reference_canonical(now_utc, tenant_a_context, in_memory_service):
    event = UsageEvent(
        usage_event_id="use_evt_003",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        identity_id="usr_admin_01",
        organization_id="org_engineering",
        provider="anthropic",
        model="claude-3-5-sonnet",
        input_tokens=150,
        output_tokens=80,
    )
    saved = in_memory_service.record_usage_event(tenant_a_context, event)
    assert saved.identity_id == "usr_admin_01"
    assert saved.organization_id == "org_engineering"


# =========================================================================
# 4. request counted once
# =========================================================================
def test_request_counted_once(now_utc, tenant_a_context, in_memory_service):
    event = UsageEvent(
        usage_event_id="use_evt_004",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=100,
        output_tokens=50,
        estimated_cost=Decimal("0.003"),
    )
    in_memory_service.record_usage_event(tenant_a_context, event)

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)
    assert agg.total_requests == 1
    assert agg.successful_requests == 1
    assert agg.failed_requests == 0
    assert agg.total_tokens == 150
    assert agg.total_estimated_cost == Decimal("0.003")


# =========================================================================
# 5. duplicate fact idempotent
# =========================================================================
def test_duplicate_fact_is_idempotent(now_utc, tenant_a_context, in_memory_service):
    event = UsageEvent(
        usage_event_id="use_evt_005",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        provider="openai",
        model="gpt-4o",
        input_tokens=200,
        output_tokens=100,
        source_reference="gw_res:cor_12345",
    )
    # Primera grabación
    first = in_memory_service.record_usage_event(tenant_a_context, event)
    # Segunda grabación idéntica
    second = in_memory_service.record_usage_event(tenant_a_context, event)

    assert first.usage_event_id == second.usage_event_id
    assert first.checksum == second.checksum

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)
    assert agg.total_requests == 1
    assert agg.total_tokens == 300


# =========================================================================
# 6. conflicting duplicate rejected
# =========================================================================
def test_conflicting_duplicate_is_rejected(now_utc, tenant_a_context, in_memory_service):
    event1 = UsageEvent(
        usage_event_id="use_evt_006",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=200,
        output_tokens=100,
    )
    in_memory_service.record_usage_event(tenant_a_context, event1)

    event2_conflicting = UsageEvent(
        usage_event_id="use_evt_006",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=999,  # Dato contradictorio con mismo ID
        output_tokens=100,
    )

    with pytest.raises(UsageEventConflictError):
        in_memory_service.record_usage_event(tenant_a_context, event2_conflicting)


# =========================================================================
# 7. token totals valid & derivation
# =========================================================================
def test_token_totals_valid_and_auto_derivation(now_utc):
    event = UsageEvent(
        usage_event_id="use_evt_007",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=400,
        output_tokens=250,
        total_tokens=None,  # Auto-deriva a 650
    )
    assert event.total_tokens == 650

    with pytest.raises(ValueError):
        UsageEvent(
            usage_event_id="use_evt_007_invalid",
            tenant_id="tenant-alpha",
            occurred_at=now_utc,
            request_status=UsageRequestStatus.SUCCESS,
            input_tokens=-10,
        )


# =========================================================================
# 8. UNKNOWN tokens != zero
# =========================================================================
def test_unknown_tokens_not_converted_to_zero(now_utc, tenant_a_context, in_memory_service):
    event = UsageEvent(
        usage_event_id="use_evt_008",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=None,  # Absent / UNKNOWN
        output_tokens=None,
        total_tokens=None,
    )
    in_memory_service.record_usage_event(tenant_a_context, event)

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)
    assert agg.total_requests == 1
    assert agg.total_tokens == 0  # Sumatoria aritmética es 0
    assert agg.unknown_token_events_count == 1  # Registro explícito de que hubo 1 evento con UNKNOWN


# =========================================================================
# 9. estimated vs actual cost distinct
# =========================================================================
def test_estimated_vs_actual_cost_distinct(now_utc, tenant_a_context, in_memory_service):
    event = UsageEvent(
        usage_event_id="use_evt_009",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=100,
        output_tokens=50,
        estimated_cost=Decimal("0.0075"),
        actual_cost=None,  # Facturación real pendiente / UNKNOWN
    )
    in_memory_service.record_usage_event(tenant_a_context, event)

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)
    assert agg.total_estimated_cost == Decimal("0.0075")
    assert agg.total_actual_cost == Decimal("0.00")
    assert agg.unknown_actual_cost_events_count == 1  # UNKNOWN actual cost contabilizado


# =========================================================================
# 10. cache hit recorded correctly
# =========================================================================
def test_cache_hit_recorded_correctly(now_utc, tenant_a_context, in_memory_service):
    event = UsageEvent(
        usage_event_id="use_evt_010",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.CACHED,
        cache_status=CacheLookupStatus.HIT,
        provider="openai",
        model="gpt-4o",
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        estimated_cost=Decimal("0.00"),
    )
    in_memory_service.record_usage_event(tenant_a_context, event)

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)
    assert agg.total_requests == 1
    assert agg.cached_requests == 1
    assert agg.total_tokens == 0
    assert agg.total_estimated_cost == Decimal("0.00")


# =========================================================================
# 11. failed request recorded
# =========================================================================
def test_failed_request_recorded_safely(now_utc, tenant_a_context, in_memory_service):
    event = UsageEvent(
        usage_event_id="use_evt_011",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.FAILED,
        provider="anthropic",
        model="claude-3-haiku",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        estimated_cost=None,
        details={"error_type": "PROVIDER_TIMEOUT"},
    )
    in_memory_service.record_usage_event(tenant_a_context, event)

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)
    assert agg.total_requests == 1
    assert agg.successful_requests == 0
    assert agg.failed_requests == 1
    assert agg.total_tokens == 0


# =========================================================================
# 12. tenant isolation
# =========================================================================
def test_cross_tenant_isolation(now_utc, tenant_a_context, tenant_b_context, in_memory_service):
    event_a = UsageEvent(
        usage_event_id="use_evt_a",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=100,
        output_tokens=50,
    )
    event_b = UsageEvent(
        usage_event_id="use_evt_b",
        tenant_id="tenant-beta",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=500,
        output_tokens=200,
    )

    in_memory_service.record_usage_event(tenant_a_context, event_a)
    in_memory_service.record_usage_event(tenant_b_context, event_b)

    # Tenant A consulta su uso
    query_a = UsageQuery(tenant_id="tenant-alpha")
    agg_a = in_memory_service.aggregate_usage(tenant_a_context, query_a)
    assert agg_a.total_tokens == 150

    # Tenant B consulta su uso
    query_b = UsageQuery(tenant_id="tenant-beta")
    agg_b = in_memory_service.aggregate_usage(tenant_b_context, query_b)
    assert agg_b.total_tokens == 700

    # Tenant A intenta consultar métricas de Tenant B -> CrossTenantAccessError
    with pytest.raises(CrossTenantAccessError):
        in_memory_service.aggregate_usage(tenant_a_context, query_b)

    # Tenant A intenta registrar evento perteneciente a Tenant B -> CrossTenantAccessError
    with pytest.raises(CrossTenantAccessError):
        in_memory_service.record_usage_event(tenant_a_context, event_b)


# =========================================================================
# 13. aggregation by model
# =========================================================================
def test_aggregation_by_model(now_utc, tenant_a_context, in_memory_service):
    evt1 = UsageEvent(
        usage_event_id="evt_m1",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
    )
    evt2 = UsageEvent(
        usage_event_id="evt_m2",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        model="gpt-4o",
        input_tokens=200,
        output_tokens=100,
    )
    evt3 = UsageEvent(
        usage_event_id="evt_m3",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        model="claude-3-5-sonnet",
        input_tokens=500,
        output_tokens=200,
    )

    in_memory_service.record_usage_event(tenant_a_context, evt1)
    in_memory_service.record_usage_event(tenant_a_context, evt2)
    in_memory_service.record_usage_event(tenant_a_context, evt3)

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)

    assert len(agg.breakdown_by_model) == 2
    assert agg.breakdown_by_model["gpt-4o"].total_requests == 2
    assert agg.breakdown_by_model["gpt-4o"].total_tokens == 450
    assert agg.breakdown_by_model["claude-3-5-sonnet"].total_requests == 1
    assert agg.breakdown_by_model["claude-3-5-sonnet"].total_tokens == 700


# =========================================================================
# 14. aggregation by user / identity
# =========================================================================
def test_aggregation_by_user_identity(now_utc, tenant_a_context, in_memory_service):
    evt1 = UsageEvent(
        usage_event_id="evt_u1",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        identity_id="usr_alice",
        input_tokens=100,
        output_tokens=50,
    )
    evt2 = UsageEvent(
        usage_event_id="evt_u2",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        identity_id="usr_bob",
        input_tokens=300,
        output_tokens=150,
    )

    in_memory_service.record_usage_event(tenant_a_context, evt1)
    in_memory_service.record_usage_event(tenant_a_context, evt2)

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)

    assert "usr_alice" in agg.breakdown_by_identity
    assert agg.breakdown_by_identity["usr_alice"].total_tokens == 150
    assert "usr_bob" in agg.breakdown_by_identity
    assert agg.breakdown_by_identity["usr_bob"].total_tokens == 450


# =========================================================================
# 15. period boundaries deterministic
# =========================================================================
def test_period_boundaries_deterministic(now_utc, tenant_a_context, in_memory_service):
    p_start = now_utc
    p_end = now_utc + timedelta(hours=1)
    period = UsagePeriod(start_time=p_start, end_time=p_end, period_type=UsagePeriodType.HOUR)

    # Evento dentro de la ventana: occurred_at == p_start (inclusivo)
    evt_inside_start = UsageEvent(
        usage_event_id="evt_p1",
        tenant_id="tenant-alpha",
        occurred_at=p_start,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=100,
        output_tokens=50,
    )
    # Evento dentro de la ventana: occurred_at == p_start + 30m
    evt_inside_mid = UsageEvent(
        usage_event_id="evt_p2",
        tenant_id="tenant-alpha",
        occurred_at=p_start + timedelta(minutes=30),
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=200,
        output_tokens=100,
    )
    # Evento fuera de la ventana: occurred_at == p_end (exclusivo)
    evt_outside_end = UsageEvent(
        usage_event_id="evt_p3",
        tenant_id="tenant-alpha",
        occurred_at=p_end,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=500,
        output_tokens=500,
    )

    in_memory_service.record_usage_event(tenant_a_context, evt_inside_start)
    in_memory_service.record_usage_event(tenant_a_context, evt_inside_mid)
    in_memory_service.record_usage_event(tenant_a_context, evt_outside_end)

    query = UsageQuery(tenant_id="tenant-alpha", period=period)
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)

    assert agg.total_requests == 2
    assert agg.total_tokens == 450


# =========================================================================
# 16. no O.7 quota enforcement
# =========================================================================
def test_no_quota_enforcement_in_metering(now_utc, tenant_a_context, in_memory_service):
    """
    O.6 solo mide y agrega, NUNCA rechaza ni bloquea por exceder umbrales.
    """
    # Registrar un consumo masivo
    massive_event = UsageEvent(
        usage_event_id="evt_massive",
        tenant_id="tenant-alpha",
        occurred_at=now_utc,
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=10_000_000,
        output_tokens=5_000_000,
        estimated_cost=Decimal("1500.00"),
    )
    # Debe ser grabado exitosamente sin lanzar QuotaExceeded o ThrottledError
    recorded = in_memory_service.record_usage_event(tenant_a_context, massive_event)
    assert recorded.total_tokens == 15_000_000

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = in_memory_service.aggregate_usage(tenant_a_context, query)
    assert agg.total_tokens == 15_000_000
    assert agg.total_estimated_cost == Decimal("1500.00")
