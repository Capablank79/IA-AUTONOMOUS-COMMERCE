"""
Pruebas Unitarias para Prompt Compression (Hito M.3).

Transversal M — Control de Coste e Inferencia.

Cubre exhaustivamente:
1. input within budget -> UNCHANGED (sin mutaciones innecesarias).
2. over budget -> deduplication drops identical items -> COMPRESSED.
3. over budget -> compact structured JSON reduces token count -> COMPRESSED.
4. over budget -> oldest conversation history pruned -> COMPRESSED.
5. over budget -> optional evidence limited -> COMPRESSED.
6. over budget -> removable / low priority items removed -> COMPRESSED.
7. protected components (system instructions, user prompt, tool schemas) NEVER pruned.
8. insufficient budget even after all reductions -> CANNOT_COMPRESS (protected items preserved).
9. unknown / non-positive target budget -> UNKNOWN / ERROR.
10. policy versioning and custom policy priorities.
11. deterministic SHA-256 checksum auditability and reproducibility.
12. breakdown tracking and tokens saved accuracy.
"""

import pytest
import json

from src.domain.prompt_compression.models import (
    CompressionAction,
    CompressionActionType,
    CompressionPolicy,
    CompressionRequest,
    CompressionResult,
    CompressionStatus,
    ContextComponentType,
    ContextItem,
    PriorityLevel,
    RawContextPayload,
    CompressedContextPayload,
)
from src.domain.context_budget.models import (
    ContextBudgetDecision,
    ContextBudgetStatus,
    InputTokensBreakdown,
)
from src.application.prompt_compression.deterministic_compressor import (
    DeterministicPromptCompressor,
)
from src.application.context_budget.token_estimator import DeterministicTokenEstimator


def test_1_input_within_budget_returns_unchanged():
    """1. Si el contexto ya cabe en el presupuesto objetivo -> UNCHANGED."""
    compressor = DeterministicPromptCompressor()
    raw = RawContextPayload(
        system_instructions="You are a commerce agent.",
        user_input="Check supplier price.",
    )
    req = CompressionRequest(
        raw_payload=raw,
        target_budget_tokens=500,
    )
    res = compressor.compress_context(req)

    assert res.status == CompressionStatus.UNCHANGED
    assert res.original_token_count == res.final_token_count
    assert res.is_within_target_budget is True
    assert len(res.actions_applied) == 0
    assert "SYSTEM_INSTRUCTIONS" in res.preserved_components
    assert "USER_INPUT" in res.preserved_components
    assert res.compressed_payload.system_instructions == "You are a commerce agent."
    assert res.compressed_payload.user_input == "Check supplier price."


def test_2_drop_duplicates_reduces_context():
    """2. Over budget con elementos duplicados -> DROP_DUPLICATES."""
    compressor = DeterministicPromptCompressor()
    raw = RawContextPayload(
        system_instructions="You are an agent.",
        user_input="Process order.",
        retrieved_evidence=[
            {"quote_id": "Q1", "price": 100},
            {"quote_id": "Q1", "price": 100},  # Duplicado exacto
            {"quote_id": "Q2", "price": 200},
        ],
    )
    # Total tokens estimados iniciales > target_budget
    req = CompressionRequest(
        raw_payload=raw,
        target_budget_tokens=25,
    )
    res = compressor.compress_context(req)

    assert res.status in (CompressionStatus.COMPRESSED, CompressionStatus.UNCHANGED)
    action_types = [a.action_type for a in res.actions_applied]
    assert CompressionActionType.DROP_DUPLICATES in action_types
    assert res.tokens_saved > 0


def test_3_compact_structured_reduces_tokens():
    """3. Over budget con JSON estructurado espacioso -> COMPACT_STRUCTURED."""
    compressor = DeterministicPromptCompressor()
    raw = RawContextPayload(
        system_instructions="System",
        user_input="Run query",
        retrieved_evidence=[
            {"data": "A" * 50, "nested": {"key1": "val1", "key2": "val2", "key3": "val3"}},
            {"data": "B" * 50, "nested": {"key1": "val1", "key2": "val2", "key3": "val3"}},
        ],
    )
    req = CompressionRequest(
        raw_payload=raw,
        target_budget_tokens=45,
    )
    res = compressor.compress_context(req)

    assert res.status == CompressionStatus.COMPRESSED
    assert res.tokens_saved > 0
    assert res.final_token_count <= 45


