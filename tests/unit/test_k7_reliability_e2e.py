"""
Test End-to-End para Confiabilidad (Hito K.7).

Flujo E2E completo:
ContinuousMission -> PolicyGuardedActionExecutor -> ReliabilityEngine -> Mocked Marketplace Boundary
-> Transient Failure / Timeout -> Reliability Recovery -> Reconciliation -> Result -> Audit Trail -> Agent Trace.
"""

import os
import shutil
import tempfile
from decimal import Decimal
from typing import Dict, Any
import pytest

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import DecisionProvider, ActionExecutor
from src.domain.policy.engine import PolicyEngine
from src.application.policy.policy_guarded_action_executor import PolicyGuardedActionExecutor
from src.domain.reliability.models import (
    FailureCategory,
    FailureRecoverability,
    RetryPolicy,
)
from src.infrastructure.reliability.reliability_infrastructure import (
    VirtualClock,
    InMemoryCircuitBreaker,
    JsonIdempotencyStore,
)
from src.application.reliability.reliability_engine import ReliabilityEngine
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.audit.audit_trail_service import AuditTrailService
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.application.agent_trace.agent_trace_service import AgentTraceService


class ResilientMarketplaceActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones que delega llamadas al ReliabilityEngine protegiendo la boundary de marketplace.
    """

    def __init__(
        self,
        reliability_engine: ReliabilityEngine,
        mock_marketplace_api: Any,
    ):
        self.reliability_engine = reliability_engine
        self.mock_marketplace_api = mock_marketplace_api
        self.executed_count = 0

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        action_type = decision.parameters.get("action_type") or str(decision.action.value)
        idempotency_key = decision.parameters.get("idempotency_key") or f"idemp_{state.mission_id}_{state.iteration}"
        payload = decision.parameters.get("payload")

        def call_external():
            return self.mock_marketplace_api.publish_listing(payload)

        def reconcile():
            return self.mock_marketplace_api.get_listing_by_sku(payload.get("sku"))

        rel_result = self.reliability_engine.execute_with_reliability(
            operation_id=f"publish_{state.mission_id}",
            operation_func=call_external,
            is_side_effect=True,
            idempotency_key=idempotency_key,
            payload=payload,
            reconcile_func=reconcile,
            correlation_id=state.mission_id,
            causation_id=f"decision_{state.iteration}",
        )

        return {
            "action_executed": action_type,
            "status": "SUCCESS" if rel_result.is_success else rel_result.status,
            "reliability_result": rel_result.to_dict(),
            "output": rel_result.output,
        }


class MockMarketplaceBoundary:
    """Mock determinista del servicio externo con inyección de fallos controlada."""

    def __init__(self):
        self.published_db: Dict[str, Dict[str, Any]] = {}
        self.fail_with_timeout_once = True
        self.api_call_count = 0

    def publish_listing(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.api_call_count += 1
        sku = payload["sku"]
        # Inyectar fallo: el marketplace recibe la orden y la guarda, pero la conexión se corta retornando timeout
        if self.fail_with_timeout_once:
            self.fail_with_timeout_once = False
            self.published_db[sku] = {"item_id": f"MLA_{sku}", "price": payload["price"], "status": "active"}
            raise TimeoutError("504 Gateway Timeout during POST /items")

        self.published_db[sku] = {"item_id": f"MLA_{sku}", "price": payload["price"], "status": "active"}
        return self.published_db[sku]

    def get_listing_by_sku(self, sku: str) -> Any:
        if sku in self.published_db:
            return self.published_db[sku]
        return None


def test_e2e_resilient_mission_workflow():
    """
    Demostración E2E:
    Misión -> Policy Engine -> Action Executor -> Reliability Engine -> Marketplace con Timeout
    -> Reconciliación exitosa -> Audit Trail + Agent Trace generados.
    """
    tmp_dir = tempfile.mkdtemp(prefix="k7_e2e_test_")
    try:
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

        mock_marketplace = MockMarketplaceBoundary()
        resilient_executor = ResilientMarketplaceActionExecutor(
            reliability_engine=engine,
            mock_marketplace_api=mock_marketplace,
        )

        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=resilient_executor,
            default_allowed_actions=["PUBLISH_LISTING"],
        )

        # 1. Estado de la misión
        state = LoopState(mission_id="mission-e2e-k7", iteration=1, goal="Execute resilient e2e commerce loop")
        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publish high margin opportunity discovered in Mercado Libre",
            parameters={
                "action_type": "PUBLISH_LISTING",
                "idempotency_key": "idemp_e2e_kingston_ssd",
                "payload": {"sku": "SSD-KINGSTON-480G", "price": 32000},
                "human_approved": True,
                "risk_level": "LOW",
                "provenance": "LIVE_FEED",
            },
        )

        # 2. Ejecución gobernada y protegida por confiabilidad
        result = guarded_executor.execute(decision, state)

        # 3. Verificación de resultado
        assert result["status"] == "SUCCESS"
        assert result["output"] == {"item_id": "MLA_SSD-KINGSTON-480G", "price": 32000, "status": "active"}
        assert result["reliability_result"]["reconciled"] is True
        assert mock_marketplace.api_call_count == 1  # 1 llamada única (sin reintentos ciegos destructivos)

        # 4. Verificación de Agent Trace persistido
        traces = trace_repo.list_records(execution_id="mission-e2e-k7")
        assert len(traces) > 0
        assert traces[0].operation == "execute_publish_mission-e2e-k7"

        # 5. Verificación de Audit Trail persistido
        audit_events = audit_repo.list_records(correlation_id="mission-e2e-k7")
        assert len(audit_events) > 0
        assert audit_events[0].status == "SUCCESS"

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
