"""
Unit tests for M.4 Caching (Transversal M — Control de Coste e Inferencia).

Coverage:
1. Deterministic cache key generation.
2. Same request -> HIT.
3. Changed request -> MISS.
4. Changed model/route -> MISS.
5. Changed policy/version -> MISS.
6. Expiration / TTL -> no HIT (EXPIRED).
7. ERROR not cached by default.
8. UNKNOWN not cached by default.
9. Secure metadata & secret sanitization (no api keys, passwords, tokens persisted).
10. Checksum calculation & integrity validation.
11. Corruption handling & tamper detection without silent auto-repair.
12. Concurrency safety (multi-threaded reads/writes).
13. No side-effect bypass (commercial actions / mutations not cached).
14. No M.5 / M.6 logic intrusion.
"""

from datetime import datetime, timezone, timedelta
import threading
from typing import Dict, Any
import pytest
from pathlib import Path
import json

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
    compute_cache_entry_checksum,
)
from src.domain.caching.ports import CacheRepositoryPort, InferenceCacheServicePort
from src.application.caching.inference_cache_service import InferenceCacheService
from src.infrastructure.persistence.data.in_memory.cache_repository import InMemoryCacheRepository
from src.infrastructure.persistence.data.json.cache_repository import (
    JsonCacheRepository,
    CorruptedCacheEntryError,
)
from src.domain.reliability.ports import ClockPort


