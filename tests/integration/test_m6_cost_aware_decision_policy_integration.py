"""
Tests de Integración y Flujo E2E para la Política de Decisión Consciente del Coste (Hito M.6).

Escenarios requeridos:
A. M.5 task -> M.1 eligible routes -> M.2 token estimate -> M.6 picks valid route within budget.
B. Cheapest route lacks capability -> Rejected / Capable route selected.
C. All valid routes exceed budget -> REJECTED / NO_ELIGIBLE_OPTION.
D. Unknown pricing -> UNKNOWN.
E. M.3 compression reduces tokens -> Lower estimated cost.
F. M.4 Cache HIT -> Inference avoided / cost impact represented correctly (0.00 incremental).
G. K.3 actual CostRecord linked after mock inference (Estimated vs Actual separation).
E2E M.6 Flow: Mission -> M.5 -> M.1 -> M.2 -> M.3 -> M.4 -> M.6 -> Mock Inference -> K.3 CostRecord.
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.domain.model_selection.models import (
    TaskSelectionRequirements,
    TaskComplexity,
)
from src.domain.model_routing.models import (
    ModelRoute,
    RouteCapability,
    QualityRequirement,
    LatencyRequirement,
    TaskCriticality,
    RouteStatus,
)
from src.domain.context_budget.models import (
    ContextBudgetDecision,
    ContextBudgetStatus,
)
from src.domain.prompt_compression.models import (
    CompressionResult,
    CompressionStatus,
)
from src.domain.caching.models import (
    CacheLookupResult,
    CacheLookupStatus,
    CacheEntry,
)
from src.domain.cost_aware_policy.models import (
    CostAwarePolicy,
    CostAwareRequest,
    CostAwareDecision,
    CostAwareDecisionStatus,
    CostAwareReasonCode,
)
from src.domain.cost.models import (
    PricingRate,
    CostType,
    CostRecord,
    UsageRecord,
    UsageUnit,
)
from src.domain.cost.ports import CostRepositoryPort
from src.application.cost.pricing_catalog import InMemoryPricingCatalog
from src.application.cost.cost_tracking_service import CostTrackingService
from src.application.cost_aware_policy.cost_aware_decision_service import (
    CostAwareDecisionService,
)


class InMemoryCostRepository(CostRepositoryPort):
    def __init__(self):
        self._records = []

    def append(self, record: CostRecord) -> CostRecord:
        for r in self._records:
            if r.cost_id == record.cost_id or r.idempotency_key == record.idempotency_key:
                return r
        self._records.append(record)
        return record

    def get_by_id(self, cost_id: str):
        for r in self._records:
            if r.cost_id == cost_id:
                return r
        return None

    def get_by_idempotency_key(self, idempotency_key: str):
        for r in self._records:
            if r.idempotency_key == idempotency_key:
                return r
        return None

    def list_records(self, **kwargs):
        return list(self._records)

    def get_summary(self, **kwargs):
        from src.domain.cost.models import CostSummary
        return CostSummary.from_records(self._records)


@pytest.fixture
def integrated_setup():
    pricing_catalog = InMemoryPricingCatalog()
    # 1. Mini / Económico
    pricing_catalog.register_rate(
        PricingRate(
            provider="omniroute",
            service_or_model="gpt-4o-mini",
            currency="USD",
            input_rate=Decimal("0.15"),
            output_rate=Decimal("0.60"),
            rate_scale=Decimal("1000000"),
            version="1.0.0",
        )
    )
    # 2. Flagship / Pro
    pricing_catalog.register_rate(
        PricingRate(
            provider="openai",
            service_or_model="gpt-4o",
            currency="USD",
            input_rate=Decimal("2.50"),
            output_rate=Decimal("10.00"),
            rate_scale=Decimal("1000000"),
            version="1.0.0",
        )
    )

    cost_repo = InMemoryCostRepository()
    cost_tracking_service = CostTrackingService(
        cost_repository=cost_repo,
        pricing_catalog=pricing_catalog,
    )
    decision_service = CostAwareDecisionService(
        pricing_catalog=pricing_catalog,
    )

    return {
        "pricing_catalog": pricing_catalog,
        "cost_repo": cost_repo,
        "cost_tracking_service": cost_tracking_service,
        "decision_service": decision_service,
    }


def test_scenario_a_m5_to_m6_standard_flow(integrated_setup):
    """
    Escenario A:
    M.5 task -> M.1 eligible routes -> M.2 token estimate -> M.6 picks valid route within budget.
    """
    # 1. M.5 Task Requirements
    task_req = TaskSelectionRequirements(
        task_type="market_analysis",
        complexity=TaskComplexity.MEDIUM,
        criticality=TaskCriticality.MEDIUM,
        min_quality=QualityRequirement.STANDARD,
        latency_requirement=LatencyRequirement.NORMAL,
        required_capabilities=(RouteCapability.TOOL_USE,),
    )

    # 2. M.1 Routes
    route_mini = ModelRoute(
        route_id="route-mini",
        provider="omniroute",
        model_id="gpt-4o-mini",
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.STANDARD,
    )
    route_pro = ModelRoute(
        route_id="route-pro",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.HIGH,
    )

    # 3. M.2 Context Budget Decision
    budget_decision = ContextBudgetDecision(
        status=ContextBudgetStatus.WITHIN_BUDGET,
        route_id="route-mini",
        model_id="gpt-4o-mini",
        context_window=128000,
        requested_input_tokens=10000,
        reserved_output_tokens=2000,
        safety_margin_tokens=500,
        available_input_tokens=125500,
        estimated_total_tokens=12000,
    )

    # 4. Pipeline M.6 Request
    cost_request = CostAwareRequest.from_pipeline(
        task_type=task_req.task_type,
        criticality=task_req.criticality,
        min_quality=task_req.min_quality,
        required_capabilities=task_req.required_capabilities,
        eligible_routes=(route_pro, route_mini),
        budget_decision=budget_decision,
        budget_ceiling=Decimal("0.05"),
        mission_id="msn-001",
    )

    # 5. M.6 Decision
    decision_service: CostAwareDecisionService = integrated_setup["decision_service"]
    decision = decision_service.evaluate(cost_request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "route-mini"
    # Coste esperado: (10000/1M)*0.15 + (2000/1M)*0.60 = 0.00150 + 0.00120 = 0.00270 USD
    assert decision.estimated_cost == Decimal("0.002700")
    assert CostAwareReasonCode.CHEAPEST_VALID_SELECTED.value in decision.reason_codes


def test_scenario_b_cheapest_lacks_capability(integrated_setup):
    """
    Escenario B:
    Cheapest route lacks capability -> Rejected -> Capable route selected (Quality First).
    """
    cheap_basic = ModelRoute(
        route_id="route-basic",
        provider="omniroute",
        model_id="gpt-4o-mini",
        capabilities=(RouteCapability.TOOL_USE,),  # Carece de VISION
        quality_class=QualityRequirement.STANDARD,
    )
    pro_vision = ModelRoute(
        route_id="route-vision",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.VISION),
        quality_class=QualityRequirement.HIGH,
    )

    budget_decision = ContextBudgetDecision(
        status=ContextBudgetStatus.WITHIN_BUDGET,
        route_id="route-vision",
        model_id="gpt-4o",
        context_window=128000,
        requested_input_tokens=5000,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
        available_input_tokens=126500,
        estimated_total_tokens=6000,
    )

    cost_request = CostAwareRequest.from_pipeline(
        task_type="visual_inspection",
        required_capabilities=(RouteCapability.VISION,),
        eligible_routes=(cheap_basic, pro_vision),
        budget_decision=budget_decision,
        budget_ceiling=Decimal("0.10"),
    )

    decision_service: CostAwareDecisionService = integrated_setup["decision_service"]
    decision = decision_service.evaluate(cost_request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "route-vision"
    basic_est = next(e for e in decision.route_estimates if e.route_id == "route-basic")
    assert basic_est.is_technically_eligible is False
    assert CostAwareReasonCode.CAPABILITY_UNMET.value in basic_est.exclusion_reasons


def test_scenario_c_all_valid_routes_exceed_budget(integrated_setup):
    """
    Escenario C:
    All valid routes exceed budget -> REJECTED.
    """
    route_pro = ModelRoute(
        route_id="route-pro",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.REASONING,),
        quality_class=QualityRequirement.SUPERIOR,
    )

    # 100K in, 50K out -> Coste: (100K/1M)*2.50 + (50K/1M)*10.00 = 0.25 + 0.50 = 0.75 USD
    cost_request = CostAwareRequest(
        task_type="heavy_reasoning",
        required_capabilities=(RouteCapability.REASONING,),
        estimated_input_tokens=100000,
        estimated_output_tokens=50000,
        budget_ceiling=Decimal("0.10"),  # Techo muy bajo ($0.10 < $0.75)
        eligible_routes=(route_pro,),
    )

    decision_service: CostAwareDecisionService = integrated_setup["decision_service"]
    decision = decision_service.evaluate(cost_request)

    assert decision.status == CostAwareDecisionStatus.REJECTED
    assert decision.selected_route is None
    assert decision.estimated_cost == Decimal("0.750000")
    assert CostAwareReasonCode.EXCEEDS_BUDGET.value in decision.reason_codes


def test_scenario_d_unknown_pricing(integrated_setup):
    """
    Escenario D:
    Unknown pricing on critical task -> UNKNOWN.
    """
    mystery_route = ModelRoute(
        route_id="route-mystery",
        provider="unregistered-llm",
        model_id="black-box-1",
        capabilities=(RouteCapability.TOOL_USE,),
    )

    cost_request = CostAwareRequest(
        task_type="secure_banking",
        criticality=TaskCriticality.HIGH,
        estimated_input_tokens=1000,
        estimated_output_tokens=500,
        eligible_routes=(mystery_route,),
    )

    decision_service: CostAwareDecisionService = integrated_setup["decision_service"]
    decision = decision_service.evaluate(cost_request)

    assert decision.status == CostAwareDecisionStatus.UNKNOWN
    assert decision.selected_route is None
    assert decision.estimated_cost is None
    assert CostAwareReasonCode.UNKNOWN_COST.value in decision.reason_codes


def test_scenario_e_m3_compression_reduces_estimated_cost(integrated_setup):
    """
    Escenario E:
    M.3 prompt compression reduces input tokens -> Lower estimated cost in M.6.
    """
    route_pro = ModelRoute(
        route_id="route-pro",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE,),
    )

    # Budget original de 20K tokens
    budget_decision = ContextBudgetDecision(
        status=ContextBudgetStatus.WITHIN_BUDGET,
        route_id="route-pro",
        model_id="gpt-4o",
        context_window=128000,
        requested_input_tokens=20000,
        reserved_output_tokens=2000,
        safety_margin_tokens=500,
        available_input_tokens=105500,
        estimated_total_tokens=22000,
    )
    # Compresión M.3 reduce a 8K tokens
    compression_result = CompressionResult(
        status=CompressionStatus.COMPRESSED,
        original_token_count=20000,
        final_token_count=8000,
        target_budget_tokens=8000,
        compressed_payload=None,
    )

    cost_request = CostAwareRequest.from_pipeline(
        task_type="doc_analysis",
        eligible_routes=(route_pro,),
        budget_decision=budget_decision,
        compression_result=compression_result,
    )

    decision_service: CostAwareDecisionService = integrated_setup["decision_service"]
    decision = decision_service.evaluate(cost_request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert cost_request.estimated_input_tokens == 8000
    assert cost_request.compression_applied is True
    # (8000/1M)*2.50 + (2000/1M)*10.00 = 0.020 + 0.020 = 0.040 USD
    assert decision.estimated_cost == Decimal("0.040000")


def test_scenario_f_m4_cache_hit_avoids_inference(integrated_setup):
    """
    Escenario F:
    M.4 Cache HIT -> Inference avoided -> Incremental cost 0.00.
    """
    route_pro = ModelRoute(
        route_id="route-pro",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE,),
    )
    cache_entry = CacheEntry(
        cache_key="cache-hash-123456",
        route_or_model_id="route-pro",
        request_fingerprint="fp-req-123456",
        result_data={"summary": "cached analysis result"},
        created_at=datetime.now(timezone.utc),
    )
    cache_hit_result = CacheLookupResult(
        status=CacheLookupStatus.HIT,
        cache_key="cache-hash-123456",
        request_fingerprint="fp-req-123456",
        entry=cache_entry,
    )

    cost_request = CostAwareRequest.from_pipeline(
        task_type="catalog_query",
        eligible_routes=(route_pro,),
        cache_result=cache_hit_result,
    )

    decision_service: CostAwareDecisionService = integrated_setup["decision_service"]
    decision = decision_service.evaluate(cost_request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.cache_impact_avoided is True
    assert decision.estimated_cost == Decimal("0.00")
    assert CostAwareReasonCode.CACHE_HIT_AVOIDED.value in decision.reason_codes


def test_scenario_g_and_e2e_mock_flow(integrated_setup):
    """
    Escenario G & E2E Completo:
    Demostrar flujo integral:
    Mission/Task (M.5) -> Routes (M.1) -> Budget (M.2) -> Compression (M.3) -> Cache Check (M.4) ->
    Cost Decision (M.6) -> Mock Inference -> Actual Cost Record (K.3).
    """
    # 1. Tarea de Misión
    mission_id = "msn-commercial-alpha-2026"
    execution_id = "exec-node-777"
    task_type = "competitor_pricing_extraction"

    # 2. Rutas candidatas M.1
    route_mini = ModelRoute(
        route_id="route-mini",
        provider="omniroute",
        model_id="gpt-4o-mini",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        quality_class=QualityRequirement.STANDARD,
    )
    route_pro = ModelRoute(
        route_id="route-pro",
        provider="openai",
        model_id="gpt-4o",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        quality_class=QualityRequirement.HIGH,
    )

    # 3. Context Budget M.2
    budget_decision = ContextBudgetDecision(
        status=ContextBudgetStatus.WITHIN_BUDGET,
        route_id="route-mini",
        model_id="gpt-4o-mini",
        context_window=128000,
        requested_input_tokens=15000,
        reserved_output_tokens=1500,
        safety_margin_tokens=500,
        available_input_tokens=125500,
        estimated_total_tokens=16500,
    )

    # 4. Prompt Compression M.3 (15000 -> 10000 tokens)
    compression_res = CompressionResult(
        status=CompressionStatus.COMPRESSED,
        original_token_count=15000,
        final_token_count=10000,
        target_budget_tokens=10000,
        compressed_payload=None,
    )

    # 5. Cache Lookup M.4 (MISS)
    cache_res = CacheLookupResult(
        status=CacheLookupStatus.MISS,
        cache_key="search-key-hash",
        request_fingerprint="fp-pipeline-req",
    )

    # 6. M.6 Decision Request & Evaluation
    cost_request = CostAwareRequest.from_pipeline(
        task_type=task_type,
        criticality=TaskCriticality.MEDIUM,
        min_quality=QualityRequirement.STANDARD,
        required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        eligible_routes=(route_pro, route_mini),
        budget_decision=budget_decision,
        compression_result=compression_res,
        cache_result=cache_res,
        budget_ceiling=Decimal("0.02"),
        mission_id=mission_id,
        execution_id=execution_id,
    )

    decision_service: CostAwareDecisionService = integrated_setup["decision_service"]
    decision = decision_service.evaluate(cost_request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "route-mini"
    # Coste estimado: (10000/1M)*0.15 + (1500/1M)*0.60 = 0.00150 + 0.00090 = 0.00240 USD
    assert decision.estimated_cost == Decimal("0.002400")

    # 7. Mock Inference Execution (produce uso real observado)
    actual_prompt_tokens = 9850    # ligeramente distinto al estimado
    actual_completion_tokens = 1420 # ligeramente distinto al estimado
    usage = UsageRecord.from_tokens(
        prompt_tokens=actual_prompt_tokens,
        completion_tokens=actual_completion_tokens,
    )

    # 8. K.3 Cost Tracking Recording
    cost_tracking: CostTrackingService = integrated_setup["cost_tracking_service"]
    cost_record = cost_tracking.calculate_and_record(
        cost_type=CostType.INFERENCE,
        provider=decision.selected_route.provider,
        service_or_model=decision.selected_route.model_id,
        execution_id=execution_id,
        mission_id=mission_id,
        usage=usage,
    )

    assert cost_record is not None
    assert cost_record.is_known is True
    # Coste real: (9850/1M)*0.15 + (1420/1M)*0.60 = 0.0014775 + 0.0008520 = 0.0023295 USD
    assert cost_record.total_cost == Decimal("0.00232950")
    # Separación: estimated cost != actual cost
    assert decision.estimated_cost != cost_record.total_cost

    # 9. Vincular registro real en la decisión inmutable
    final_decision = CostAwareDecision(
        status=decision.status,
        selected_route=decision.selected_route,
        estimated_cost=decision.estimated_cost,
        currency=decision.currency,
        budget_ceiling=decision.budget_ceiling,
        task_type=decision.task_type,
        mission_id=decision.mission_id,
        task_id=decision.task_id,
        execution_id=decision.execution_id,
        eligible_routes=decision.eligible_routes,
        route_estimates=decision.route_estimates,
        cache_impact_avoided=decision.cache_impact_avoided,
        policy_id=decision.policy_id,
        policy_version=decision.policy_version,
        reason_codes=decision.reason_codes,
        deterministic_rationale=decision.deterministic_rationale,
        actual_cost_record_id=cost_record.cost_id,
    )

    assert final_decision.actual_cost_record_id == cost_record.cost_id
    assert final_decision.calculate_checksum() is not None
