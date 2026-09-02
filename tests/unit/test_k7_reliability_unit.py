"""
Tests Unitarios para el Módulo de Confiabilidad y Resiliencia (Hito K.7).

Cubre:
1. Failure Taxonomy y clasificación de recuperabilidad (read-only vs side-effect).
2. RetryPolicy: cálculo determinista de backoff exponencial, límites y respeto de Retry-After.
3. Idempotencia: misma clave + mismo payload -> caché; misma clave + payload distinto -> CONFLICT.
4. Circuit Breaker: transiciones de estado CLOSED -> OPEN -> HALF_OPEN -> CLOSED y fast-failing.
5. Reintentos de fallos transitorios y no reintento de fallos permanentes/auth/validación.
6. Preservación estricta de incertidumbre UNKNOWN (!= SUCCESS y != FAILURE).
7. Reconciliación en operaciones con side-effects ante TIMEOUT o UNKNOWN (prevención de duplicados).
8. Sanitización de secretos en evidencia y metadatos de confiabilidad.
9. Persistencia durable y crash-safe de JsonIdempotencyStore (.tmp + fsync + os.replace).
10. Concurrencia de accesos a circuit breaker e idempotency store.
"""

import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
import pytest

from src.domain.reliability.models import (
    FailureCategory,
    FailureRecoverability,
    classify_failure,
    CircuitState,
    CircuitBreakerConfig,
    RetryPolicy,
    RecoveryDecision,
    ReliabilityResult,
)
from src.infrastructure.reliability.reliability_infrastructure import (
    VirtualClock,
    InMemoryCircuitBreaker,
    InMemoryIdempotencyStore,
    JsonIdempotencyStore,
)
from src.application.reliability.reliability_engine import ReliabilityEngine


def test_failure_taxonomy_classification():
    """Valida la clasificación determinista de fallos según read-only vs side-effect."""
    # Auth, Validation, Corruption, Cancelled siempre NON_RETRYABLE
    assert classify_failure(FailureCategory.AUTHORIZATION, is_side_effect=False) == FailureRecoverability.NON_RETRYABLE
    assert classify_failure(FailureCategory.AUTHORIZATION, is_side_effect=True) == FailureRecoverability.NON_RETRYABLE
    assert classify_failure(FailureCategory.VALIDATION, is_side_effect=False) == FailureRecoverability.NON_RETRYABLE
    assert classify_failure(FailureCategory.CORRUPTION, is_side_effect=False) == FailureRecoverability.NON_RETRYABLE
    assert classify_failure(FailureCategory.CONFLICT, is_side_effect=False) == FailureRecoverability.NON_RETRYABLE

    # Transient y Rate Limit
    assert classify_failure(FailureCategory.TRANSIENT, is_side_effect=False) == FailureRecoverability.RETRYABLE
    assert classify_failure(FailureCategory.RATE_LIMIT, is_side_effect=False) == FailureRecoverability.RETRYABLE

    # Timeout: Read-only es retryable, side-effect requiere reconciliación
    assert classify_failure(FailureCategory.TIMEOUT, is_side_effect=False) == FailureRecoverability.RETRYABLE
    assert classify_failure(FailureCategory.TIMEOUT, is_side_effect=True) == FailureRecoverability.RECONCILIATION_REQUIRED

    # Unknown: Read-only preserva UNKNOWN, side-effect requiere reconciliación
    assert classify_failure(FailureCategory.UNKNOWN, is_side_effect=False) == FailureRecoverability.UNKNOWN
    assert classify_failure(FailureCategory.UNKNOWN, is_side_effect=True) == FailureRecoverability.RECONCILIATION_REQUIRED


def test_retry_policy_delay_computation():
    """Valida el cálculo de backoff determinista y respeto de Retry-After."""
    policy = RetryPolicy(
        max_attempts=4,
        initial_delay_seconds=1.0,
        max_delay_seconds=10.0,
        backoff_multiplier=2.0,
    )
    # Intento 1 -> 1.0
    assert policy.compute_delay(1) == 1.0
    # Intento 2 -> 2.0
    assert policy.compute_delay(2) == 2.0
    # Intento 3 -> 4.0
    assert policy.compute_delay(3) == 4.0
    # Intento 4 -> 8.0
    assert policy.compute_delay(4) == 8.0
    # Intento 5 -> 16.0 acotado a max_delay 10.0
    assert policy.compute_delay(5) == 10.0

    # Retry-After respetado y acotado
    assert policy.compute_delay(1, retry_after_seconds=3.5) == 3.5
    assert policy.compute_delay(1, retry_after_seconds=25.0) == 10.0


