"""
Pruebas de Integración y End-to-End para Caching de Inferencia (Hito M.4).

Transversal M — Control de Coste e Inferencia.

Escenarios requeridos:
A. M.1 -> M.2 -> M.3 canonical request -> MISS -> mock inference -> store -> repeat -> HIT (no inference call).
B. Same apparent prompt but different route/model -> MISS.
C. Compressed context changes (M.3) -> correct cache key behavior.
D. Expired entry -> MISS / recompute -> fresh store.
E. UNKNOWN / ERROR result -> not reusable.
F. Restart -> cache preserved in durable JSON store.
G. Tampered entry -> corruption detected and safely evicted.
H. Concurrent identical requests -> consistent result / no corruption.
E2E: Mission/Agent Request -> M.1 Routing -> M.2 Budget -> M.3 Compression -> M.4 Cache -> Inference Boundary.
     Verificar contador de llamadas al mock de inferencia (1st = 1 call, 2nd = 0 calls).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import threading
from typing import Dict, Any
from unittest.mock import MagicMock
import pytest

from src.domain.mission.models import LoopDecision, LoopAction, LoopState, MissionStatus
from src.domain.model_routing.models import (
    RoutingDecisionStatus,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RouteCapability,
    RouteStatus,
    ModelRoute,
    RoutingRequest,
    RoutingPolicy,
    RoutingDecision,
)
from src.domain.context_budget.models import (
    ContextBudgetStatus,
    BudgetExclusionReason,
    InputTokensBreakdown,
    ContextBudgetPolicy,
    ContextBudgetRequest,
    ContextBudgetDecision,
)
from src.domain.prompt_compression.models import (
    CompressionStatus,
    ContextComponentType,
    PriorityLevel,
    ContextItem,
    CompressionActionType,
    CompressionAction,
    CompressionPolicy,
    RawContextPayload,
    CompressedContextPayload,
    CompressionRequest,
    CompressionResult,
)
from src.domain.caching.models import (
    CacheLookupStatus,
    CacheEvictionReason,
    CachePolicy,
    CacheEntry,
    CacheLookupRequest,
    CacheLookupResult,
    CacheStoreRequest,
    compute_request_fingerprint,
    compute_cache_key,
)
from src.domain.reliability.ports import ClockPort
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry
from src.application.context_budget.context_budget_service import ContextBudgetService
from src.application.context_budget.token_estimator import DeterministicTokenEstimator
from src.application.prompt_compression.deterministic_compressor import DeterministicPromptCompressor
from src.application.caching.inference_cache_service import InferenceCacheService
from src.infrastructure.persistence.data.in_memory.cache_repository import InMemoryCacheRepository
from src.infrastructure.persistence.data.json.cache_repository import JsonCacheRepository


class IntegrationClock(ClockPort):
    def __init__(self, start_time: datetime):
        self._current = start_time if start_time.tzinfo else start_time.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta

    def sleep(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


@pytest.fixture
def base_clock():
    return IntegrationClock(datetime(2026, 9, 3, 14, 0, 0, tzinfo=timezone.utc))


# ==============================================================================
# Escenario A: Pipeline M.1 -> M.2 -> M.3 -> M.4 -> Mock Inference -> Cache HIT
# ==============================================================================
def test_scenario_a_pipeline_miss_store_hit(base_clock):
    # 1. M.1 Routing Strategy
    route_flash = ModelRoute(
        route_id="route-gemini-flash",
        provider="google",
        model_id="gemini-1.5-flash",
        context_window=16384,
        capabilities=(RouteCapability.STRUCTURED_OUTPUT, RouteCapability.TOOL_USE),
        quality_class=QualityRequirement.STANDARD,
        priority=1,
    )
    registry = InMemoryModelRouteRegistry([route_flash])
    routing_strategy = DeterministicModelRoutingStrategy(registry)
    routing_decision = routing_strategy.route(
        RoutingRequest(
            task_type="market_analysis",
            criticality=TaskCriticality.MEDIUM,
            required_capabilities=(RouteCapability.STRUCTURED_OUTPUT,),
        )
    )
    assert routing_decision.status == RoutingDecisionStatus.SELECTED
    selected_route = routing_decision.selected_route

    # 2. M.2 Budget
    token_estimator = DeterministicTokenEstimator()
    budget_service = ContextBudgetService(token_estimator=token_estimator)
    raw_payload = RawContextPayload(
            system_instructions="You are a market intelligence analyst.",
            user_input="Evaluate competitor pricing for SKU-X100",
        )
    budget_decision = budget_service.assess_budget(
        ContextBudgetRequest(
            route=selected_route,
            input_breakdown=token_estimator.estimate_breakdown(
                system_instructions="You are a market intelligence analyst.",
                user_input="Evaluate competitor pricing for SKU-X100",
            ),
        )
    )
    assert budget_decision.status == ContextBudgetStatus.WITHIN_BUDGET

    # 3. M.3 Compression (Unchanged since within budget)
    compressor = DeterministicPromptCompressor(token_estimator=token_estimator)
    compression_result = compressor.compress_context(
        CompressionRequest(
            raw_payload=raw_payload,
            target_budget_tokens=budget_decision.available_input_tokens,
            model_id=selected_route.model_id,
        )
    )
    assert compression_result.status == CompressionStatus.UNCHANGED

    # 4. M.4 Caching Service
    cache_repo = InMemoryCacheRepository()
    cache_service = InferenceCacheService(repository=cache_repo, clock=base_clock)

    canonical_prompt = f"{compression_result.compressed_payload.system_instructions}\n{compression_result.compressed_payload.user_input}"
    lookup_request = CacheLookupRequest(
        normalized_prompt_or_payload=canonical_prompt,
        route_or_model_id=selected_route.model_id,
        system_instructions="You are a market intelligence analyst.",
    )

    # 1st execution: MISS -> call mock inference -> store
    mock_inference_engine = MagicMock(return_value={"action": "HOLD", "price_target": 45.0})
    res_1 = cache_service.lookup(lookup_request)
    assert res_1.status == CacheLookupStatus.MISS

    inference_out = mock_inference_engine(canonical_prompt)
    assert mock_inference_engine.call_count == 1

    cache_service.store(
        CacheStoreRequest(
            lookup_request=lookup_request,
            result_data=inference_out,
        )
    )

    # 2nd execution: HIT -> NO inference call
    res_2 = cache_service.lookup(lookup_request)
    assert res_2.status == CacheLookupStatus.HIT
    assert res_2.entry is not None
    assert dict(res_2.entry.result_data) == {"action": "HOLD", "price_target": 45.0}
    # Verify mock was NOT called again
    assert mock_inference_engine.call_count == 1


# ==============================================================================
# Escenario B: Same apparent prompt but different route -> MISS
# ==============================================================================
def test_scenario_b_same_prompt_different_route_miss(base_clock):
    cache_repo = InMemoryCacheRepository()
    cache_service = InferenceCacheService(repository=cache_repo, clock=base_clock)

    prompt = "Classify supplier sentiment: 'Fast shipping, high quality'"

    req_route_a = CacheLookupRequest(normalized_prompt_or_payload=prompt, route_or_model_id="gpt-4o-mini")
    req_route_b = CacheLookupRequest(normalized_prompt_or_payload=prompt, route_or_model_id="gemini-1.5-pro")

    cache_service.store(CacheStoreRequest(lookup_request=req_route_a, result_data={"sentiment": "POSITIVE"}))

    # Route A -> HIT
    assert cache_service.lookup(req_route_a).status == CacheLookupStatus.HIT

    # Route B -> MISS (different model route)
    assert cache_service.lookup(req_route_b).status == CacheLookupStatus.MISS


# ==============================================================================
# Escenario C: Compressed context changes (M.3) -> correct cache key behavior
# ==============================================================================
def test_scenario_c_compressed_context_changes_cache_key(base_clock):
    cache_repo = InMemoryCacheRepository()
    cache_service = InferenceCacheService(repository=cache_repo, clock=base_clock)

    prompt_uncompressed = "Context item 1\nContext item 2\nContext item 3\nUser Question"
    prompt_compressed = "Context item 1\nUser Question"

    req_full = CacheLookupRequest(normalized_prompt_or_payload=prompt_uncompressed, route_or_model_id="gpt-4o-mini")
    req_comp = CacheLookupRequest(normalized_prompt_or_payload=prompt_compressed, route_or_model_id="gpt-4o-mini")

    cache_service.store(CacheStoreRequest(lookup_request=req_full, result_data={"analysis": "detailed"}))

    # Full prompt is HIT
    assert cache_service.lookup(req_full).status == CacheLookupStatus.HIT

    # Compressed prompt is distinct -> MISS
    assert cache_service.lookup(req_comp).status == CacheLookupStatus.MISS


# ==============================================================================
# Escenario D: Expired entry -> MISS/recompute -> fresh store
# ==============================================================================
def test_scenario_d_expired_entry_recompute(base_clock):
    cache_repo = InMemoryCacheRepository()
    policy = CachePolicy(policy_id="short_lived", version="1.0.0", ttl_seconds=300)
    cache_service = InferenceCacheService(repository=cache_repo, clock=base_clock, default_policy=policy)

    req = CacheLookupRequest(normalized_prompt_or_payload="Real-time forex rate", route_or_model_id="gpt-4o-mini")

    cache_service.store(CacheStoreRequest(lookup_request=req, result_data={"rate": 1.10}))

    # Before TTL -> HIT
    assert cache_service.lookup(req).status == CacheLookupStatus.HIT

    # Advance beyond 300s
    base_clock.advance(timedelta(seconds=301))

    # Now -> EXPIRED / evicted
    res_exp = cache_service.lookup(req)
    assert res_exp.status == CacheLookupStatus.EXPIRED
    assert res_exp.entry is None

    # Recompute and store updated data
    cache_service.store(CacheStoreRequest(lookup_request=req, result_data={"rate": 1.12}))

    # Now -> HIT with new rate
    res_new = cache_service.lookup(req)
    assert res_new.status == CacheLookupStatus.HIT
    assert dict(res_new.entry.result_data) == {"rate": 1.12}


# ==============================================================================
# Escenario E: UNKNOWN / ERROR result -> not reusable
# ==============================================================================
def test_scenario_e_unknown_and_error_not_reusable(base_clock):
    cache_repo = InMemoryCacheRepository()
    cache_service = InferenceCacheService(repository=cache_repo, clock=base_clock)

    req_err = CacheLookupRequest(normalized_prompt_or_payload="Failing prompt", route_or_model_id="gpt-4o-mini")
    req_unk = CacheLookupRequest(normalized_prompt_or_payload="Ambiguous prompt", route_or_model_id="gpt-4o-mini")

    # Store ERROR
    res_store_err = cache_service.store(
        CacheStoreRequest(lookup_request=req_err, result_data={"error": "TIMEOUT"}, is_error=True)
    )
    assert res_store_err is None
    assert cache_service.lookup(req_err).status == CacheLookupStatus.MISS

    # Store UNKNOWN
    res_store_unk = cache_service.store(
        CacheStoreRequest(lookup_request=req_unk, result_data={"status": "UNKNOWN"}, is_unknown=True)
    )
    assert res_store_unk is None
    assert cache_service.lookup(req_unk).status == CacheLookupStatus.MISS


# ==============================================================================
# Escenario F: Restart -> cache preserved in durable JSON store
# ==============================================================================
def test_scenario_f_durable_json_store_restart(tmp_path, base_clock):
    store_dir = tmp_path / "durable_cache"
    repo1 = JsonCacheRepository(store_dir)
    service1 = InferenceCacheService(repository=repo1, clock=base_clock)

    req = CacheLookupRequest(
        normalized_prompt_or_payload="Extract product specifications for catalog",
        route_or_model_id="gemini-1.5-flash",
    )

    service1.store(CacheStoreRequest(lookup_request=req, result_data={"specs": {"weight": "2kg", "color": "blue"}}))

    # Simulate restart by instantiating new repo and service instances pointing to same directory
    repo2 = JsonCacheRepository(store_dir)
    service2 = InferenceCacheService(repository=repo2, clock=base_clock)

    res = service2.lookup(req)
    assert res.status == CacheLookupStatus.HIT
    assert res.entry is not None
    assert dict(res.entry.result_data["specs"]) == {"weight": "2kg", "color": "blue"}


# ==============================================================================
# Escenario G: Tampered entry -> corruption detected and safely evicted
# ==============================================================================
def test_scenario_g_tampered_entry_eviction(tmp_path, base_clock):
    store_dir = tmp_path / "tamper_cache"
    repo = JsonCacheRepository(store_dir)
    service = InferenceCacheService(repository=repo, clock=base_clock)

    req = CacheLookupRequest(normalized_prompt_or_payload="Security compliance check", route_or_model_id="gpt-4o")

    entry = service.store(CacheStoreRequest(lookup_request=req, result_data={"compliant": True}))
    assert entry is not None

    # Tamper with file
    path = repo._get_entry_path(entry.cache_key)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["result_data"]["compliant"] = False  # Changed without recalculating checksum
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Reload repo
    reloaded_repo = JsonCacheRepository(store_dir)
    reloaded_service = InferenceCacheService(repository=reloaded_repo, clock=base_clock)

    res = reloaded_service.lookup(req)
    assert res.status == CacheLookupStatus.INVALID
    assert res.eviction_reason == CacheEvictionReason.CHECKSUM_MISMATCH

    # Subsequent lookup should be MISS (evicted)
    assert reloaded_service.lookup(req).status == CacheLookupStatus.MISS


# ==============================================================================
# Escenario H: Concurrent identical requests -> consistent result / no corruption
# ==============================================================================
def test_scenario_h_concurrent_identical_requests(tmp_path, base_clock):
    repo = JsonCacheRepository(tmp_path / "concurrent_cache")
    service = InferenceCacheService(repository=repo, clock=base_clock)

    req = CacheLookupRequest(
        normalized_prompt_or_payload="Heavy parallel reasoning task",
        route_or_model_id="gemini-1.5-pro",
    )

    results = []
    errors = []

    def worker(worker_id: int):
        try:
            lookup = service.lookup(req)
            if lookup.status == CacheLookupStatus.MISS:
                service.store(
                    CacheStoreRequest(
                        lookup_request=req,
                        result_data={"winner": "thread_work", "status": "COMPLETED"},
                    )
                )
                results.append("STORED")
            else:
                results.append(lookup.status.value)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Encountered concurrency errors: {errors}"
    final_res = service.lookup(req)
    assert final_res.status == CacheLookupStatus.HIT
    assert dict(final_res.entry.result_data) == {"winner": "thread_work", "status": "COMPLETED"}


# ==============================================================================
# Escenario E2E: Mission loop decision caching
# ==============================================================================
def test_e2e_mission_decision_inference_caching(base_clock):
    """
    Demostrar que una decisión idéntica de misión / agente evita la llamada a inferencia.
    """
    cache_repo = InMemoryCacheRepository()
    cache_service = InferenceCacheService(repository=cache_repo, clock=base_clock)

    # Mock del proveedor de inferencia
    mock_llm_inference = MagicMock()
    mock_llm_inference.return_value = {
        "action": "ANALYZE_MARKET",
        "confidence": 0.95,
        "rationale": "High margin opportunity detected in electronics",
    }

    def execute_agent_step(state_payload: Dict[str, Any]) -> Dict[str, Any]:
        req = CacheLookupRequest(
            normalized_prompt_or_payload=state_payload,
            route_or_model_id="gpt-4o-mini",
            system_instructions="Autonomous commerce decision agent.",
        )
        lookup = cache_service.lookup(req)
        if lookup.status == CacheLookupStatus.HIT:
            return dict(lookup.entry.result_data)

        # Inferencia ejecutada
        out = mock_llm_inference(state_payload)
        cache_service.store(CacheStoreRequest(lookup_request=req, result_data=out))
        return out

    agent_input = {
        "mission_id": "miss-101",
        "inventory_level": 45,
        "competitor_price": 19.99,
        "demand_trend": "GROWING",
    }

    # Step 1: Primera ejecución -> Inferencia ejecutada
    res1 = execute_agent_step(agent_input)
    assert mock_llm_inference.call_count == 1
    assert res1["action"] == "ANALYZE_MARKET"

    # Step 2: Segunda ejecución con exactamente el mismo estado -> Caché HIT (0 inferencias adicionales)
    res2 = execute_agent_step(agent_input)
    assert mock_llm_inference.call_count == 1  # Sigue en 1
    assert res2["action"] == "ANALYZE_MARKET"

    # Step 3: Cambio de estado de mercado -> Inferencia ejecutada
    agent_input_changed = dict(agent_input, competitor_price=17.50)
    res3 = execute_agent_step(agent_input_changed)
    assert mock_llm_inference.call_count == 2  # Subió a 2
    assert res3["action"] == "ANALYZE_MARKET"
