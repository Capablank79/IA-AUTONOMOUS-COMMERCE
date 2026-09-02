"""
Suite de Pruebas Unitarias para K.3 Cost Tracking.

Cubre exhaustivamente los casos de prueba requeridos por el Master Prompt:
A. immutable CostRecord
B. inference usage
C. input tokens
D. output tokens
E. token cost calculation
F. request cost
G. Decimal precision
H. pricing lookup
I. pricing version
J. effective pricing
K. UNKNOWN usage
L. UNKNOWN pricing
M. zero cost vs unknown
N. currency
O. multi-currency separation
P. mission aggregation
Q. execution aggregation
R. cycle aggregation
S. provider/model aggregation
T. known total
U. unknown count
V. idempotency
W. replay
X. persistence
Y. restart
Z. AgentTrace linkage
AA. Audit linkage
AB. security
AC. no optimization/Hito M behavior
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import pytest
import tempfile
import uuid

from src.domain.cost.models import (
    CostRecord,
    CostSummary,
    CurrencyCostSummary,
    CostType,
    UsageRecord,
    UsageUnit,
    PricingRate,
)
from src.domain.cost.ports import (
    CostRepositoryPort,
    PricingCatalogPort,
)
from src.application.cost.pricing_catalog import (
    InMemoryPricingCatalog,
    get_default_pricing_catalog,
)
from src.infrastructure.persistence.data.json.cost_repository import (
    JsonCostRepository,
    CorruptedCostRecordError,
)
from src.application.cost.cost_tracking_service import (
    CostTrackingService,
)
from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
)
from src.domain.audit.models import AuditRecordType
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository


# ==============================================================================
# A. IMMUTABLE COSTRECORD
# ==============================================================================
def test_a_immutable_cost_record():
    now = datetime.now(timezone.utc)
    usage = UsageRecord.from_tokens(prompt_tokens=100, completion_tokens=50)
    record = CostRecord(
        cost_id="cst-001",
        occurred_at=now,
        cost_type=CostType.INFERENCE,
        provider="omniroute",
        service_or_model="gpt-4o-mini",
        execution_id="exec-001",
        usage=usage,
        total_cost=Decimal("0.000045"),
        currency="USD",
    )
    with pytest.raises(Exception):
        record.total_cost = Decimal("1.00")  # type: ignore

    with pytest.raises(Exception):
        record.currency = "EUR"  # type: ignore


# ==============================================================================
# B. INFERENCE USAGE
# ==============================================================================
def test_b_inference_usage_normalization():
    usage = UsageRecord.from_tokens(prompt_tokens=1500, completion_tokens=500)
    assert usage.unit == UsageUnit.TOKENS
    assert usage.input_quantity == Decimal("1500")
    assert usage.output_quantity == Decimal("500")
    assert usage.total_quantity == Decimal("2000")


# ==============================================================================
# C. INPUT TOKENS
# ==============================================================================
def test_c_input_tokens_recording():
    usage = UsageRecord.from_tokens(prompt_tokens=2500)
    assert usage.input_quantity == Decimal("2500")
    assert usage.output_quantity is None
    assert usage.total_quantity == Decimal("2500")


# ==============================================================================
# D. OUTPUT TOKENS
# ==============================================================================
def test_d_output_tokens_recording():
    usage = UsageRecord.from_tokens(completion_tokens=800)
    assert usage.input_quantity is None
    assert usage.output_quantity == Decimal("800")
    assert usage.total_quantity == Decimal("800")


# ==============================================================================
# E. TOKEN COST CALCULATION
# ==============================================================================
def test_e_token_cost_calculation():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(
        PricingRate(
            provider="omniroute",
            service_or_model="gpt-4o-mini",
            currency="USD",
            input_rate=Decimal("0.150"),  # $0.15 / 1M tokens
            output_rate=Decimal("0.600"),  # $0.60 / 1M tokens
            rate_scale=Decimal("1000000"),
            version="2024-07",
        )
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        record = service.record_inference_cost(
            execution_id="exec-e",
            provider="omniroute",
            model="gpt-4o-mini",
            prompt_tokens=1000000,
            completion_tokens=500000,
        )
        assert record is not None
        assert record.is_known
        # input: 1M * 0.15/1M = 0.15 USD
        # output: 0.5M * 0.60/1M = 0.30 USD
        # total = 0.45 USD
        assert record.total_cost == Decimal("0.450000")


# ==============================================================================
# F. REQUEST COST
# ==============================================================================
def test_f_request_cost_calculation():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(
        PricingRate(
            provider="mercadolibre",
            service_or_model="search_api",
            currency="USD",
            flat_rate=Decimal("0.005"),  # $0.005 per request
            rate_scale=Decimal("1"),
            version="v1",
        )
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        record = service.record_tool_cost(
            execution_id="exec-f",
            tool_name="search_api",
            provider="mercadolibre",
            request_count=10,
        )
        assert record is not None
        assert record.is_known
        # 10 * 0.005 = 0.050 USD
        assert record.total_cost == Decimal("0.050000")


# ==============================================================================
# G. DECIMAL PRECISION
# ==============================================================================
def test_g_decimal_precision():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(
        PricingRate(
            provider="precise_llm",
            service_or_model="nano-1",
            currency="USD",
            input_rate=Decimal("0.00001234"),
            rate_scale=Decimal("1000"),
            version="1.0",
        )
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        record = service.record_inference_cost(
            execution_id="exec-g",
            provider="precise_llm",
            model="nano-1",
            prompt_tokens=333,
        )
        assert record is not None
        assert isinstance(record.total_cost, Decimal)
        expected = Decimal("333") * Decimal("0.00001234") / Decimal("1000")
        assert record.total_cost == expected


# ==============================================================================
# H. PRICING LOOKUP
# ==============================================================================
def test_h_pricing_lookup():
    catalog = InMemoryPricingCatalog()
    rate = PricingRate(
        provider="custom_provider",
        service_or_model="custom_model",
        currency="EUR",
        flat_rate=Decimal("0.02"),
    )
    catalog.register_rate(rate)

    found = catalog.get_rate(provider="custom_provider", service_or_model="custom_model")
    assert found is not None
    assert found.currency == "EUR"
    assert found.flat_rate == Decimal("0.02")

    not_found = catalog.get_rate(provider="other", service_or_model="other")
    assert not_found is None


# ==============================================================================
# I. PRICING VERSION
# ==============================================================================
def test_i_pricing_version():
    catalog = InMemoryPricingCatalog()
    r1 = PricingRate(
        provider="test",
        service_or_model="m1",
        flat_rate=Decimal("0.01"),
        version="v1.0",
    )
    r2 = PricingRate(
        provider="test",
        service_or_model="m1",
        flat_rate=Decimal("0.02"),
        version="v2.0",
    )
    catalog.register_rate(r1)
    catalog.register_rate(r2)

    latest = catalog.get_rate("test", "m1")
    assert latest is not None
    assert latest.version == "v2.0"
    assert latest.flat_rate == Decimal("0.02")


# ==============================================================================
# J. EFFECTIVE PRICING (TEMPORAL RANGE)
# ==============================================================================
def test_j_effective_pricing():
    t0 = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)

    catalog = InMemoryPricingCatalog()
    catalog.register_rate(
        PricingRate(
            provider="vendor",
            service_or_model="api-x",
            flat_rate=Decimal("0.10"),
            version="old-h1",
            effective_from=t0,
            effective_to=t1,
        )
    )
    catalog.register_rate(
        PricingRate(
            provider="vendor",
            service_or_model="api-x",
            flat_rate=Decimal("0.08"),
            version="new-h2",
            effective_from=t1,
            effective_to=t2,
        )
    )

    # Consulta para fecha en H1
    rate_h1 = catalog.get_rate("vendor", "api-x", at_time=datetime(2025, 3, 1, tzinfo=timezone.utc))
    assert rate_h1 is not None
    assert rate_h1.version == "old-h1"
    assert rate_h1.flat_rate == Decimal("0.10")

    # Consulta para fecha en H2
    rate_h2 = catalog.get_rate("vendor", "api-x", at_time=datetime(2025, 8, 1, tzinfo=timezone.utc))
    assert rate_h2 is not None
    assert rate_h2.version == "new-h2"
    assert rate_h2.flat_rate == Decimal("0.08")


# ==============================================================================
# K. UNKNOWN USAGE
# ==============================================================================
def test_k_unknown_usage():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo)

        # Provider conocido pero usage es None / unknown
        record = service.calculate_and_record(
            cost_type=CostType.INFERENCE,
            provider="omniroute",
            service_or_model="gpt-4o-mini",
            execution_id="exec-k",
            usage=UsageRecord.unknown(),
        )
        assert record is not None
        assert record.is_unknown
        assert record.total_cost is None
        assert record.usage.unit == UsageUnit.UNKNOWN


# ==============================================================================
# L. UNKNOWN PRICING
# ==============================================================================
def test_l_unknown_pricing():
    catalog = InMemoryPricingCatalog()  # Catálogo vacío
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        record = service.record_inference_cost(
            execution_id="exec-l",
            provider="unregistered_provider",
            model="mysterious_model",
            prompt_tokens=500,
            completion_tokens=200,
        )
        assert record is not None
        assert record.is_unknown
        assert record.total_cost is None
        assert record.pricing_source == "UNKNOWN"


# ==============================================================================
# M. ZERO COST VS UNKNOWN
# ==============================================================================
def test_m_zero_cost_vs_unknown():
    catalog = InMemoryPricingCatalog()
    # Registramos una tool explícitamente gratuita ($0.00)
    catalog.register_rate(
        PricingRate(
            provider="internal",
            service_or_model="local_validator",
            currency="USD",
            flat_rate=Decimal("0.00"),
            version="1.0",
        )
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        # Zero cost
        zero_record = service.record_tool_cost(
            execution_id="exec-m1",
            tool_name="local_validator",
            provider="internal",
            request_count=1,
        )
        assert zero_record is not None
        assert zero_record.is_known
        assert not zero_record.is_unknown
        assert zero_record.total_cost == Decimal("0.00")

        # Unknown cost (sin tarifa)
        unknown_record = service.record_tool_cost(
            execution_id="exec-m2",
            tool_name="unregistered_tool",
            provider="internal",
            request_count=1,
        )
        assert unknown_record is not None
        assert unknown_record.is_unknown
        assert not unknown_record.is_known
        assert unknown_record.total_cost is None

        # UNKNOWN != 0.00
        assert zero_record.total_cost != unknown_record.total_cost


# ==============================================================================
# N. CURRENCY EXPLICIT
# ==============================================================================
def test_n_currency_explicit():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(
        PricingRate(
            provider="cl_billing",
            service_or_model="transbank_gateway",
            currency="CLP",
            flat_rate=Decimal("50.00"),
            version="1.0",
        )
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        record = service.record_tool_cost(
            execution_id="exec-n",
            tool_name="transbank_gateway",
            provider="cl_billing",
            request_count=2,
        )
        assert record is not None
        assert record.currency == "CLP"
        assert record.total_cost == Decimal("100.00")


# ==============================================================================
# O. MULTI-CURRENCY SEPARATION
# ==============================================================================
def test_o_multi_currency_separation():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(
        PricingRate(
            provider="usd_prov",
            service_or_model="usd_svc",
            currency="USD",
            flat_rate=Decimal("5.00"),
        )
    )
    catalog.register_rate(
        PricingRate(
            provider="eur_prov",
            service_or_model="eur_svc",
            currency="EUR",
            flat_rate=Decimal("4.00"),
        )
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        service.record_tool_cost(
            execution_id="exec-o",
            tool_name="usd_svc",
            provider="usd_prov",
            mission_id="mission-multi-curr",
        )
        service.record_tool_cost(
            execution_id="exec-o",
            tool_name="eur_svc",
            provider="eur_prov",
            mission_id="mission-multi-curr",
        )

        summary = service.get_summary(mission_id="mission-multi-curr")
        assert summary.total_records == 2
        assert "USD" in summary.by_currency
        assert "EUR" in summary.by_currency
        assert summary.by_currency["USD"].known_total == Decimal("5.00")
        assert summary.by_currency["EUR"].known_total == Decimal("4.00")


# ==============================================================================
# P. MISSION AGGREGATION
# ==============================================================================
def test_p_mission_aggregation():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="p", service_or_model="m", currency="USD", flat_rate=Decimal("1.50")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        service.record_tool_cost(execution_id="ex1", tool_name="m", provider="p", mission_id="msn-alpha")
        service.record_tool_cost(execution_id="ex2", tool_name="m", provider="p", mission_id="msn-alpha")
        service.record_tool_cost(execution_id="ex3", tool_name="m", provider="p", mission_id="msn-beta")

        alpha_summary = service.get_summary(mission_id="msn-alpha")
        assert alpha_summary.total_records == 2
        assert alpha_summary.by_currency["USD"].known_total == Decimal("3.00")


# ==============================================================================
# Q. EXECUTION AGGREGATION
# ==============================================================================
def test_q_execution_aggregation():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="p", service_or_model="m", currency="USD", flat_rate=Decimal("2.00")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        service.record_tool_cost(execution_id="exec-target", tool_name="m", provider="p", trace_id="trc-1")
        service.record_tool_cost(execution_id="exec-target", tool_name="m", provider="p", trace_id="trc-2")
        service.record_tool_cost(execution_id="exec-other", tool_name="m", provider="p", trace_id="trc-3")

        exec_summary = service.get_summary(execution_id="exec-target")
        assert exec_summary.total_records == 2
        assert exec_summary.by_currency["USD"].known_total == Decimal("4.00")


# ==============================================================================
# R. CYCLE AGGREGATION
# ==============================================================================
def test_r_cycle_aggregation():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="p", service_or_model="m", currency="USD", flat_rate=Decimal("1.25")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        service.record_tool_cost(execution_id="ex1", tool_name="m", provider="p", cycle_id="cycle-101")
        service.record_tool_cost(execution_id="ex2", tool_name="m", provider="p", cycle_id="cycle-101")

        summary = service.get_summary(cycle_id="cycle-101")
        assert summary.total_records == 2
        assert summary.by_currency["USD"].known_total == Decimal("2.50")


# ==============================================================================
# S. PROVIDER / MODEL AGGREGATION
# ==============================================================================
def test_s_provider_model_aggregation():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="prov-a", service_or_model="mod-1", currency="USD", flat_rate=Decimal("1.00")))
    catalog.register_rate(PricingRate(provider="prov-b", service_or_model="mod-2", currency="USD", flat_rate=Decimal("3.00")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        service.record_tool_cost(execution_id="ex1", tool_name="mod-1", provider="prov-a", mission_id="msn-s")
        service.record_tool_cost(execution_id="ex1", tool_name="mod-2", provider="prov-b", mission_id="msn-s")

        records_a = repo.list_records(provider="prov-a")
        assert len(records_a) == 1
        assert records_a[0].provider == "prov-a"

        records_b = repo.list_records(provider="prov-b")
        assert len(records_b) == 1
        assert records_b[0].provider == "prov-b"


# ==============================================================================
# T. KNOWN TOTAL
# ==============================================================================
def test_t_known_total():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="p", service_or_model="m", currency="USD", flat_rate=Decimal("10.50")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        service.record_tool_cost(execution_id="ex-t1", tool_name="m", provider="p", mission_id="msn-t")
        service.record_tool_cost(execution_id="ex-t2", tool_name="m", provider="p", mission_id="msn-t")

        summary = service.get_summary(mission_id="msn-t")
        assert summary.total_known_records == 2
        assert summary.total_unknown_records == 0
        assert summary.by_currency["USD"].known_total == Decimal("21.00")


# ==============================================================================
# U. UNKNOWN COUNT
# ==============================================================================
def test_u_unknown_count():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="known_prov", service_or_model="known_svc", currency="USD", flat_rate=Decimal("1.00")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        # 1 known, 2 unknown
        service.record_tool_cost(execution_id="ex1", tool_name="known_svc", provider="known_prov", mission_id="msn-u")
        service.record_tool_cost(execution_id="ex1", tool_name="unknown_svc1", provider="unknown_prov", mission_id="msn-u")
        service.record_tool_cost(execution_id="ex1", tool_name="unknown_svc2", provider="unknown_prov", mission_id="msn-u")

        summary = service.get_summary(mission_id="msn-u")
        assert summary.total_records == 3
        assert summary.total_known_records == 1
        assert summary.total_unknown_records == 2
        assert summary.by_currency["USD"].known_total == Decimal("1.00")
        assert summary.by_currency["USD"].unknown_record_count == 2


# ==============================================================================
# V. IDEMPOTENCY
# ==============================================================================
def test_v_idempotency():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="prov", service_or_model="mod", currency="USD", flat_rate=Decimal("2.50")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        idemp_key = "idemp-unique-call-123"
        r1 = service.calculate_and_record(
            cost_type=CostType.TOOL_CALL,
            provider="prov",
            service_or_model="mod",
            execution_id="exec-v",
            idempotency_key=idemp_key,
        )
        r2 = service.calculate_and_record(
            cost_type=CostType.TOOL_CALL,
            provider="prov",
            service_or_model="mod",
            execution_id="exec-v",
            idempotency_key=idemp_key,
        )

        assert r1 is not None
        assert r2 is not None
        assert r1.cost_id == r2.cost_id

        all_records = repo.list_records(execution_id="exec-v")
        assert len(all_records) == 1


# ==============================================================================
# W. REPLAY SAFETY
# ==============================================================================
def test_w_replay_safety():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="p", service_or_model="m", currency="USD", flat_rate=Decimal("5.00")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        trace_id = "trc-replay-001"
        exec_id = "exec-replay-001"

        r1 = service.record_tool_cost(execution_id=exec_id, tool_name="m", provider="p", trace_id=trace_id)
        r2 = service.record_tool_cost(execution_id=exec_id, tool_name="m", provider="p", trace_id=trace_id)

        assert r1.cost_id == r2.cost_id
        summary = service.get_summary(execution_id=exec_id)
        assert summary.total_records == 1
        assert summary.by_currency["USD"].known_total == Decimal("5.00")


# ==============================================================================
# X. PERSISTENCE DURABLE (ATOMIC & CHECKSUM)
# ==============================================================================
def test_x_persistence_durable():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        record = CostRecord(
            cost_id="cst-durable-1",
            occurred_at=datetime.now(timezone.utc),
            cost_type=CostType.INFERENCE,
            provider="omniroute",
            service_or_model="gpt-4o-mini",
            execution_id="exec-dur",
            usage=UsageRecord.from_tokens(prompt_tokens=100, completion_tokens=50),
            total_cost=Decimal("0.000045"),
            currency="USD",
        )
        saved = repo.append(record)
        assert saved.checksum is not None

        file_path = Path(tmpdir) / "costs" / "cst-durable-1.json"
        assert file_path.exists()

        loaded = repo.get_by_id("cst-durable-1")
        assert loaded is not None
        assert loaded.cost_id == "cst-durable-1"
        assert loaded.total_cost == Decimal("0.000045")
        assert loaded.checksum == saved.checksum


# ==============================================================================
# Y. RESTART / RELOAD
# ==============================================================================
def test_y_restart_reload():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="p", service_or_model="m", currency="USD", flat_rate=Decimal("3.50")))
    with tempfile.TemporaryDirectory() as tmpdir:
        # Sesión 1: Crear registros
        repo1 = JsonCostRepository(tmpdir)
        service1 = CostTrackingService(cost_repository=repo1, pricing_catalog=catalog)
        service1.record_tool_cost(execution_id="exec-restart-1", tool_name="m", provider="p", mission_id="msn-restart")
        service1.record_tool_cost(execution_id="exec-restart-2", tool_name="m", provider="p", mission_id="msn-restart")

        sum1 = service1.get_summary(mission_id="msn-restart")
        assert sum1.total_records == 2
        assert sum1.by_currency["USD"].known_total == Decimal("7.00")

        del service1
        del repo1

        repo2 = JsonCostRepository(tmpdir)
        service2 = CostTrackingService(cost_repository=repo2, pricing_catalog=catalog)

        sum2 = service2.get_summary(mission_id="msn-restart")
        assert sum2.total_records == 2
        assert sum2.by_currency["USD"].known_total == Decimal("7.00")
        assert sum2.records[0].cost_id is not None


# ==============================================================================
# Z. AGENT TRACE LINKAGE
# ==============================================================================
def test_z_agent_trace_linkage():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="omniroute", service_or_model="gpt-4o-mini", currency="USD", flat_rate=Decimal("0.01")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        # Simular un trace de K.2
        trace_record = AgentTraceRecord(
            trace_id="trc-k2-999",
            component_name="OmniRouteDecisionProvider",
            execution_id="exec-k2-link",
            step_number=1,
            step_type=StepType.SERVICE_CALL,
            operation="evaluate_opportunity",
            started_at=datetime.now(timezone.utc),
            status=TraceStatus.SUCCESS,
            mission_id="mission-k2-link",
            correlation_id="corr-k2-link",
        )

        cost_rec = service.record_inference_cost(
            execution_id=trace_record.execution_id,
            provider="omniroute",
            model="gpt-4o-mini",
            trace_id=trace_record.trace_id,
            mission_id=trace_record.mission_id,
            correlation_id=trace_record.correlation_id,
            prompt_tokens=1000,
            completion_tokens=500,
        )

        assert cost_rec is not None
        assert cost_rec.trace_id == trace_record.trace_id
        assert cost_rec.execution_id == trace_record.execution_id
        assert cost_rec.mission_id == trace_record.mission_id
        assert cost_rec.correlation_id == trace_record.correlation_id


# ==============================================================================
# AA. AUDIT LINKAGE
# ==============================================================================
def test_aa_audit_linkage():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="p", service_or_model="m", currency="USD", flat_rate=Decimal("0.50")))
    with tempfile.TemporaryDirectory() as tmpdir:
        cost_repo = JsonCostRepository(Path(tmpdir) / "costs")
        audit_repo = JsonAuditRepository(Path(tmpdir) / "audit")
        service = CostTrackingService(
            cost_repository=cost_repo,
            pricing_catalog=catalog,
            audit_repository=audit_repo,
        )

        cost_rec = service.record_tool_cost(
            execution_id="exec-audit-link",
            tool_name="m",
            provider="p",
            mission_id="msn-audit-link",
        )
        assert cost_rec is not None

        audit_records = audit_repo.list_records(correlation_id="exec-audit-link")
        assert len(audit_records) >= 1
        assert any(r.action_or_operation == "COST_RECORDED" for r in audit_records)


# ==============================================================================
# AB. SECURITY (RECURSIVE METADATA SANITIZATION & NO SECRETS)
# ==============================================================================
def test_ab_security_redaction():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="p", service_or_model="m", currency="USD", flat_rate=Decimal("0.10")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        record = service.calculate_and_record(
            cost_type=CostType.EXTERNAL_API,
            provider="p",
            service_or_model="m",
            execution_id="exec-sec",
            metadata={
                "api_key": "sk-1234567890abcdef",
                "auth_token": "bearer xyz987",
                "safe_field": "public_data",
                "nested": {
                    "password": "super_secret_pw",
                    "reasoning": "private chain of thought should not be saved",
                }
            },
        )
        assert record is not None
        assert record.metadata["api_key"] == "[REDACTED]"
        assert record.metadata["safe_field"] == "public_data"
        assert record.metadata["nested"]["password"] == "[REDACTED]"
        assert record.metadata["nested"]["reasoning"] == "[REDACTED]"

        raw_json = (Path(tmpdir) / "costs" / f"{record.cost_id}.json").read_text()
        assert "sk-1234567890abcdef" not in raw_json
        assert "super_secret_pw" not in raw_json
        assert "[REDACTED]" in raw_json


# ==============================================================================
# AC. NO OPTIMIZATION / HITO M BEHAVIOR
# ==============================================================================
def test_ac_no_optimization_or_budget_enforcement():
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(provider="costly", service_or_model="huge-model", currency="USD", flat_rate=Decimal("1000.00")))
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = JsonCostRepository(tmpdir)
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        record = service.record_tool_cost(
            execution_id="exec-ac",
            tool_name="huge-model",
            provider="costly",
            request_count=1,
        )
        assert record is not None
        assert record.total_cost == Decimal("1000.00")
        assert not hasattr(service, "enforce_budget")
        assert not hasattr(service, "optimize_model_selection")
        assert not hasattr(service, "cache_prompt")
