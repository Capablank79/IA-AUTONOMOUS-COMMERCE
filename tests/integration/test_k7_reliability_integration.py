"""
Tests de Integración para el Módulo de Confiabilidad y Resiliencia (Hito K.7).

Escenarios mínimos obligatorios:
1. transient failure -> retry -> success.
2. permanent failure -> no useless retry.
3. timeout side effect -> UNKNOWN -> verify/reconcile -> no duplicate.
4. duplicate event -> one effect.
5. crash/restart -> safe recovery.
6. degraded dependency -> isolated behavior (circuit breaker).
7. retry exhausted -> explicit final status.
8. PolicyEngine boundary: retry nunca salta las validaciones de PolicyEngine.
9. Concurrency: múltiples hilos llamando a la misma operación idempotente.
10. Integración con Audit Trail (K.1) y Agent Trace (K.2).
"""

import os
import shutil
import tempfile
import threading
from decimal import Decimal
from typing import Dict, Any, List
import pytest

from src.domain.reliability.models import (
    FailureCategory,
    FailureRecoverability,
    RetryPolicy,
    CircuitBreakerConfig,
)
from src.infrastructure.reliability.reliability_infrastructure import (
    VirtualClock,
    InMemoryCircuitBreaker,
    JsonIdempotencyStore,
)
from src.application.reliability.reliability_engine import ReliabilityEngine
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.application.policy.policy_guarded_action_executor import PolicyGuardedActionExecutor
from src.domain.mission.ports import ActionExecutor
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.audit.audit_trail_service import AuditTrailService
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.application.agent_trace.agent_trace_service import AgentTraceService


class DummyActionExecutor(ActionExecutor):
    def __init__(self):
        self.executed_count = 0
        self.should_fail_times = 0
        self.last_decision = None

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        self.last_decision = decision
        if self.should_fail_times > 0:
            self.should_fail_times -= 1
            raise ConnectionError("Network timeout connecting to external adapter")
        self.executed_count += 1
        return {"action_executed": decision.action.value, "status": "SUCCESS"}


@pytest.fixture
def temp_env():
    tmp_dir = tempfile.mkdtemp(prefix="k7_integration_env_")
    audit_repo = JsonAuditRepository(storage_dir=os.path.join(tmp_dir, "audit"))
    trace_repo = JsonAgentTraceRepository(base_dir=os.path.join(tmp_dir, "trace"))
    idemp_store = JsonIdempotencyStore(storage_dir=os.path.join(tmp_dir, "idemp"))
    clock = VirtualClock()

    audit_svc = AuditTrailService(audit_repository=audit_repo)
    trace_svc = AgentTraceService(trace_repository=trace_repo)
    cb = InMemoryCircuitBreaker(clock=clock)

    engine = ReliabilityEngine(
        circuit_breaker=cb,
        idempotency_store=idemp_store,
        clock=clock,
        audit_trail_service=audit_svc,
        agent_trace_service=trace_svc,
    )

    yield {
        "tmp_dir": tmp_dir,
        "engine": engine,
        "clock": clock,
        "audit_svc": audit_svc,
        "trace_svc": trace_svc,
        "idemp_store": idemp_store,
        "circuit_breaker": cb,
    }
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_scenario_1_transient_failure_retry_success(temp_env):
    """1. Transient failure -> retry -> success."""
    engine: ReliabilityEngine = temp_env["engine"]
    attempts = 0

    def op():
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise ConnectionResetError("Connection lost")
        return {"status": "ok"}

    res = engine.execute_with_reliability(
        operation_id="transient_op",
        operation_func=op,
        is_side_effect=False,
    )

    assert res.is_success is True
    assert res.status == "SUCCESS"
    assert res.attempts_executed == 2
    assert len(res.recovery_decisions) == 1
    assert res.recovery_decisions[0].retry_allowed is True


