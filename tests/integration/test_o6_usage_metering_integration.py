"""
Tests de Integración y E2E para Usage Metering SaaS (Hito O.6 — Usage Metering).

Escenarios Obligatorios:
A. O.5 successful inference -> one usage event recorded.
B. multiple requests same tenant/model -> aggregate correctly.
C. Tenant A/B same model -> separate totals (cross-tenant isolation).
D. same tenant different users -> separate user aggregates.
E. cache HIT -> request recorded without false provider usage.
F. provider failure -> failed request recorded safely without falsifying tokens/cost.
G. retry/correlation -> no accidental double count (strict idempotency).
H. restart -> totals reproducible across repository reloads.
I. tampered event -> integrity failure detected, not silently counted.
J. Audit/Trace safe.
K. E2E Multi-tenant flow (Tenant A: 3 requests, Tenant B: 2 requests -> exact independent totals).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import json
import pytest

from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.caching.models import CacheLookupStatus
from src.domain.model_routing.models import ModelRoute, RouteCapability, RouteStatus
from src.domain.model_gateway.models import (
    ModelGatewayRequest,
    ModelGatewayResponse,
    ModelGatewayStatus,
    ProviderRequestReference,
    ProviderErrorType,
)
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageQuery,
    UsagePeriod,
    UsagePeriodType,
    UsageRequestStatus,
    UsageEventIntegrityError,
    UsageEventConflictError,
)
from src.infrastructure.persistence.data.json.usage_event_repository import (
    InMemoryUsageEventRepository,
    JsonUsageEventRepository,
)
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.usage_metering.model_gateway_bridge import ModelGatewayUsageBridge
from src.domain.audit.models import AuditRecord, AuditRecordType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import AgentTraceRecord
from src.domain.agent_trace.ports import AgentTraceRepositoryPort


class InMemoryAuditRepository(AuditRepositoryPort):
    def __init__(self):
        self.records: List[AuditRecord] = []

    def append(self, record: AuditRecord) -> AuditRecord:
        self.records.append(record)
        return record

    def get_by_id(self, audit_id: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.audit_id == audit_id:
                return r
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.idempotency_key == idempotency_key:
                return r
        return None

    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        return [r for r in self.records if (record_type is None or r.record_type == record_type)]

    def reconstruct_mission_timeline(self, mission_id: str):
        raise NotImplementedError()


class InMemoryAgentTraceRepository(AgentTraceRepositoryPort):
    def __init__(self):
        self.records: List[AgentTraceRecord] = []

    def append(self, record: AgentTraceRecord) -> AgentTraceRecord:
        self.records.append(record)
        return record

    def get_by_id(self, trace_id: str) -> Optional[AgentTraceRecord]:
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AgentTraceRecord]:
        return None

    def find_by_mission(self, mission_id: str, limit: int = 100, offset: int = 0) -> List[AgentTraceRecord]:
        return []

    def find_by_step_type(self, step_type: Any, limit: int = 100, offset: int = 0) -> List[AgentTraceRecord]:
        return []

    def count_by_mission(self, mission_id: str) -> int:
        return 0


@pytest.fixture
def now_utc() -> datetime:
    return datetime(2026, 9, 8, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def base_route() -> ModelRoute:
    return ModelRoute(
        route_id="route-openai-gpt4o",
        provider="openai",
        model_id="gpt-4o",
        status=RouteStatus.AVAILABLE,
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        estimated_cost_input_per_million=Decimal("5.00"),
        estimated_cost_output_per_million=Decimal("15.00"),
    )


# =========================================================================
# Escenario A: O.5 successful inference -> one usage event
# =========================================================================
def test_scenario_a_o5_successful_inference_records_one_event(now_utc, base_route):
    context = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryUsageEventRepository()
    service = UsageMeteringService(repository=repo)

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        identity_id="usr_01",
        prompt_payload="Analyze market trends",
    )
    res = ModelGatewayResponse(
        status=ModelGatewayStatus.SUCCESS,
        tenant_id="tenant-alpha",
        route_used=base_route,
        provider_reference=ProviderRequestReference(provider_name="openai", model_id="gpt-4o"),
        input_tokens=150,
        output_tokens=75,
        total_tokens=225,
        estimated_cost=Decimal("0.003375"),
        actual_cost=Decimal("0.003375"),
    )

    event = ModelGatewayUsageBridge.record_gateway_usage(
        usage_service=service,
        context=context,
        request=req,
        response=res,
        occurred_at=now_utc,
    )

    assert event.tenant_id == "tenant-alpha"
    assert event.request_status == UsageRequestStatus.SUCCESS
    assert event.total_tokens == 225
    assert event.estimated_cost == Decimal("0.003375")

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = service.aggregate_usage(context, query)
    assert agg.total_requests == 1
    assert agg.successful_requests == 1
    assert agg.total_tokens == 225


# =========================================================================
# Escenario B: Multiple requests same tenant/model -> aggregate correctly
# =========================================================================
def test_scenario_b_multiple_requests_aggregate_correctly(now_utc, base_route):
    context = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryUsageEventRepository()
    service = UsageMeteringService(repository=repo)

    for i in range(5):
        req = ModelGatewayRequest(
            tenant_id="tenant-alpha",
            identity_id="usr_01",
            prompt_payload=f"Task {i}",
        )
        res = ModelGatewayResponse(
            status=ModelGatewayStatus.SUCCESS,
            tenant_id="tenant-alpha",
            route_used=base_route,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            estimated_cost=Decimal("0.002"),
            actual_cost=Decimal("0.002"),
        )
        ModelGatewayUsageBridge.record_gateway_usage(
            usage_service=service,
            context=context,
            request=req,
            response=res,
            occurred_at=now_utc + timedelta(minutes=i),
        )

    query = UsageQuery(tenant_id="tenant-alpha")
    agg = service.aggregate_usage(context, query)
    assert agg.total_requests == 5
    assert agg.successful_requests == 5
    assert agg.total_input_tokens == 500
    assert agg.total_output_tokens == 250
    assert agg.total_tokens == 750
    assert agg.total_estimated_cost == Decimal("0.010")


# =========================================================================
# Escenario C: Tenant A/B same model -> separate totals
# =========================================================================
def test_scenario_c_cross_tenant_totals_strictly_separated(now_utc, base_route):
    ctx_a = TenantContext(tenant_id="tenant-alpha")
    ctx_b = TenantContext(tenant_id="tenant-beta")
    repo = InMemoryUsageEventRepository()
    service = UsageMeteringService(repository=repo)

    # 3 requests for Tenant A
    for _ in range(3):
        req_a = ModelGatewayRequest(tenant_id="tenant-alpha", identity_id="usr_a", prompt_payload="A prompt")
        res_a = ModelGatewayResponse(
            status=ModelGatewayStatus.SUCCESS,
            tenant_id="tenant-alpha",
            route_used=base_route,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )
        ModelGatewayUsageBridge.record_gateway_usage(service, ctx_a, req_a, res_a, now_utc)

    # 2 requests for Tenant B
    for _ in range(2):
        req_b = ModelGatewayRequest(tenant_id="tenant-beta", identity_id="usr_b", prompt_payload="B prompt")
        res_b = ModelGatewayResponse(
            status=ModelGatewayStatus.SUCCESS,
            tenant_id="tenant-beta",
            route_used=base_route,
            input_tokens=200,
            output_tokens=100,
            total_tokens=300,
        )
        ModelGatewayUsageBridge.record_gateway_usage(service, ctx_b, req_b, res_b, now_utc)

    agg_a = service.aggregate_usage(ctx_a, UsageQuery(tenant_id="tenant-alpha"))
    agg_b = service.aggregate_usage(ctx_b, UsageQuery(tenant_id="tenant-beta"))

    assert agg_a.total_requests == 3
    assert agg_a.total_tokens == 450

    assert agg_b.total_requests == 2
    assert agg_b.total_tokens == 600

    # Intento de acceso cruzado denegado
    with pytest.raises(CrossTenantAccessError):
        service.aggregate_usage(ctx_a, UsageQuery(tenant_id="tenant-beta"))


# =========================================================================
# Escenario D: Same tenant different users -> separate user aggregates
# =========================================================================
def test_scenario_d_separate_user_aggregates(now_utc, base_route):
    ctx = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryUsageEventRepository()
    service = UsageMeteringService(repository=repo)

    # Alice: 2 requests
    for _ in range(2):
        req = ModelGatewayRequest(tenant_id="tenant-alpha", identity_id="usr_alice", prompt_payload="p")
        res = ModelGatewayResponse(status=ModelGatewayStatus.SUCCESS, tenant_id="tenant-alpha", route_used=base_route, total_tokens=100)
        ModelGatewayUsageBridge.record_gateway_usage(service, ctx, req, res, now_utc)

    # Bob: 3 requests
    for _ in range(3):
        req = ModelGatewayRequest(tenant_id="tenant-alpha", identity_id="usr_bob", prompt_payload="p")
        res = ModelGatewayResponse(status=ModelGatewayStatus.SUCCESS, tenant_id="tenant-alpha", route_used=base_route, total_tokens=200)
        ModelGatewayUsageBridge.record_gateway_usage(service, ctx, req, res, now_utc)

    agg = service.aggregate_usage(ctx, UsageQuery(tenant_id="tenant-alpha"))
    assert agg.total_requests == 5
    assert agg.breakdown_by_identity["usr_alice"].total_requests == 2
    assert agg.breakdown_by_identity["usr_alice"].total_tokens == 200
    assert agg.breakdown_by_identity["usr_bob"].total_requests == 3
    assert agg.breakdown_by_identity["usr_bob"].total_tokens == 600


# =========================================================================
# Escenario E: Cache HIT -> request recorded without false provider usage
# =========================================================================
def test_scenario_e_cache_hit_recorded_without_false_provider_usage(now_utc, base_route):
    ctx = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryUsageEventRepository()
    service = UsageMeteringService(repository=repo)

    req = ModelGatewayRequest(tenant_id="tenant-alpha", prompt_payload="Cached query")
    res = ModelGatewayResponse(
        status=ModelGatewayStatus.CACHED,
        tenant_id="tenant-alpha",
        route_used=base_route,
        cache_status=CacheLookupStatus.HIT,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        estimated_cost=Decimal("0.00"),
        actual_cost=Decimal("0.00"),
    )

    event = ModelGatewayUsageBridge.record_gateway_usage(service, ctx, req, res, now_utc)
    assert event.request_status == UsageRequestStatus.CACHED
    assert event.cache_status == CacheLookupStatus.HIT

    agg = service.aggregate_usage(ctx, UsageQuery(tenant_id="tenant-alpha"))
    assert agg.total_requests == 1
    assert agg.cached_requests == 1
    assert agg.total_tokens == 0
    assert agg.total_estimated_cost == Decimal("0.00")


# =========================================================================
# Escenario F: Provider failure -> failed request recorded safely
# =========================================================================
def test_scenario_f_provider_failure_recorded_safely(now_utc, base_route):
    ctx = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryUsageEventRepository()
    service = UsageMeteringService(repository=repo)

    req = ModelGatewayRequest(tenant_id="tenant-alpha", prompt_payload="Failed query")
    res = ModelGatewayResponse(
        status=ModelGatewayStatus.PROVIDER_ERROR,
        tenant_id="tenant-alpha",
        route_used=base_route,
        error_type=ProviderErrorType.TIMEOUT,
        error_message="Provider connection timed out after 30s",
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        estimated_cost=None,
        actual_cost=None,
    )

    event = ModelGatewayUsageBridge.record_gateway_usage(service, ctx, req, res, now_utc)
    assert event.request_status == UsageRequestStatus.FAILED

    agg = service.aggregate_usage(ctx, UsageQuery(tenant_id="tenant-alpha"))
    assert agg.total_requests == 1
    assert agg.successful_requests == 0
    assert agg.failed_requests == 1
    assert agg.total_tokens == 0


# =========================================================================
# Escenario G: Retry / correlation -> no accidental double count
# =========================================================================
def test_scenario_g_retry_correlation_prevents_double_counting(now_utc, base_route):
    ctx = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryUsageEventRepository()
    service = UsageMeteringService(repository=repo)

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        prompt_payload="Retry query",
        correlation_id="cor_retry_100",
    )
    res = ModelGatewayResponse(
        status=ModelGatewayStatus.SUCCESS,
        tenant_id="tenant-alpha",
        route_used=base_route,
        total_tokens=150,
        correlation_id="cor_retry_100",
    )

    # Primer intento
    ModelGatewayUsageBridge.record_gateway_usage(service, ctx, req, res, now_utc, event_id="evt_retry_100")
    # Segundo intento con misma correlación e id
    ModelGatewayUsageBridge.record_gateway_usage(service, ctx, req, res, now_utc, event_id="evt_retry_100")

    agg = service.aggregate_usage(ctx, UsageQuery(tenant_id="tenant-alpha"))
    assert agg.total_requests == 1
    assert agg.total_tokens == 150


# =========================================================================
# Escenario H: Restart -> totals reproducible across repository reloads
# =========================================================================
def test_scenario_h_restart_totals_reproducible(tmp_path, now_utc, base_route):
    storage_dir = tmp_path / "saas_storage"
    ctx = TenantContext(tenant_id="tenant-alpha")

    # 1. Primera instancia del servicio (antes de "restart")
    repo1 = JsonUsageEventRepository(base_storage_dir=storage_dir)
    service1 = UsageMeteringService(repository=repo1)

    for i in range(3):
        req = ModelGatewayRequest(tenant_id="tenant-alpha", prompt_payload=f"q{i}")
        res = ModelGatewayResponse(
            status=ModelGatewayStatus.SUCCESS,
            tenant_id="tenant-alpha",
            route_used=base_route,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            estimated_cost=Decimal("0.002"),
        )
        ModelGatewayUsageBridge.record_gateway_usage(service1, ctx, req, res, now_utc + timedelta(minutes=i))

    agg1 = service1.aggregate_usage(ctx, UsageQuery(tenant_id="tenant-alpha"))
    assert agg1.total_requests == 3
    assert agg1.total_tokens == 450

    # 2. Segunda instancia simulando reinicio de la aplicación
    repo2 = JsonUsageEventRepository(base_storage_dir=storage_dir)
    service2 = UsageMeteringService(repository=repo2)

    agg2 = service2.aggregate_usage(ctx, UsageQuery(tenant_id="tenant-alpha"))
    assert agg2.total_requests == 3
    assert agg2.total_tokens == 450
    assert agg2.total_estimated_cost == Decimal("0.006")
    assert agg2.checksum == agg1.checksum


# =========================================================================
# Escenario I: Tampered event -> integrity failure detected
# =========================================================================
def test_scenario_i_tampered_event_detected_on_disk(tmp_path, now_utc, base_route):
    storage_dir = tmp_path / "saas_storage"
    ctx = TenantContext(tenant_id="tenant-alpha")

    repo = JsonUsageEventRepository(base_storage_dir=storage_dir)
    service = UsageMeteringService(repository=repo)

    req = ModelGatewayRequest(tenant_id="tenant-alpha", prompt_payload="Tamper test")
    res = ModelGatewayResponse(
        status=ModelGatewayStatus.SUCCESS,
        tenant_id="tenant-alpha",
        route_used=base_route,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
    )
    evt = ModelGatewayUsageBridge.record_gateway_usage(service, ctx, req, res, now_utc, event_id="evt_tamper")

    # Modificar maliciosamente el archivo en disco alterando los tokens sin actualizar checksum
    event_file = storage_dir / "tenants" / "tenant-alpha" / "usage" / "events" / "evt_tamper.json"
    with open(event_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["total_tokens"] = 999999
    with open(event_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # La lectura posterior debe fallar por violación de integridad SHA-256
    with pytest.raises(UsageEventIntegrityError):
        service.aggregate_usage(ctx, UsageQuery(tenant_id="tenant-alpha"))


# =========================================================================
# Escenario J: Audit / Trace safe
# =========================================================================
def test_scenario_j_audit_trail_safe_metadata_emission(now_utc, base_route):
    ctx = TenantContext(tenant_id="tenant-alpha")
    repo = InMemoryUsageEventRepository()
    audit_repo = InMemoryAuditRepository()
    service = UsageMeteringService(repository=repo, audit_repository=audit_repo)

    req = ModelGatewayRequest(
        tenant_id="tenant-alpha",
        identity_id="usr_admin",
        prompt_payload="Secret sensitive question",
    )
    res = ModelGatewayResponse(
        status=ModelGatewayStatus.SUCCESS,
        tenant_id="tenant-alpha",
        route_used=base_route,
        input_tokens=120,
        output_tokens=60,
        total_tokens=180,
    )
    ModelGatewayUsageBridge.record_gateway_usage(service, ctx, req, res, now_utc)

    # Verificar que el evento de auditoría fue registrado
    assert len(audit_repo.records) == 1
    rec = audit_repo.records[0]
    assert rec.actor.actor_id == "usr_admin"
    assert rec.metadata["tenant_id"] == "tenant-alpha"
    assert "Secret sensitive question" not in str(rec.metadata)


# =========================================================================
# Escenario K: E2E Multi-tenant flow (Tenant A: 3 reqs, Tenant B: 2 reqs)
# =========================================================================
def test_scenario_k_e2e_multitenant_complete_flow(tmp_path, now_utc, base_route):
    storage_dir = tmp_path / "saas_e2e_storage"
    repo = JsonUsageEventRepository(base_storage_dir=storage_dir)
    service = UsageMeteringService(repository=repo)

    ctx_a = TenantContext(tenant_id="tenant-a")
    ctx_b = TenantContext(tenant_id="tenant-b")

    # Tenant A: 3 requests
    for i in range(3):
        req_a = ModelGatewayRequest(
            tenant_id="tenant-a",
            identity_id=f"user_a_{i%2}",
            prompt_payload=f"Tenant A request {i}",
        )
        res_a = ModelGatewayResponse(
            status=ModelGatewayStatus.SUCCESS,
            tenant_id="tenant-a",
            route_used=base_route,
            input_tokens=100 * (i + 1),
            output_tokens=50 * (i + 1),
            total_tokens=150 * (i + 1),
            estimated_cost=Decimal(f"0.00{i+1}"),
            actual_cost=Decimal(f"0.00{i+1}"),
        )
        ModelGatewayUsageBridge.record_gateway_usage(service, ctx_a, req_a, res_a, now_utc + timedelta(minutes=i))

    # Tenant B: 2 requests
    for j in range(2):
        req_b = ModelGatewayRequest(
            tenant_id="tenant-b",
            identity_id=f"user_b_{j}",
            prompt_payload=f"Tenant B request {j}",
        )
        res_b = ModelGatewayResponse(
            status=ModelGatewayStatus.SUCCESS,
            tenant_id="tenant-b",
            route_used=base_route,
            input_tokens=500,
            output_tokens=250,
            total_tokens=750,
            estimated_cost=Decimal("0.010"),
            actual_cost=Decimal("0.010"),
        )
        ModelGatewayUsageBridge.record_gateway_usage(service, ctx_b, req_b, res_b, now_utc + timedelta(minutes=j))

    # Consultar Tenant A
    agg_a = service.aggregate_usage(ctx_a, UsageQuery(tenant_id="tenant-a"))
    assert agg_a.total_requests == 3
    assert agg_a.total_tokens == (150 + 300 + 450)  # 900 tokens
    assert agg_a.total_estimated_cost == Decimal("0.001") + Decimal("0.002") + Decimal("0.003")
    assert "user_a_0" in agg_a.breakdown_by_identity
    assert "user_a_1" in agg_a.breakdown_by_identity

    # Consultar Tenant B
    agg_b = service.aggregate_usage(ctx_b, UsageQuery(tenant_id="tenant-b"))
    assert agg_b.total_requests == 2
    assert agg_b.total_tokens == 1500  # 750 * 2
    assert agg_b.total_estimated_cost == Decimal("0.020")
    assert "user_b_0" in agg_b.breakdown_by_identity
    assert "user_b_1" in agg_b.breakdown_by_identity
