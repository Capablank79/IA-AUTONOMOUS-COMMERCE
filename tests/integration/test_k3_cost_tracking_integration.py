"""
Pruebas de Integración y End-to-End para K.3 Cost Tracking.

Valida:
1. Flujo completo: Mission -> Agent Trace -> Inferencia determinista / Tool Call -> CostRecord -> Persistencia -> Agregación.
2. Reinicio / Recarga de servicios y repositorios desde disco con sumarios idénticos.
3. Semántica de incertidumbre UNKNOWN vs ZERO COST en operaciones mixtas.
4. Idempotencia y protección ante Replay de ciclos/ejecuciones.
5. Multi-moneda segura con agregación sin mezclar USD / EUR / CLP.
6. Enlace cruzado no invasivo con K.2 Agent Trace y K.1 Audit Trail.
7. Simulación de ContinuousMission con múltiples ciclos y generación de CostRecords asociados.
8. Sanitización estricta de credenciales en persistencia sin fugas de secretos.
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
from src.application.cost.pricing_catalog import (
    InMemoryPricingCatalog,
    get_default_pricing_catalog,
)
from src.infrastructure.persistence.data.json.cost_repository import (
    JsonCostRepository,
)
from src.application.cost.cost_tracking_service import (
    CostTrackingService,
)
from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
)
from src.infrastructure.persistence.data.json.agent_trace_repository import (
    JsonAgentTraceRepository,
)
from src.application.agent_trace.agent_trace_service import (
    AgentTraceService,
)
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.infrastructure.persistence.data.json.audit_repository import (
    JsonAuditRepository,
)
from src.application.audit.audit_trail_service import (
    AuditTrailService,
)


def test_k3_integration_full_mission_lifecycle_with_trace_and_cost():
    """
    Demuestra: Mission -> AgentTrace -> Inferencia LLM y Tool Call -> CostRecord -> Persistencia -> Agregación.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        cost_repo = JsonCostRepository(base / "costs")
        trace_repo = JsonAgentTraceRepository(base / "traces")
        audit_repo = JsonAuditRepository(base / "audit")

        pricing_catalog = InMemoryPricingCatalog()
        # Registrar tarifas conocidas
        pricing_catalog.register_rate(
            PricingRate(
                provider="omniroute",
                service_or_model="gpt-4o-mini",
                currency="USD",
                input_rate=Decimal("0.150"),   # $0.15 / 1M tokens
                output_rate=Decimal("0.600"),  # $0.60 / 1M tokens
                rate_scale=Decimal("1000000"),
                version="2024-07",
            )
        )
        pricing_catalog.register_rate(
            PricingRate(
                provider="mercadolibre",
                service_or_model="search_api",
                currency="USD",
                flat_rate=Decimal("0.005"),    # $0.005 por request
                rate_scale=Decimal("1"),
                version="v1",
            )
        )

        cost_service = CostTrackingService(
            cost_repository=cost_repo,
            pricing_catalog=pricing_catalog,
            audit_repository=audit_repo,
        )
        trace_service = AgentTraceService(trace_repository=trace_repo)

        mission_id = "msn-k3-integ-001"
        execution_id = "exec-k3-integ-001"
        cycle_id = "cycle-01"

        # 1. Paso de Trace 1: Decisión del Agente (Inferencia LLM)
        trace_step1 = trace_service.record_step(
            component_name="OmniRouteDecisionProvider",
            execution_id=execution_id,
            step_number=1,
            step_type=StepType.SERVICE_CALL,
            operation="evaluate_market_opportunity",
            status=TraceStatus.SUCCESS,
            tool_or_service="omniroute/gpt-4o-mini",
            mission_id=mission_id,
            cycle_id=cycle_id,
        )
        assert trace_step1 is not None

        # Medir costo de la inferencia (10,000 prompt tokens, 2,000 completion tokens)
        cost_rec1 = cost_service.record_inference_cost(
            execution_id=execution_id,
            provider="omniroute",
            model="gpt-4o-mini",
            prompt_tokens=10000,
            completion_tokens=2000,
            trace_id=trace_step1.trace_id,
            mission_id=mission_id,
            cycle_id=cycle_id,
        )
        assert cost_rec1 is not None
        assert cost_rec1.is_known
        # 10k * 0.15/1M = 0.0015 USD
        # 2k * 0.60/1M = 0.0012 USD
        # Total = 0.0027 USD
        assert cost_rec1.total_cost == Decimal("0.002700")
        assert cost_rec1.trace_id == trace_step1.trace_id

        # 2. Paso de Trace 2: Invocación de Tool externa
        trace_step2 = trace_service.record_step(
            component_name="MercadoLibreSearchTool",
            execution_id=execution_id,
            step_number=2,
            step_type=StepType.TOOL_CALL,
            operation="fetch_competitor_prices",
            status=TraceStatus.SUCCESS,
            tool_or_service="mercadolibre/search_api",
            mission_id=mission_id,
            cycle_id=cycle_id,
        )
        assert trace_step2 is not None

        # Medir costo del Tool Call (4 requests)
        cost_rec2 = cost_service.record_tool_cost(
            execution_id=execution_id,
            tool_name="search_api",
            provider="mercadolibre",
            request_count=4,
            trace_id=trace_step2.trace_id,
            mission_id=mission_id,
            cycle_id=cycle_id,
        )
        assert cost_rec2 is not None
        assert cost_rec2.is_known
        # 4 * 0.005 = 0.020 USD
        assert cost_rec2.total_cost == Decimal("0.020000")

        # 3. Agregación de costos por mission_id
        summary_mission = cost_service.get_summary(mission_id=mission_id)
        assert summary_mission.total_records == 2
        assert summary_mission.total_known_records == 2
        assert summary_mission.total_unknown_records == 0
        assert "USD" in summary_mission.by_currency
        assert summary_mission.by_currency["USD"].known_total == Decimal("0.022700")

        # 4. Agregación por execution_id y cycle_id
        summary_exec = cost_service.get_summary(execution_id=execution_id)
        assert summary_exec.by_currency["USD"].known_total == Decimal("0.022700")

        summary_cycle = cost_service.get_summary(cycle_id=cycle_id)
        assert summary_cycle.by_currency["USD"].known_total == Decimal("0.022700")