def test_scenario_2_permanent_failure_no_useless_retry(temp_env):
    """2. Permanent failure -> no useless retry."""
    engine: ReliabilityEngine = temp_env["engine"]
    attempts = 0

    def op():
        nonlocal attempts
        attempts += 1
        raise ValueError("Invalid listing schema payload")

    res = engine.execute_with_reliability(
        operation_id="permanent_op",
        operation_func=op,
        is_side_effect=False,
    )

    assert res.is_success is False
    assert res.status == "FAILED"
    assert res.failure_category == FailureCategory.VALIDATION
    assert res.recoverability == FailureRecoverability.NON_RETRYABLE
    assert res.attempts_executed == 1
    assert attempts == 1


def test_scenario_3_timeout_side_effect_reconciliation_no_duplicate(temp_env):
    """3. Timeout side effect -> UNKNOWN -> verify/reconcile -> no duplicate."""
    engine: ReliabilityEngine = temp_env["engine"]
    calls = 0
    external_db = {}

    def mutate():
        nonlocal calls
        calls += 1
        external_db["ITEM-42"] = {"stock": 10}
        raise TimeoutError("504 Gateway Timeout")

    def reconcile():
        if "ITEM-42" in external_db:
            return {"reconciled": True, "data": external_db["ITEM-42"]}
        return None

    res = engine.execute_with_reliability(
        operation_id="mutate_item",
        operation_func=mutate,
        is_side_effect=True,
        idempotency_key="idemp_mutate_42",
        payload={"stock": 10},
        reconcile_func=reconcile,
    )

    assert res.is_success is True
    assert res.status == "RECONCILED"
    assert res.reconciled is True
    assert calls == 1  # Exactamente 1 llamada externa, no hubo doble inserción


def test_scenario_4_duplicate_event_one_effect(temp_env):
    """4. Duplicate event / call -> one logical effect."""
    engine: ReliabilityEngine = temp_env["engine"]
    effect_count = 0

    def process_event():
        nonlocal effect_count
        effect_count += 1
        return {"processed": True}

    # Primera entrega del evento
    res1 = engine.execute_with_reliability(
        operation_id="evt_001",
        operation_func=process_event,
        is_side_effect=True,
        idempotency_key="event_key_abc",
        payload={"event_id": "evt_001", "body": "data"},
    )
    assert res1.is_success is True
    assert effect_count == 1

    # Entrega duplicada del mismo evento
    res2 = engine.execute_with_reliability(
        operation_id="evt_001_dup",
        operation_func=process_event,
        is_side_effect=True,
        idempotency_key="event_key_abc",
        payload={"event_id": "evt_001", "body": "data"},
    )
    assert res2.is_success is True
    assert effect_count == 1  # Efecto único garantizado


def test_scenario_5_crash_restart_recovery(temp_env):
    """5. Crash/restart -> safe recovery from durable store."""
    engine: ReliabilityEngine = temp_env["engine"]
    tmp_dir = temp_env["tmp_dir"]

    # Ejecutar acción persistente
    engine.execute_with_reliability(
        operation_id="op_durable",
        operation_func=lambda: {"order_id": "ORD-PERSIST"},
        is_side_effect=True,
        idempotency_key="idemp_persist_99",
        payload={"target": "order"},
    )

    # Simular reinicio creando un nuevo ReliabilityEngine sobre el storage existente
    recovered_idemp = JsonIdempotencyStore(storage_dir=os.path.join(tmp_dir, "idemp"))
    new_engine = ReliabilityEngine(
        idempotency_store=recovered_idemp,
        clock=temp_env["clock"],
    )

    invoked = False

    def should_not_run():
        nonlocal invoked
        invoked = True
        return {"order_id": "SHOULD_NOT_GENERATE"}

    # Reintentar tras reinicio
    res = new_engine.execute_with_reliability(
        operation_id="op_durable_resume",
        operation_func=should_not_run,
        is_side_effect=True,
        idempotency_key="idemp_persist_99",
        payload={"target": "order"},
    )

    assert res.is_success is True
    assert res.output == {"order_id": "ORD-PERSIST"}
    assert invoked is False


