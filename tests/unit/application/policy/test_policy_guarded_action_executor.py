import pytest
from decimal import Decimal
from typing import Dict, Any

from src.domain.mission.models import LoopDecision, LoopAction, LoopState
from src.domain.mission.ports import ActionExecutor
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
)
from src.domain.capital.models import CapitalBudget
from src.domain.supplier_intelligence.models import RiskLevel, EvidenceProvenanceType
from src.application.policy.policy_enforcement_service import PolicyEnforcementService
from src.application.policy.policy_guarded_action_executor import PolicyGuardedActionExecutor


class MockActionExecutor(ActionExecutor):
    def __init__(self):
        self.calls = []
        self.external_calls_count = 0

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        self.calls.append((decision, state))
        self.external_calls_count += 1
        return {"status": "SUCCESS", "executed": decision.action.value}


@pytest.fixture
def mock_executor():
    return MockActionExecutor()


@pytest.fixture
def policy_service():
    return PolicyEnforcementService()


def test_guarded_executor_allow_executes_delegate(mock_executor, policy_service):
    guarded = PolicyGuardedActionExecutor(
        delegate_executor=mock_executor,
        policy_service=policy_service,
    )
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Safe action",
        parameters={"action_type": "SEARCH", "risk_level": RiskLevel.LOW, "provenance": EvidenceProvenanceType.LIVE}
    )
    state = LoopState(mission_id="m-exec-1", iteration=1, goal="Execute test")

    result = guarded.execute(decision, state)

    assert result["status"] == "SUCCESS"
    assert result["policy_decision"] == PolicyDecisionType.ALLOW.value
    assert len(mock_executor.calls) == 1
    assert mock_executor.external_calls_count == 1


def test_guarded_executor_deny_blocks_external_execution(mock_executor, policy_service):
    guarded = PolicyGuardedActionExecutor(
        delegate_executor=mock_executor,
        policy_service=policy_service,
        default_prohibited_actions=["UNAUTHORIZED_ACTION"],
    )
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Forbidden action",
        parameters={"action_type": "UNAUTHORIZED_ACTION"}
    )
    state = LoopState(mission_id="m-exec-2", iteration=1, goal="Block test")

    result = guarded.execute(decision, state)

    assert result["status"] == "POLICY_DENIED"
    assert result["is_allowed"] is False
    assert result["decision"] == PolicyDecisionType.DENY.value
    # Asegurar que NUNCA llamó al ejecutor subyacente
    assert len(mock_executor.calls) == 0
    assert mock_executor.external_calls_count == 0


def test_guarded_executor_require_approval_blocks_execution(mock_executor, policy_service):
    guarded = PolicyGuardedActionExecutor(
        delegate_executor=mock_executor,
        policy_service=policy_service,
        default_actions_requiring_approval=["PUBLISH"],
    )
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publish requires human sign-off",
        parameters={
            "action_type": "PUBLISH",
            "risk_level": RiskLevel.LOW,
            "provenance": EvidenceProvenanceType.LIVE,
            "human_approved": False,
        }
    )
    state = LoopState(mission_id="m-exec-3", iteration=1, goal="Approval test")

    result = guarded.execute(decision, state)

    assert result["status"] == "POLICY_APPROVAL_REQUIRED"
    assert result["is_allowed"] is False
    assert result["requires_approval"] is True
    # Asegurar que NUNCA llamó al ejecutor subyacente
    assert len(mock_executor.calls) == 0
    assert mock_executor.external_calls_count == 0


def test_guarded_executor_unknown_blocks_execution(mock_executor, policy_service):
    guarded = PolicyGuardedActionExecutor(
        delegate_executor=mock_executor,
        policy_service=policy_service,
    )
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Action with missing required idempotency key",
        parameters={
            "action_type": "SEARCH",
            "is_external_impact": True,
            "risk_level": RiskLevel.LOW,
            "provenance": EvidenceProvenanceType.LIVE,
            "idempotency_key": None,  # Causa UNKNOWN
        }
    )
    state = LoopState(mission_id="m-exec-4", iteration=1, goal="Unknown test")

    result = guarded.execute(decision, state)

    assert result["status"] == "POLICY_UNKNOWN"
    assert result["is_allowed"] is False
    assert result["is_unknown"] is True
    # Asegurar que NUNCA llamó al ejecutor subyacente
    assert len(mock_executor.calls) == 0
    assert mock_executor.external_calls_count == 0


def test_guarded_executor_prevents_replay_attack(mock_executor, policy_service):
    guarded = PolicyGuardedActionExecutor(
        delegate_executor=mock_executor,
        policy_service=policy_service,
    )
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="First execution",
        parameters={
            "action_type": "SEARCH",
            "idempotency_key": "idemp-safe-123",
            "risk_level": RiskLevel.LOW,
            "provenance": EvidenceProvenanceType.LIVE,
        }
    )
    state = LoopState(mission_id="m-exec-5", iteration=1, goal="Replay test")

    # 1. Primera ejecución: pasa
    res1 = guarded.execute(decision, state)
    assert res1["status"] == "SUCCESS"
    assert len(mock_executor.calls) == 1

    # 2. Intento de re-ejecución con la misma idempotency_key: bloqueado por Policy DENY
    res2 = guarded.execute(decision, state)
    assert res2["status"] == "POLICY_DENIED"
    assert res2["decision"] == PolicyDecisionType.DENY.value
    # No incrementa llamadas
    assert len(mock_executor.calls) == 1
    assert mock_executor.external_calls_count == 1