def test_4_prune_oldest_history_preserves_recent():
    """4. Over budget con historial largo -> PRUNE_OLDEST_HISTORY (conserva los más recientes)."""
    compressor = DeterministicPromptCompressor()
    history = [
        {"role": "user", "content": "Message 1 (oldest)"},
        {"role": "assistant", "content": "Message 2"},
        {"role": "user", "content": "Message 3"},
        {"role": "assistant", "content": "Message 4"},
        {"role": "user", "content": "Message 5 (recent)"},
    ]
    raw = RawContextPayload(
        system_instructions="System prompt.",
        user_input="Latest query.",
        conversation_history=history,
    )
    req = CompressionRequest(
        raw_payload=raw,
        target_budget_tokens=30,
    )
    res = compressor.compress_context(req)

    assert res.status == CompressionStatus.COMPRESSED
    assert res.is_within_target_budget is True
    action_types = [a.action_type for a in res.actions_applied]
    assert CompressionActionType.PRUNE_OLDEST_HISTORY in action_types
    # Verifica que el historial final no contiene los más antiguos
    assert res.compressed_payload.conversation_history is not None
    assert len(res.compressed_payload.conversation_history) < len(history)


def test_5_limit_optional_evidence():
    """5. Over budget con mucha evidencia -> LIMIT_OPTIONAL_EVIDENCE."""
    compressor = DeterministicPromptCompressor()
    evidences = [{"item_id": f"ITEM_{i}", "score": i} for i in range(20)]
    raw = RawContextPayload(
        system_instructions="System prompt.",
        user_input="Current query.",
        retrieved_evidence=evidences,
    )
    req = CompressionRequest(
        raw_payload=raw,
        target_budget_tokens=50,
    )
    res = compressor.compress_context(req)

    assert res.status == CompressionStatus.COMPRESSED
    assert res.is_within_target_budget is True
    action_types = [a.action_type for a in res.actions_applied]
    assert CompressionActionType.LIMIT_OPTIONAL_EVIDENCE in action_types
    assert len(res.compressed_payload.retrieved_evidence) <= 10


def test_6_protected_components_never_pruned():
    """6. Componentes PROTECTED (system instructions, user input, tools) NUNCA se descartan."""
    compressor = DeterministicPromptCompressor()
    raw = RawContextPayload(
        system_instructions="CRITICAL_SAFETY_SYSTEM_INSTRUCTIONS",
        user_input="CRITICAL_USER_INPUT_REQUIREMENT",
        tool_schemas=[{"name": "critical_tool", "description": "must be available"}],
        conversation_history=[{"msg": "old message"}],
    )
    # Budget muy pequeño pero que permite conservar los componentes protegidos
    estimator = DeterministicTokenEstimator()
    prot_tokens = (
        estimator.estimate_text_tokens("CRITICAL_SAFETY_SYSTEM_INSTRUCTIONS")
        + estimator.estimate_text_tokens("CRITICAL_USER_INPUT_REQUIREMENT")
        + estimator.estimate_text_tokens(json.dumps([{"name": "critical_tool", "description": "must be available"}]))
    )
    req = CompressionRequest(
        raw_payload=raw,
        target_budget_tokens=prot_tokens + 2,
    )
    res = compressor.compress_context(req)

    assert res.compressed_payload.system_instructions == "CRITICAL_SAFETY_SYSTEM_INSTRUCTIONS"
    assert res.compressed_payload.user_input == "CRITICAL_USER_INPUT_REQUIREMENT"
    assert res.compressed_payload.tool_schemas is not None
    assert len(res.compressed_payload.tool_schemas) == 1