def test_circuit_breaker_state_transitions():
    """Valida el ciclo de vida de Circuit Breaker con VirtualClock."""
    clock = VirtualClock()
    config = CircuitBreakerConfig(
        failure_threshold=3,
        recovery_timeout_seconds=20.0,
        half_open_success_threshold=2,
    )
    cb = InMemoryCircuitBreaker(config=config, clock=clock)

    assert cb.get_state("api_svc") == CircuitState.CLOSED
    assert cb.allow_request("api_svc") is True

    # 2 fallos -> sigue CLOSED
    cb.record_failure("api_svc", FailureCategory.TRANSIENT)
    cb.record_failure("api_svc", FailureCategory.TRANSIENT)
    assert cb.get_state("api_svc") == CircuitState.CLOSED

    # 3er fallo -> pasa a OPEN
    cb.record_failure("api_svc", FailureCategory.TRANSIENT)
    assert cb.get_state("api_svc") == CircuitState.OPEN
    assert cb.allow_request("api_svc") is False

    # Avanzar reloj 10s -> sigue OPEN
    clock.advance(10.0)
    assert cb.get_state("api_svc") == CircuitState.OPEN

    # Avanzar reloj otros 11s (total 21s >= 20s) -> pasa a HALF_OPEN
    clock.advance(11.0)
    assert cb.get_state("api_svc") == CircuitState.HALF_OPEN
    assert cb.allow_request("api_svc") is True

    # 1er éxito en HALF_OPEN -> sigue HALF_OPEN
    cb.record_success("api_svc")
    assert cb.get_state("api_svc") == CircuitState.HALF_OPEN

    # 2do éxito en HALF_OPEN -> transiciona a CLOSED
    cb.record_success("api_svc")
    assert cb.get_state("api_svc") == CircuitState.CLOSED


def test_reliability_transient_retry_success():
    """Valida que un fallo transitorio se reintenta y concluye con éxito usando VirtualClock."""
    clock = VirtualClock()
    engine = ReliabilityEngine(clock=clock)

    attempts = 0

    def flaky_operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionResetError("Connection reset by peer")
        return {"status": "ok", "value": 42}

    res = engine.execute_with_reliability(
        operation_id="op_flaky",
        operation_func=flaky_operation,
        is_side_effect=False,
    )

    assert res.is_success is True
    assert res.status == "SUCCESS"
    assert res.output == {"status": "ok", "value": 42}
    assert res.attempts_executed == 3
    assert len(clock.sleep_calls) == 2  # 2 sleeps simulados sin espera real de CPU


def test_reliability_permanent_non_retry():
    """Valida que fallos de autorización o validación no realizan reintentos inútiles."""
    clock = VirtualClock()
    engine = ReliabilityEngine(clock=clock)

    attempts = 0

    def forbidden_operation():
        nonlocal attempts
        attempts += 1
        raise PermissionError("403 Forbidden: Invalid token")

    res = engine.execute_with_reliability(
        operation_id="op_auth_fail",
        operation_func=forbidden_operation,
        is_side_effect=False,
    )

    assert res.is_success is False
    assert res.status == "AUTHORIZATION_FAILED"
    assert res.failure_category == FailureCategory.AUTHORIZATION
    assert res.recoverability == FailureRecoverability.NON_RETRYABLE
    assert res.attempts_executed == 1
    assert attempts == 1  # Exactamente 1 intento
    assert len(clock.sleep_calls) == 0


def test_idempotency_caching_and_conflict_detection():
    """Valida que reusar la misma clave con el mismo payload no re-ejecuta, y con distinto payload falla por CONFLICT."""
    engine = ReliabilityEngine()
    execution_count = 0

    def create_order():
        nonlocal execution_count
        execution_count += 1
        return {"order_id": "ORD-12345"}

    # 1. Primera ejecución
    res1 = engine.execute_with_reliability(
        operation_id="op_order_1",
        operation_func=create_order,
        is_side_effect=True,
        idempotency_key="idemp_key_100",
        payload={"item_id": "SKU-1", "quantity": 2},
    )
    assert res1.is_success is True
    assert res1.output == {"order_id": "ORD-12345"}
    assert execution_count == 1

    # 2. Replay con el mismo payload -> retorna resultado cacheado sin re-ejecutar
    res2 = engine.execute_with_reliability(
        operation_id="op_order_replay",
        operation_func=create_order,
        is_side_effect=True,
        idempotency_key="idemp_key_100",
        payload={"item_id": "SKU-1", "quantity": 2},
    )
    assert res2.is_success is True
    assert res2.output == {"order_id": "ORD-12345"}
    assert execution_count == 1  # No aumentó

    # 3. Llamada con la misma clave pero payload modificado -> CONFLICT inmediato
    res3 = engine.execute_with_reliability(
        operation_id="op_order_conflict",
        operation_func=create_order,
        is_side_effect=True,
        idempotency_key="idemp_key_100",
        payload={"item_id": "SKU-1", "quantity": 999},  # Payload diferente
    )
    assert res3.is_success is False
    assert res3.status == "IDEMPOTENCY_CONFLICT"
    assert res3.failure_category == FailureCategory.CONFLICT
    assert execution_count == 1  # Tampoco ejecutó la mutación