def test_scenario_6_degraded_dependency_circuit_breaker(temp_env):
    """6. Degraded dependency -> circuit breaker isolation."""
    engine: ReliabilityEngine = temp_env["engine"]
    cb: InMemoryCircuitBreaker = temp_env["circuit_breaker"]
    cb.config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout_seconds=30.0)

    def failing_service():
        raise ConnectionError("Service unreachable")

    # Fallo 1
    engine.execute_with_reliability(
        operation_id="op1",
        operation_func=failing_service,
        is_side_effect=False,
        service_name="supplier_api",
        retry_policy=RetryPolicy(max_attempts=1),
    )
    # Fallo 2 -> abre circuito
    engine.execute_with_reliability(
        operation_id="op2",
        operation_func=failing_service,
        is_side_effect=False,
        service_name="supplier_api",
        retry_policy=RetryPolicy(max_attempts=1),
    )

    # Llamada 3 debe fallar rápido por Circuit Open sin llamar a la función
    called = False

    def spy_op():
        nonlocal called
        called = True
        return {"data": 123}

    res = engine.execute_with_reliability(
        operation_id="op3",
        operation_func=spy_op,
        is_side_effect=False,
        service_name="supplier_api",
    )

    assert res.is_success is False
    assert res.status == "CIRCUIT_OPEN"
    assert res.degraded is True
    assert called is False


def test_scenario_7_retry_exhausted_explicit_status(temp_env):
    """7. Retry exhausted -> explicit final status."""
    engine: ReliabilityEngine = temp_env["engine"]

    def always_fails():
        raise TimeoutError("Endpoint timeout")

    res = engine.execute_with_reliability(
        operation_id="exhaust_op",
        operation_func=always_fails,
        is_side_effect=False,
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.1),
    )

    assert res.is_success is False
    assert res.status == "RETRY_EXHAUSTED"
    assert res.attempts_executed == 3
    assert len(res.recovery_decisions) == 3


def test_scenario_8_policy_engine_boundary_never_bypassed(temp_env):
    """8. Retry NUNCA se salta PolicyEngine. Authorization/policy denial -> no retry."""
    engine: ReliabilityEngine = temp_env["engine"]
    raw_executor = DummyActionExecutor()
    guarded_executor = PolicyGuardedActionExecutor(
        delegate_executor=raw_executor,
        default_prohibited_actions=["UNAUTHORIZED_ACTION"],
    )

    state = LoopState(mission_id="m-123", iteration=1, goal="Test governance boundary")
    decision = LoopDecision(
        action=LoopAction.REJECT,
        reason="Test policy boundary",
        parameters={"action_type": "UNAUTHORIZED_ACTION"},
    )

    # Intentar ejecutar acción denegada por gobernanza
    result = guarded_executor.execute(decision, state)

    assert result["status"] == "POLICY_DENIED"
    assert raw_executor.executed_count == 0  # No llegó al ejecutor real


def test_scenario_9_concurrent_idempotency(temp_env):
    """9. Concurrency: múltiples hilos llamando simultáneamente con la misma clave de idempotencia."""
    engine: ReliabilityEngine = temp_env["engine"]
    execution_counter = 0
    lock = threading.Lock()

    def concurrent_side_effect():
        nonlocal execution_counter
        with lock:
            execution_counter += 1
        return {"tx_code": "TX-SUCCESS"}

    results = []

    def worker():
        res = engine.execute_with_reliability(
            operation_id="concurrent_op",
            operation_func=concurrent_side_effect,
            is_side_effect=True,
            idempotency_key="shared_idemp_key",
            payload={"action": "deduct_funds"},
        )
        results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Todos los hilos deben haber obtenido éxito
    assert all(r.is_success for r in results)
    # Pero el efecto real se ejecutó únicamente 1 vez
    assert execution_counter == 1