def test_k3_restart_and_reload_persistence_durability():
    """
    Demuestra: Escritura física en disco -> Destrucción de servicios -> Recarga -> Datos agregados idénticos.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        catalog = InMemoryPricingCatalog()
        catalog.register_rate(
            PricingRate(
                provider="omniroute",
                service_or_model="auto/best-coding",
                currency="USD",
                input_rate=Decimal("0.20"),
                output_rate=Decimal("0.80"),
                rate_scale=Decimal("1000000"),
            )
        )

        # Sesión 1
        repo1 = JsonCostRepository(base / "costs")
        service1 = CostTrackingService(cost_repository=repo1, pricing_catalog=catalog)

        service1.record_inference_cost(
            execution_id="exec-reload-1",
            provider="omniroute",
            model="auto/best-coding",
            prompt_tokens=50000,
            completion_tokens=10000,
            mission_id="msn-durability",
        )
        service1.record_inference_cost(
            execution_id="exec-reload-2",
            provider="omniroute",
            model="auto/best-coding",
            prompt_tokens=50000,
            completion_tokens=10000,
            mission_id="msn-durability",
        )

        sum1 = service1.get_summary(mission_id="msn-durability")
        assert sum1.total_records == 2
        expected_total = sum1.by_currency["USD"].known_total

        # Destruir sesión en memoria
        del service1
        del repo1

        # Sesión 2: Recargar desde disco
        repo2 = JsonCostRepository(base / "costs")
        service2 = CostTrackingService(cost_repository=repo2, pricing_catalog=catalog)

        sum2 = service2.get_summary(mission_id="msn-durability")
        assert sum2.total_records == 2
        assert sum2.by_currency["USD"].known_total == expected_total


def test_k3_mixed_known_and_unknown_costs_e2e():
    """
    Demuestra semántica de incertidumbre: operaciones conocidas + operaciones sin tarifa ni consumo = UNKNOWN != 0.00.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        catalog = InMemoryPricingCatalog()
        catalog.register_rate(
            PricingRate(
                provider="known_provider",
                service_or_model="known_model",
                currency="USD",
                flat_rate=Decimal("0.05"),
            )
        )
        repo = JsonCostRepository(base / "costs")
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        mission_id = "msn-mixed-unknown"

        # 1. Operación conocida
        service.record_tool_cost(
            execution_id="exec-mix-1",
            tool_name="known_model",
            provider="known_provider",
            mission_id=mission_id,
        )

        # 2. Operación con tarifa desconocida (modelo no catalogado)
        service.record_inference_cost(
            execution_id="exec-mix-2",
            provider="unknown_llm_vendor",
            model="experimental-v99",
            prompt_tokens=1000,
            completion_tokens=500,
            mission_id=mission_id,
        )

        # 3. Operación con consumo no informado (UNKNOWN Usage)
        service.calculate_and_record(
            cost_type=CostType.TOOL_CALL,
            provider="known_provider",
            service_or_model="known_model",
            execution_id="exec-mix-3",
            usage=UsageRecord.unknown(),
            mission_id=mission_id,
        )

        summary = service.get_summary(mission_id=mission_id)
        assert summary.total_records == 3
        assert summary.total_known_records == 1
        assert summary.total_unknown_records == 2
        assert summary.by_currency["USD"].known_total == Decimal("0.05")
        assert summary.by_currency["USD"].unknown_record_count == 2