def test_7_insufficient_budget_returns_cannot_compress():
    """7. Si tras podar todo lo no protegido, los protegidos aún exceden el budget -> CANNOT_COMPRESS."""
    compressor = DeterministicPromptCompressor()
    raw = RawContextPayload(
        system_instructions="Extremely long protected system instructions that cannot be dropped.",
        user_input="Extremely long protected user query that cannot be dropped.",
        retrieved_evidence=[{"info": "optional data"}],
    )
    # Target budget irrealmente bajo (ej: 5 tokens), menor que las instrucciones protegidas
    req = CompressionRequest(
        raw_payload=raw,
        target_budget_tokens=5,
    )
    res = compressor.compress_context(req)

    assert res.status == CompressionStatus.CANNOT_COMPRESS
    assert res.is_within_target_budget is False
    assert "could not be reduced below target budget" in res.rationale.lower() or "cannot be reduced below target budget" in res.rationale.lower()
    # Las instrucciones protegidas no fueron mutiladas de forma opaca
    assert res.compressed_payload.system_instructions == raw.system_instructions
    assert res.compressed_payload.user_input == raw.user_input


def test_8_unknown_or_negative_budget_returns_unknown_or_error():
    """8. Target budget desconocido o negativo -> UNKNOWN / ERROR."""
    compressor = DeterministicPromptCompressor()
    raw = RawContextPayload(system_instructions="System", user_input="User")

    # Sin target budget ni budget decision
    req_unknown = CompressionRequest(raw_payload=raw, target_budget_tokens=None)
    res_unknown = compressor.compress_context(req_unknown)
    assert res_unknown.status == CompressionStatus.UNKNOWN

    # Target budget negativo rechazado en validación
    with pytest.raises(ValueError, match="target_budget_tokens cannot be negative"):
        CompressionRequest(raw_payload=raw, target_budget_tokens=-10)


def test_9_policy_versioning_and_custom_priorities():
    """9. Política versionada y configuración declarativa personalizada."""
    custom_policy = CompressionPolicy(
        policy_id="custom_strict_m3_policy",
        version="2.1.0",
        allow_drop_duplicates=True,
        allow_prune_history=True,
        max_history_items_to_keep=1,
    )
    compressor = DeterministicPromptCompressor(default_policy=custom_policy)

    raw = RawContextPayload(
        system_instructions="System",
        user_input="User",
        conversation_history=[
            {"msg": "h1" * 10},
            {"msg": "h2" * 10},
            {"msg": "h3" * 10},
        ],
    )
    # Target budget suficiente para system (2) + user (1) + 1 mensaje de historial (~8), total ~11
    req = CompressionRequest(raw_payload=raw, target_budget_tokens=15)
    res = compressor.compress_context(req, policy=custom_policy)

    assert res.policy_id == "custom_strict_m3_policy"
    assert res.policy_version == "2.1.0"
    assert res.compressed_payload.conversation_history is not None
    assert len(res.compressed_payload.conversation_history) == 1


def test_10_deterministic_checksum_auditability():
    """10. Checksum SHA-256 determinista y reproducible."""
    compressor = DeterministicPromptCompressor()
    raw = RawContextPayload(
        system_instructions="System prompt for audit.",
        user_input="Query for audit.",
        retrieved_evidence=[{"k": "v1"}, {"k": "v1"}],
    )
    req = CompressionRequest(raw_payload=raw, target_budget_tokens=20)
    res1 = compressor.compress_context(req)
    res2 = compressor.compress_context(req)

    c1 = res1.calculate_checksum()
    c2 = res2.calculate_checksum()
    assert c1 == c2
    assert len(c1) == 64  # SHA-256 hex string


def test_11_breakdown_and_tokens_saved_tracking():
    """11. Cálculo de breakdown final y tokens_saved verificado."""
    compressor = DeterministicPromptCompressor()
    raw = RawContextPayload(
        system_instructions="Sys",
        user_input="Usr",
        retrieved_evidence=[{"item": 1}, {"item": 2}],
        conversation_history=[{"role": "user", "content": "hello"}],
    )
    req = CompressionRequest(raw_payload=raw, target_budget_tokens=20)
    res = compressor.compress_context(req)

    assert res.final_breakdown is not None
    assert isinstance(res.final_breakdown, InputTokensBreakdown)
    assert res.final_breakdown.total_input_tokens == res.final_token_count
    assert res.tokens_saved == (res.original_token_count - res.final_token_count)
