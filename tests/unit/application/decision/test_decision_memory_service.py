import pytest
from datetime import datetime, timezone
from decimal import Decimal

from src.domain.decision.models import (
    DecisionRecord,
    DecisionType,
    DecisionStatus,
    DecisionOutcome,
    DecisionEvidenceReference,
)
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel
from src.domain.policy.models import (
    PolicyEvaluation,
    PolicyDecisionType,
    RuleEvaluationResult,
    PolicyViolation,
    PolicyRuleCategory,
    PolicySeverity,
)
from src.infrastructure.persistence.data.json.decision_repository import (
    JsonDecisionRepository,
    InvalidDecisionDataError,
)
from src.application.decision.decision_service import (
    DecisionMemoryService,
    DecisionNotFoundError,
)


@pytest.fixture
def temp_storage_dir(tmp_path):
    return tmp_path / "decision_storage"


def test_decision_record_domain_immutability_and_update():
    evidence = DecisionEvidenceReference(
        evidence_id="ev-101",
        evidence_type="MARKET_PRICE",
        source="MercadoLibreAPI",
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        metadata={"price": 19990},
    )

    decision = DecisionRecord(
        decision_id="dec-001",
        mission_id="mission-100",
        decision_type=DecisionType.MARKET_OPPORTUNITY,
        status=DecisionStatus.PROPOSED,
        reason="High demand detected with profitable margin",
        target_resource="MLB-123456",
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        risk_level=RiskLevel.LOW,
        evidence_references=(evidence,),
        correlation_id="corr-xyz",
        idempotency_key="idemp-abc",
        version=1,
    )

    assert decision.decision_id == "dec-001"
    assert decision.version == 1
    assert decision.status == DecisionStatus.PROPOSED
    assert len(decision.evidence_references) == 1

    updated = decision.update_status(DecisionStatus.EXECUTED, outcome=DecisionOutcome.SUCCESS)
    assert updated.status == DecisionStatus.EXECUTED
    assert updated.outcome == DecisionOutcome.SUCCESS
    assert updated.version == 2
    assert updated.decision_id == decision.decision_id
    assert decision.status == DecisionStatus.PROPOSED  # Original remains immutable


def test_json_decision_repository_round_trip(temp_storage_dir):
    repo = JsonDecisionRepository(temp_storage_dir)

    policy_eval = PolicyEvaluation(
        evaluation_id="eval-001",
        decision=PolicyDecisionType.ALLOW,
        action_type="PUBLISH",
        actor_id="agent-01",
        mission_id="mission-100",
        correlation_id="corr-xyz",
        rules_evaluated=("RULE_BUDGET", "RULE_AUTHORIZATION"),
        rule_results=(),
        reasons=("All policy rules passed",),
        violations=(),
        is_allowed=True,
        requires_approval=False,
        is_unknown=False,
        is_denied=False,
        is_deferred=False,
        budget_impact=Decimal("150000"),
        risk_level=RiskLevel.LOW,
        idempotency_key="idemp-abc",
    )

    decision = DecisionRecord(
        decision_id="dec-002",
        mission_id="mission-100",
        decision_type=DecisionType.PUBLICATION_STRATEGY,
        status=DecisionStatus.APPROVED,
        reason="Approved for publishing",
        policy_evaluation=policy_eval,
        policy_decision_type=PolicyDecisionType.ALLOW,
        correlation_id="corr-xyz",
        idempotency_key="idemp-abc",
    )

    repo.save(decision)

    loaded = repo.get_by_id("dec-002")
    assert loaded is not None
    assert loaded.decision_id == "dec-002"
    assert loaded.mission_id == "mission-100"
    assert loaded.decision_type == DecisionType.PUBLICATION_STRATEGY
    assert loaded.policy_decision_type == PolicyDecisionType.ALLOW
    assert loaded.policy_evaluation is not None
    assert loaded.policy_evaluation.evaluation_id == "eval-001"
    assert loaded.policy_evaluation.budget_impact == Decimal("150000")


def test_json_decision_repository_sensitive_data_exclusion(temp_storage_dir):
    repo = JsonDecisionRepository(temp_storage_dir)

    decision = DecisionRecord(
        decision_id="dec-secret",
        mission_id="mission-secret",
        decision_type=DecisionType.GENERIC_LOOP,
        status=DecisionStatus.PROPOSED,
        reason="Secret check",
        parameters={"safe_param": "hello", "password": "super-secret-password", "token": "oauth-xyz"},
        metadata={"api_key": "12345", "public_meta": "ok"},
    )

    repo.save(decision)

    file_path = temp_storage_dir / "decisions" / "dec-secret.json"
    content = file_path.read_text(encoding="utf-8")

    assert "super-secret-password" not in content
    assert "oauth-xyz" not in content
    assert "12345" not in content
    assert "hello" in content
    assert "ok" in content


def test_decision_memory_service_record_and_idempotency(temp_storage_dir):
    repo = JsonDecisionRepository(temp_storage_dir)
    service = DecisionMemoryService(repo)

    d1 = service.record_decision(
        mission_id="mission-200",
        decision_type=DecisionType.CAPITAL_ALLOCATION,
        reason="Initial allocation",
        idempotency_key="unique-idemp-100",
        parameters={"amount": 50000},
    )

    assert d1.decision_id.startswith("dec-")
    assert d1.mission_id == "mission-200"

    # Replay with same idempotency key
    d2 = service.record_decision(
        mission_id="mission-200",
        decision_type=DecisionType.CAPITAL_ALLOCATION,
        reason="Replay attempt",
        idempotency_key="unique-idemp-100",
        parameters={"amount": 999999},
    )

    assert d2.decision_id == d1.decision_id
    assert d2.parameters["amount"] == 50000


def test_decision_memory_service_update_and_get(temp_storage_dir):
    repo = JsonDecisionRepository(temp_storage_dir)
    service = DecisionMemoryService(repo)

    d = service.record_decision(
        mission_id="mission-300",
        decision_type=DecisionType.SUPPLIER_SELECTION,
        reason="Selecting preferred supplier",
    )

    updated = service.update_decision_status(
        decision_id=d.decision_id,
        new_status=DecisionStatus.VALIDATED,
        outcome=DecisionOutcome.SUCCESS,
    )

    assert updated.status == DecisionStatus.VALIDATED
    assert updated.outcome == DecisionOutcome.SUCCESS

    retrieved = service.get_decision(d.decision_id)
    assert retrieved.status == DecisionStatus.VALIDATED


def test_decision_memory_service_not_found(temp_storage_dir):
    repo = JsonDecisionRepository(temp_storage_dir)
    service = DecisionMemoryService(repo)

    with pytest.raises(DecisionNotFoundError):
        service.get_decision("non-existent-dec-id")