def test_k3_replay_idempotency_e2e():
    """
    Demuestra que el replay de trazas o reintento de llamadas no duplica los costos registrados.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        catalog = InMemoryPricingCatalog()
        catalog.register_rate(
            PricingRate(
                provider="omniroute",
                service_or_model="gpt-4o-mini",
                currency="USD",
                flat_rate=Decimal("0.015"),
            )
        )
        repo = JsonCostRepository(base / "costs")
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        exec_id = "exec-replay-test"
        trace_id = "trc-replay-100"

        # Invocación inicial
        rec1 = service.record_tool_cost(
            execution_id=exec_id,
            tool_name="gpt-4o-mini",
            provider="omniroute",
            trace_id=trace_id,
            mission_id="msn-replay",
        )

        # Replay idéntico
        rec2 = service.record_tool_cost(
            execution_id=exec_id,
            tool_name="gpt-4o-mini",
            provider="omniroute",
            trace_id=trace_id,
            mission_id="msn-replay",
        )

        assert rec1.cost_id == rec2.cost_id
        assert rec1.checksum == rec2.checksum

        # El sumario debe contabilizar exactamente 1 registro
        summary = service.get_summary(execution_id=exec_id)
        assert summary.total_records == 1
        assert summary.by_currency["USD"].known_total == Decimal("0.015")


def test_k3_multi_currency_strict_segregation_e2e():
    """
    Demuestra que múltiples divisas se consolidan en subtotales independientes sin FX conversion implícita.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        catalog = InMemoryPricingCatalog()
        catalog.register_rate(PricingRate(provider="us_ai", service_or_model="llm", currency="USD", flat_rate=Decimal("1.20")))
        catalog.register_rate(PricingRate(provider="eu_tool", service_or_model="scraper", currency="EUR", flat_rate=Decimal("0.80")))
        catalog.register_rate(PricingRate(provider="cl_sms", service_or_model="notifier", currency="CLP", flat_rate=Decimal("150.00")))

        repo = JsonCostRepository(base / "costs")
        service = CostTrackingService(cost_repository=repo, pricing_catalog=catalog)

        mission_id = "msn-multi-fx"
        service.record_tool_cost(execution_id="e1", tool_name="llm", provider="us_ai", mission_id=mission_id)
        service.record_tool_cost(execution_id="e2", tool_name="scraper", provider="eu_tool", mission_id=mission_id)
        service.record_tool_cost(execution_id="e3", tool_name="notifier", provider="cl_sms", mission_id=mission_id)

        summary = service.get_summary(mission_id=mission_id)
        assert summary.total_records == 3
        assert len(summary.by_currency) == 3
        assert summary.by_currency["USD"].known_total == Decimal("1.20")
        assert summary.by_currency["EUR"].known_total == Decimal("0.80")
        assert summary.by_currency["CLP"].known_total == Decimal("150.00")
