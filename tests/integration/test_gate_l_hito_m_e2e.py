"""Validación formal E2E de Gate L: coste de inferencia medible y controlable."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.application.audit.audit_trail_service import AuditTrailService
from src.application.caching.inference_cache_service import InferenceCacheService
from src.application.context_budget.context_budget_service import ContextBudgetService
from src.application.context_budget.token_estimator import DeterministicTokenEstimator
from src.application.cost.cost_tracking_service import CostTrackingService
from src.application.cost.pricing_catalog import InMemoryPricingCatalog
from src.application.cost_aware_policy.cost_aware_decision_service import CostAwareDecisionService
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry
from src.application.model_selection.model_selection_service import ModelSelectionByTaskService
from src.application.prompt_compression.deterministic_compressor import DeterministicPromptCompressor
from src.domain.agent_trace.models import StepType, TraceStatus
from src.domain.caching.models import CacheLookupRequest, CacheLookupStatus, CacheStoreRequest
from src.domain.context_budget.models import ContextBudgetPolicy, ContextBudgetRequest, ContextBudgetStatus
from src.domain.cost.models import PricingRate
from src.domain.cost_aware_policy.models import CostAwareDecisionStatus, CostAwareReasonCode, CostAwareRequest
from src.domain.mission.models import Mission, MissionPriority, MissionStatus, MissionType
from src.domain.model_routing.models import (
    LatencyRequirement,
    ModelRoute,
    QualityRequirement,
    RouteCapability,
    RouteStatus,
    RoutingDecisionStatus,
    TaskCriticality,
)
from src.domain.model_selection.models import SelectionStatus, StandardTaskType, TaskSelectionRequest
from src.domain.prompt_compression.models import CompressionRequest, CompressionStatus, RawContextPayload
from src.infrastructure.persistence.data.in_memory.cache_repository import InMemoryCacheRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.cache_repository import JsonCacheRepository
from src.infrastructure.persistence.data.json.cost_repository import JsonCostRepository


def _routes():
    return (
        ModelRoute(
            route_id="cheap-incapable",
            provider="omniroute",
            model_id="cheap-model",
            capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
            quality_class=QualityRequirement.STANDARD,
            context_window=4096,
            priority=1,
        ),
        ModelRoute(
            route_id="commercial-superior",
            provider="omniroute",
            model_id="commercial-model",
            capabilities=(RouteCapability.REASONING, RouteCapability.STRUCTURED_OUTPUT),
            quality_class=QualityRequirement.SUPERIOR,
            context_window=4096,
            priority=2,
        ),
        ModelRoute(
            route_id="degraded-superior",
            provider="omniroute",
            model_id="degraded-model",
            capabilities=(RouteCapability.REASONING, RouteCapability.STRUCTURED_OUTPUT),
            quality_class=QualityRequirement.SUPERIOR,
            status=RouteStatus.DEGRADED,
            context_window=4096,
            priority=3,
        ),
    )


def _catalog(include_commercial=True):
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(
        provider="omniroute", service_or_model="cheap-model", currency="USD",
        input_rate=Decimal("0.01"), output_rate=Decimal("0.02"),
        rate_scale=Decimal("1000000"), version="gate-l",
    ))
    if include_commercial:
        catalog.register_rate(PricingRate(
            provider="omniroute", service_or_model="commercial-model", currency="USD",
            input_rate=Decimal("2.50"), output_rate=Decimal("10.00"),
            rate_scale=Decimal("1000000"), version="gate-l",
        ))
    return catalog


def _select(routes=None):
    routes = routes or _routes()
    registry = InMemoryModelRouteRegistry(routes)
    selector = ModelSelectionByTaskService(
        routing_strategy=DeterministicModelRoutingStrategy(registry), registry=registry,
    )
    result = selector.select_model_for_task(
        TaskSelectionRequest(
            task_type=StandardTaskType.COMMERCIAL_REASONING.value,
            correlation_id="corr-gate-l",
            task_metadata={"api_key": "must-not-leak", "chain_of_thought": "private"},
        ),
        available_routes=routes,
    )
    assert result.status == SelectionStatus.SUCCESS
    assert result.routing_decision.status == RoutingDecisionStatus.SELECTED
    assert result.selected_route.route_id == "commercial-superior"
    assert result.requirements.criticality == TaskCriticality.CRITICAL
    assert result.requirements.min_quality == QualityRequirement.SUPERIOR
    assert result.requirements.required_capabilities == (
        RouteCapability.REASONING, RouteCapability.STRUCTURED_OUTPUT,
    )
    return result


def _payload(large=False, incompressible=False):
    if incompressible:
        return RawContextPayload(system_instructions="S" * 14000, user_input="U" * 14000)
    return RawContextPayload(
        system_instructions="Analyze commercial evidence and return structured output.",
        user_input="Select the commercially valid action.",
        retrieved_evidence=[{"source": "market", "detail": "x" * 300} for _ in range(30 if large else 2)],
        conversation_history=[{"role": "user", "content": "old context " * 40} for _ in range(30 if large else 1)],
    )


def _budget(route, payload):
    estimator = DeterministicTokenEstimator()
    breakdown = estimator.estimate_breakdown(
        system_instructions=payload.system_instructions,
        user_input=payload.user_input,
        retrieved_evidence=payload.retrieved_evidence,
        conversation_history=payload.conversation_history,
        model_id=route.model_id,
    )
    service = ContextBudgetService(
        token_estimator=estimator,
        default_policy=ContextBudgetPolicy(
            policy_id="gate-l-budget", default_reserved_output_tokens=512, safety_margin_tokens=128,
        ),
    )
    return estimator, service, service.assess_budget(ContextBudgetRequest(route=route, input_breakdown=breakdown))


def _cost_request(selection, budget, cache_result=None, compression=None, ceiling=Decimal("0.05")):
    req = selection.requirements
    return CostAwareRequest.from_pipeline(
        task_type=req.task_type,
        criticality=req.criticality,
        min_quality=req.min_quality,
        required_capabilities=req.required_capabilities,
        max_latency=req.latency_requirement,
        eligible_routes=_routes(),
        budget_decision=budget,
        compression_result=compression,
        cache_result=cache_result,
        budget_ceiling=ceiling,
        mission_id="mission-gate-l",
        task_id="task-commercial",
        execution_id="exec-gate-l",
        metadata={"correlation_id": "corr-gate-l", "authorization": "Bearer secret"},
    )


def test_complete_mission_two_cycles_miss_hit_estimated_actual_and_observability(tmp_path):
    mission = Mission(
        mission_id="mission-gate-l", type=MissionType.FULL_OPPORTUNITY_ANALYSIS,
        priority=MissionPriority.CRITICAL, status=MissionStatus.RUNNING,
        parameters={"goal": "commercial decision"},
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    audit = AuditTrailService(JsonAuditRepository(tmp_path / "audit"))
    audit_record = audit.record_mission_created(mission, correlation_id="corr-gate-l")
    trace = AgentTraceService(JsonAgentTraceRepository(tmp_path / "traces"))
    trace_record = trace.record_step(
        component_name="GateLInference", execution_id="exec-gate-l", step_number=1,
        step_type=StepType.SERVICE_CALL, operation="COMMERCIAL_INFERENCE",
        status=TraceStatus.SUCCESS, tool_or_service="omniroute/commercial-model",
        correlation_id="corr-gate-l", causation_id=audit_record.audit_id,
        mission_id=mission.mission_id, metadata={"api_key": "secret", "chain_of_thought": "private"},
    )
    selection = _select()
    estimator, _, budget = _budget(selection.selected_route, _payload())
    assert budget.status == ContextBudgetStatus.WITHIN_BUDGET

    cache = InferenceCacheService(JsonCacheRepository(tmp_path / "cache"))
    lookup = CacheLookupRequest(
        normalized_prompt_or_payload={"mission": mission.mission_id, "task": "commercial decision"},
        route_or_model_id=selection.selected_route.model_id,
        security_context_id="tenant-gate-l",
    )
    miss = cache.lookup(lookup)
    assert miss.status == CacheLookupStatus.MISS
    policy = CostAwareDecisionService(_catalog())
    estimated = policy.evaluate(_cost_request(selection, budget, miss))
    assert estimated.status == CostAwareDecisionStatus.APPROVED
    assert estimated.estimated_cost is not None and estimated.estimated_cost <= estimated.budget_ceiling

    inference = MagicMock(return_value={"decision": "APPROVE", "confidence": "HIGH"})
    result = inference(lookup.normalized_prompt_or_payload)
    cost = CostTrackingService(JsonCostRepository(tmp_path / "costs"), _catalog())
    actual = cost.record_inference_cost(
        execution_id="exec-gate-l", provider=estimated.selected_route.provider,
        model=estimated.selected_route.model_id,
        prompt_tokens=budget.requested_input_tokens - 1, completion_tokens=500,
        trace_id=trace_record.trace_id, mission_id=mission.mission_id,
        correlation_id="corr-gate-l", causation_id=audit_record.audit_id,
        metadata={"estimated_cost": str(estimated.estimated_cost), "token": "secret"},
    )
    cache.store(CacheStoreRequest(lookup_request=lookup, result_data=result))

    restarted_cache = InferenceCacheService(JsonCacheRepository(tmp_path / "cache"))
    hit = restarted_cache.lookup(lookup)
    cached = policy.evaluate(_cost_request(selection, budget, hit))
    assert hit.status == CacheLookupStatus.HIT
    assert cached.status == CostAwareDecisionStatus.APPROVED
    assert cached.estimated_cost == Decimal("0.00") and cached.cache_impact_avoided
    assert inference.call_count == 1
    assert actual.total_cost is not None and actual.total_cost != estimated.estimated_cost
    assert actual.currency == estimated.currency == "USD"
    assert actual.service_or_model == estimated.selected_route.model_id
    assert actual.mission_id == estimated.mission_id == mission.mission_id
    assert actual.correlation_id == "corr-gate-l" and actual.trace_id == trace_record.trace_id
    assert CostTrackingService(JsonCostRepository(tmp_path / "costs"), _catalog()).get_summary(
        mission_id=mission.mission_id
    ).total_records == 1
    persisted = (tmp_path / "traces").read_text() if (tmp_path / "traces").is_file() else ""
    assert "must-not-leak" not in persisted and "Bearer secret" not in persisted


def test_context_over_budget_is_compressed_then_approved():
    selection = _select()
    estimator, budget_service, initial = _budget(selection.selected_route, _payload(large=True))
    assert initial.status == ContextBudgetStatus.OVER_BUDGET
    compression = DeterministicPromptCompressor(estimator).compress_context(CompressionRequest(
        raw_payload=_payload(large=True), target_budget_tokens=initial.available_input_tokens,
        budget_decision=initial, model_id=selection.selected_route.model_id,
    ))
    assert compression.status == CompressionStatus.COMPRESSED
    final = budget_service.assess_budget(ContextBudgetRequest(
        route=selection.selected_route, input_breakdown=compression.final_breakdown,
    ))
    assert final.status == ContextBudgetStatus.WITHIN_BUDGET
    decision = CostAwareDecisionService(_catalog()).evaluate(_cost_request(selection, final, compression=compression))
    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert compression.tokens_saved > 0 and decision.estimated_cost is not None


def test_cannot_compress_blocks_inference():
    selection = _select()
    estimator, _, initial = _budget(selection.selected_route, _payload(incompressible=True))
    assert initial.status == ContextBudgetStatus.OVER_BUDGET
    compression = DeterministicPromptCompressor(estimator).compress_context(CompressionRequest(
        raw_payload=_payload(incompressible=True), target_budget_tokens=initial.available_input_tokens,
        budget_decision=initial, model_id=selection.selected_route.model_id,
    ))
    inference = MagicMock()
    assert compression.status == CompressionStatus.CANNOT_COMPRESS
    if compression.status != CompressionStatus.CANNOT_COMPRESS:
        inference()
    inference.assert_not_called()


def test_budget_ceiling_rejects_without_inference():
    selection = _select()
    _, _, budget = _budget(selection.selected_route, _payload())
    decision = CostAwareDecisionService(_catalog()).evaluate(
        _cost_request(selection, budget, ceiling=Decimal("0.000001"))
    )
    inference = MagicMock()
    if decision.is_approved:
        inference()
    assert decision.status == CostAwareDecisionStatus.REJECTED
    assert CostAwareReasonCode.EXCEEDS_BUDGET.value in decision.reason_codes
    inference.assert_not_called()


def test_quality_and_capability_beat_cheapest_route_for_critical_task():
    selection = _select()
    _, _, budget = _budget(selection.selected_route, _payload())
    decision = CostAwareDecisionService(_catalog()).evaluate(_cost_request(selection, budget))
    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "commercial-superior"
    cheap = next(item for item in decision.route_estimates if item.route_id == "cheap-incapable")
    degraded = next(item for item in decision.route_estimates if item.route_id == "degraded-superior")
    assert not cheap.is_technically_eligible
    assert not degraded.is_technically_eligible


def test_unknown_cost_is_not_zero_or_silently_approved():
    selection = _select()
    _, _, budget = _budget(selection.selected_route, _payload())
    decision = CostAwareDecisionService(_catalog(include_commercial=False)).evaluate(
        _cost_request(selection, budget)
    )
    inference = MagicMock()
    if decision.is_approved:
        inference()
    assert decision.status == CostAwareDecisionStatus.UNKNOWN
    assert decision.estimated_cost is None
    assert CostAwareReasonCode.UNKNOWN_COST.value in decision.reason_codes
    inference.assert_not_called()


def test_restart_routing_budget_policy_determinism_and_context_mismatch_miss(tmp_path):
    first = _select()
    estimator, _, budget = _budget(first.selected_route, _payload())
    first_decision = CostAwareDecisionService(_catalog()).evaluate(_cost_request(first, budget))
    cache = InferenceCacheService(JsonCacheRepository(tmp_path / "cache"))
    request = CacheLookupRequest({"context": "A"}, first.selected_route.model_id)
    cache.store(CacheStoreRequest(request, {"decision": "APPROVE"}))

    second = _select()
    _, _, restarted_budget = _budget(second.selected_route, _payload())
    second_decision = CostAwareDecisionService(_catalog()).evaluate(_cost_request(second, restarted_budget))
    restarted_cache = InferenceCacheService(JsonCacheRepository(tmp_path / "cache"))
    mismatch = restarted_cache.lookup(CacheLookupRequest({"context": "B"}, second.selected_route.model_id))

    assert first.selected_route.route_id == second.selected_route.route_id
    assert budget.calculate_checksum() == restarted_budget.calculate_checksum()
    assert first_decision.calculate_checksum() == second_decision.calculate_checksum()
    assert mismatch.status == CacheLookupStatus.MISS