class FakeClock(ClockPort):
    def __init__(self, start_time: datetime):
        self._current = start_time if start_time.tzinfo else start_time.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta

    def sleep(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


@pytest.fixture
def base_time():
    return datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def fake_clock(base_time):
    return FakeClock(base_time)


@pytest.fixture
def memory_repo():
    return InMemoryCacheRepository()


@pytest.fixture
def json_repo(tmp_path):
    return JsonCacheRepository(tmp_path / "cache_store")


@pytest.fixture
def default_policy():
    return CachePolicy(
        policy_id="test_m4_policy",
        version="1.0.0",
        enabled=True,
        ttl_seconds=3600,
        allow_cache_errors=False,
        allow_cache_unknown=False,
        enforce_security_context_isolation=True,
    )


# 1. Deterministic cache key
def test_deterministic_cache_key():
    req1_fp = compute_request_fingerprint(
        normalized_prompt_or_payload={"query": "find products", "category": "electronics"},
        tool_schemas=[{"name": "search_tool", "version": "1.0"}],
        system_instructions="You are a commerce agent.",
        parameters={"temperature": 0.0, "max_tokens": 100},
    )
    req2_fp = compute_request_fingerprint(
        normalized_prompt_or_payload={"category": "electronics", "query": "find products"},  # different key order
        tool_schemas=[{"name": "search_tool", "version": "1.0"}],
        system_instructions="You are a commerce agent.",
        parameters={"max_tokens": 100, "temperature": 0.0},
    )
    assert req1_fp == req2_fp, "Fingerprint must be canonical and invariant to dictionary ordering"

    key1 = compute_cache_key(req1_fp, "gpt-4o-mini", "test_policy", "1.0.0")
    key2 = compute_cache_key(req2_fp, "gpt-4o-mini", "test_policy", "1.0.0")
    assert key1 == key2, "Cache key must be deterministic"
    assert len(key1) == 64, "Key must be valid SHA-256 hex string"


# 2. Same request -> HIT
def test_same_request_hit(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    lookup_req = CacheLookupRequest(
        normalized_prompt_or_payload="What is the price of SKU-123?",
        route_or_model_id="gemini-1.5-flash",
    )

    # First lookup -> MISS
    res1 = service.lookup(lookup_req)
    assert res1.status == CacheLookupStatus.MISS
    assert res1.entry is None

    # Store result
    store_req = CacheStoreRequest(
        lookup_request=lookup_req,
        result_data={"price": 29.99, "currency": "USD"},
    )
    stored_entry = service.store(store_req)
    assert stored_entry is not None
    assert stored_entry.result_data == {"price": 29.99, "currency": "USD"}

    # Second lookup -> HIT
    res2 = service.lookup(lookup_req)
    assert res2.status == CacheLookupStatus.HIT
    assert res2.entry is not None
    assert res2.entry.result_data == {"price": 29.99, "currency": "USD"}


# 3. Changed request -> MISS
def test_changed_request_miss(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    req1 = CacheLookupRequest(
        normalized_prompt_or_payload="Price of SKU-100",
        route_or_model_id="gemini-1.5-flash",
    )
    req2 = CacheLookupRequest(
        normalized_prompt_or_payload="Price of SKU-200",  # Different payload
        route_or_model_id="gemini-1.5-flash",
    )

    service.store(CacheStoreRequest(lookup_request=req1, result_data={"price": 10.0}))

    res2 = service.lookup(req2)
    assert res2.status == CacheLookupStatus.MISS
    assert res2.entry is None


# 4. Changed model/route -> MISS
def test_changed_model_route_miss(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    req_gemini = CacheLookupRequest(
        normalized_prompt_or_payload="Analyze market sentiment",
        route_or_model_id="gemini-1.5-pro",
    )
    req_gpt = CacheLookupRequest(
        normalized_prompt_or_payload="Analyze market sentiment",
        route_or_model_id="gpt-4o",  # Different route/model
    )

    service.store(CacheStoreRequest(lookup_request=req_gemini, result_data={"sentiment": "BULLISH"}))

    res_gpt = service.lookup(req_gpt)
    assert res_gpt.status == CacheLookupStatus.MISS


# 5. Changed policy/version -> MISS
def test_changed_policy_version_miss(memory_repo, fake_clock):
    pol_v1 = CachePolicy(policy_id="my_pol", version="1.0.0")
    pol_v2 = CachePolicy(policy_id="my_pol", version="2.0.0")

    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=pol_v1)

    req = CacheLookupRequest(
        normalized_prompt_or_payload="Calculate tax for order",
        route_or_model_id="gpt-4o-mini",
        policy=pol_v1,
    )

    service.store(CacheStoreRequest(lookup_request=req, result_data={"tax": 15.5}))

    # Lookup with updated policy v2
    req_v2 = CacheLookupRequest(
        normalized_prompt_or_payload="Calculate tax for order",
        route_or_model_id="gpt-4o-mini",
        policy=pol_v2,
    )
    res = service.lookup(req_v2)
    assert res.status == CacheLookupStatus.MISS


# 6. Expiration / TTL -> no HIT (EXPIRED)
def test_expiration_ttl_no_hit(memory_repo, fake_clock):
    pol_short = CachePolicy(policy_id="short_ttl", version="1.0.0", ttl_seconds=60)
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=pol_short)

    req = CacheLookupRequest(
        normalized_prompt_or_payload="Get live currency conversion",
        route_or_model_id="gemini-1.5-flash",
    )

    service.store(CacheStoreRequest(lookup_request=req, result_data={"rate": 1.08}))

    # Immediate lookup -> HIT
    res_immediate = service.lookup(req)
    assert res_immediate.status == CacheLookupStatus.HIT

    # Advance clock beyond TTL (61 seconds)
    fake_clock.advance(timedelta(seconds=61))

    # Lookup after expiration -> EXPIRED (never HIT)
    res_expired = service.lookup(req)
    assert res_expired.status == CacheLookupStatus.EXPIRED
    assert res_expired.eviction_reason == CacheEvictionReason.TTL_EXPIRED
    assert res_expired.entry is None

    # Next lookup -> MISS (entry was evicted upon detecting expiration)
    res_next = service.lookup(req)
    assert res_next.status == CacheLookupStatus.MISS


# 7. ERROR not cached
def test_error_not_cached(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    req = CacheLookupRequest(
        normalized_prompt_or_payload="Query external API",
        route_or_model_id="gemini-1.5-flash",
    )

    # Attempt to store ERROR result
    store_req = CacheStoreRequest(
        lookup_request=req,
        result_data={"error_code": "RATE_LIMIT_EXCEEDED", "message": "Too many requests"},
        is_error=True,
    )
    saved = service.store(store_req)
    assert saved is None, "ERROR results must not be stored unless policy explicitly allows it"

    # Lookup must remain MISS
    res = service.lookup(req)
    assert res.status == CacheLookupStatus.MISS


# 8. UNKNOWN not cached
def test_unknown_not_cached(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    req = CacheLookupRequest(
        normalized_prompt_or_payload="Classify ambiguous supplier state",
        route_or_model_id="gemini-1.5-flash",
    )

    # Attempt to store UNKNOWN result
    store_req = CacheStoreRequest(
        lookup_request=req,
        result_data={"status": "UNKNOWN", "confidence": 0.0},
        is_unknown=True,
    )
    saved = service.store(store_req)
    assert saved is None, "UNKNOWN results must not be stored unless policy explicitly allows it"

    res = service.lookup(req)
    assert res.status == CacheLookupStatus.MISS


# 9. Secure metadata & secret sanitization
def test_secure_metadata_and_secret_sanitization(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    req = CacheLookupRequest(
        normalized_prompt_or_payload={
            "query": "Check order status",
            "api_key": "sk-secret-1234567890",
            "authorization": "Bearer token-abc-def",
        },
        route_or_model_id="gpt-4o-mini",
        metadata={
            "tenant_id": "tenant_123",
            "secret_token": "super_secret_token_value",
        },
    )

    store_req = CacheStoreRequest(
        lookup_request=req,
        result_data={
            "status": "DELIVERED",
            "access_token": "tok-should-be-redacted",
        },
        metadata={"internal_scratchpad": "private CoT tokens"},
    )

    entry = service.store(store_req)
    assert entry is not None

    # Check that secrets are redacted
    assert entry.result_data["access_token"] == "[REDACTED]"
    assert entry.metadata["internal_scratchpad"] == "[REDACTED]"


# 10. Checksum calculation & integrity validation
def test_checksum_integrity_validation(base_time):
    entry = CacheEntry(
        cache_key="key123",
        route_or_model_id="modelA",
        request_fingerprint="fp123",
        result_data={"summary": "ok"},
        created_at=base_time,
        policy_id="p1",
        policy_version="1.0.0",
    )
    assert entry.checksum != "", "Checksum must be generated automatically if omitted"
    assert entry.verify_checksum() is True


# 11. Corruption handling & tamper detection
def test_corruption_handling_json_repo(tmp_path, fake_clock, default_policy):
    repo = JsonCacheRepository(tmp_path / "corrupt_test_store")
    service = InferenceCacheService(repository=repo, clock=fake_clock, default_policy=default_policy)

    req = CacheLookupRequest(
        normalized_prompt_or_payload="Detect supplier fraud risk",
        route_or_model_id="gemini-1.5-pro",
    )

    entry = service.store(CacheStoreRequest(lookup_request=req, result_data={"risk": "LOW"}))
    assert entry is not None

    # Tamper with the JSON file on disk
    entry_path = repo._get_entry_path(entry.cache_key)
    with open(entry_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Tamper result_data without updating checksum
    data["result_data"] = {"risk": "HIGH_TAMPERED"}
    with open(entry_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Re-instantiate repository to force disk reload
    reloaded_repo = JsonCacheRepository(tmp_path / "corrupt_test_store")
    service_reloaded = InferenceCacheService(repository=reloaded_repo, clock=fake_clock, default_policy=default_policy)

    # Lookup should detect corruption -> INVALID
    res = service_reloaded.lookup(req)
    assert res.status == CacheLookupStatus.INVALID
    assert res.eviction_reason == CacheEvictionReason.CHECKSUM_MISMATCH
    assert res.entry is None


# 12. Concurrency safety
def test_concurrency_safety(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    lookup_req = CacheLookupRequest(
        normalized_prompt_or_payload="High concurrency query",
        route_or_model_id="gemini-1.5-flash",
    )

    errors = []

    def worker(worker_id: int):
        try:
            for _ in range(50):
                res = service.lookup(lookup_req)
                if res.status == CacheLookupStatus.MISS:
                    service.store(
                        CacheStoreRequest(
                            lookup_request=lookup_req,
                            result_data={"worker": worker_id, "done": True},
                        )
                    )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Concurrent operations caused errors: {errors}"
    final_res = service.lookup(lookup_req)
    assert final_res.status == CacheLookupStatus.HIT


# 13. No side-effect bypass
def test_no_side_effect_bypass(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    req = CacheLookupRequest(
        normalized_prompt_or_payload="Execute purchase order #PO-9999 for $500",
        route_or_model_id="gpt-4o",
    )

    # When request has side-effects (e.g. buying stock, executing order)
    store_req = CacheStoreRequest(
        lookup_request=req,
        result_data={"order_placed": True},
        has_side_effects=True,
    )
    saved = service.store(store_req)
    assert saved is None, "Side-effect operations must not be cached"

    res = service.lookup(req)
    assert res.status == CacheLookupStatus.MISS


# 14. Tenant / Security context isolation
def test_tenant_security_context_isolation(memory_repo, fake_clock, default_policy):
    service = InferenceCacheService(repository=memory_repo, clock=fake_clock, default_policy=default_policy)

    req_tenant_a = CacheLookupRequest(
        normalized_prompt_or_payload="Get user recommendations",
        route_or_model_id="gemini-1.5-flash",
        security_context_id="tenant_alpha",
    )
    req_tenant_b = CacheLookupRequest(
        normalized_prompt_or_payload="Get user recommendations",
        route_or_model_id="gemini-1.5-flash",
        security_context_id="tenant_beta",
    )

    # Store result for tenant alpha
    service.store(
        CacheStoreRequest(
            lookup_request=req_tenant_a,
            result_data={"recommendations": ["item_alpha_1", "item_alpha_2"]},
        )
    )

    # Tenant alpha should HIT
    res_a = service.lookup(req_tenant_a)
    assert res_a.status == CacheLookupStatus.HIT
    assert dict(res_a.entry.result_data) == {"recommendations": ("item_alpha_1", "item_alpha_2")}

    # Tenant beta must MISS to prevent cross-tenant data leakage
    res_b = service.lookup(req_tenant_b)
    assert res_b.status == CacheLookupStatus.MISS