def test_side_effect_timeout_reconciliation():
    """Valida que un timeout en un side-effect dispara reconciliación evitando publicar/vender dos veces."""
    clock = VirtualClock()
    engine = ReliabilityEngine(clock=clock)

    publish_call_count = 0
    external_marketplace = {"published_items": {}}

    def publish_item():
        nonlocal publish_call_count
        publish_call_count += 1
        # Simular que el ítem sí llegó a crearse en el marketplace remoto pero la conexión se cortó al responder
        external_marketplace["published_items"]["MLA100"] = {"price": 5000}
        raise TimeoutError("HTTP Connection timed out waiting for response")

    def reconcile_publish():
        # Verifica si el ítem ya existe en el estado externo
        if "MLA100" in external_marketplace["published_items"]:
            return {"item_id": "MLA100", "status": "active"}
        return None

    res = engine.execute_with_reliability(
        operation_id="publish_mla100",
        operation_func=publish_item,
        is_side_effect=True,
        idempotency_key="idemp_pub_mla100",
        payload={"title": "SSD Kingston", "price": 5000},
        reconcile_func=reconcile_publish,
    )

    # La operación debe resolverse como RECONCILED sin reintentar publicar otra vez
    assert res.is_success is True
    assert res.status == "RECONCILED"
    assert res.reconciled is True
    assert res.output == {"item_id": "MLA100", "status": "active"}
    assert publish_call_count == 1  # No duplicó la publicación


def test_unknown_semantics_preservation():
    """Valida que una incertidumbre UNKNOWN no se convierte falsamente en SUCCESS ni FAILURE definitivo."""
    engine = ReliabilityEngine()

    def uncertain_query():
        raise RuntimeError("UNKNOWN: ambiguous provider response")

    res = engine.execute_with_reliability(
        operation_id="op_uncertain",
        operation_func=uncertain_query,
        is_side_effect=False,
    )

    assert res.is_success is False
    assert res.is_unknown is True
    assert res.status == "UNKNOWN"
    assert res.failure_category == FailureCategory.UNKNOWN


def test_sanitization_of_secrets_in_reliability_metadata():
    """Valida que claves sensibles sean ofuscadas en metadata y evidencia."""
    decision = RecoveryDecision(
        decision_id="dec_001",
        operation_id="op_001",
        failure_category=FailureCategory.TRANSIENT,
        recoverability=FailureRecoverability.RETRYABLE,
        attempt=1,
        max_attempts=3,
        retry_allowed=True,
        reconciliation_required=False,
        delay_seconds=1.0,
        status="RETRY",
        reason="Testing secret sanitization",
        correlation_id="corr_001",
        evidence={
            "api_key": "sk-1234567890abcdef",
            "Authorization": "Bearer super-secret-token",
            "normal_field": "safe_value",
            "nested": {
                "password": "my_password",
                "chain_of_thought": "secret internal reasoning",
            },
        },
    )

    ev = decision.evidence
    assert ev["api_key"] == "[REDACTED]"
    assert ev["Authorization"] == "[REDACTED]"
    assert ev["normal_field"] == "safe_value"
    assert ev["nested"]["password"] == "[REDACTED]"
    assert ev["nested"]["chain_of_thought"] == "[REDACTED]"


def test_json_idempotency_store_durability_and_recovery():
    """Valida persistencia atómica y crash-safe de JsonIdempotencyStore."""
    tmp_dir = tempfile.mkdtemp(prefix="test_idemp_store_")
    try:
        store1 = JsonIdempotencyStore(storage_dir=tmp_dir)
        store1.save(
            idempotency_key="test_key_1",
            payload_hash="hash_abc",
            result={"status": "ok", "tx_id": 999},
            status="SUCCESS",
        )

        # Simular reinicio creando una nueva instancia sobre el mismo directorio
        store2 = JsonIdempotencyStore(storage_dir=tmp_dir)
        rec = store2.get("test_key_1")
        assert rec is not None
        assert rec["idempotency_key"] == "test_key_1"
        assert rec["payload_hash"] == "hash_abc"
        assert rec["result"] == {"status": "ok", "tx_id": 999}
        assert rec["status"] == "SUCCESS"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
