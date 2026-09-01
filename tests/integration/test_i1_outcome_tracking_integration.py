import pytest
from src.domain.mission.models import Mission, MissionType
from src.domain.decision.models import DecisionRecord, DecisionType, PolicyEvaluation
from src.domain.action.models import ActionRecord, ActionStatus
from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType

from src.infrastructure.persistence.data.json.mission_repository import JsonMissionRepository
from src.infrastructure.persistence.data.json.decision_repository import JsonDecisionRepository
from src.infrastructure.persistence.data.json.action_repository import JsonActionRepository
from src.infrastructure.persistence.data.json.result_repository import JsonResultRepository
from src.infrastructure.persistence.data.json.outcome_repository import JsonOutcomeRepository

from src.application.decision.decision_service import DecisionMemoryService
from src.application.action.action_service import ActionMemoryService
from src.application.result.result_service import ResultMemoryService
from src.application.outcome.outcome_service import OutcomeTrackingService


def test_outcome_tracking_causal_chain_e2e(tmp_path):
    # 1. Setup real persistence repositories
    mission_repo = JsonMissionRepository(tmp_path / "missions.json")
    decision_repo = JsonDecisionRepository(tmp_path / "decisions.json")
    action_repo = JsonActionRepository(tmp_path / "actions.json")
    result_repo = JsonResultRepository(tmp_path / "results.json")
    outcome_repo = JsonOutcomeRepository(tmp_path / "outcomes.json")

    decision_service = DecisionMemoryService(decision_repo)
    action_service = ActionMemoryService(action_repo)
    result_service = ResultMemoryService(result_repo)
    outcome_service = OutcomeTrackingService(outcome_repo)

    mission_id = "mission-e2e-001"
    decision_id = "dec-e2e-001"
    action_id = "act-e2e-001"
    result_id = "res-e2e-001"
    outcome_id = "out-e2e-001"

    # 2. Mission
    mission = Mission.create(
        mission_type=MissionType.COMMERCIAL_PUBLICATION,
        parameters={"objective": "Validate I.1 Outcome Tracking E2E Causal Chain"},
    )
    mission_repo.save(mission)
    mission_id = mission.mission_id

    # 3. Decision
    decision_record = decision_service.record_decision(
        mission_id=mission_id,
        decision_type=DecisionType.PUBLICATION_STRATEGY,
        reason="Publish winner product to Mercado Libre",
        idempotency_key="idemp-dec-e2e-001",
    )
    decision_id = decision_record.decision_id

    # 4. Action
    action_record = action_service.record_action(
        action_id=action_id,
        decision_id=decision_id,
        mission_id=mission_id,
        action_type="CREATE_LISTING",
        idempotency_key="idemp-act-e2e-001",
    )
    action_service.update_action_status(action_id, ActionStatus.COMPLETED)

    # 5. Result
    result_record = result_service.record_result(
        result_id=result_id,
        action_id=action_id,
        decision_id=decision_id,
        mission_id=mission_id,
        outcome=ResultOutcome.SUCCESS,
        response_summary={"external_id": "MLC123456789"},
    )

    # 6. Outcome Observed (I.1)
    outcome_record = outcome_service.record_outcome(
        outcome_id=outcome_id,
        mission_id=mission_id,
        decision_id=decision_id,
        action_id=action_id,
        result_id=result_id,
        status=OutcomeStatus.SUCCESS,
        value_metrics={
            "units_sold_30d": 12,
            "realized_revenue_clp": 144000,
            "realized_margin": 0.28,
            "returns_count": 0,
        },
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        correlation_id="corr-e2e-999",
        idempotency_key="idemp-e2e-999",
    )

    # 7. Verification of Causal Chain and Reload
    loaded_outcome = outcome_service.get_outcome(outcome_id)
    assert loaded_outcome is not None
    assert loaded_outcome.mission_id == mission.mission_id
    assert loaded_outcome.decision_id == decision_record.decision_id
    assert loaded_outcome.action_id == action_record.action_id
    assert loaded_outcome.result_id == result_record.result_id
    assert loaded_outcome.status == OutcomeStatus.SUCCESS
    assert loaded_outcome.value_metrics["realized_margin"] == 0.28

    # Query back using causal references
    by_mission = outcome_service.get_outcomes_for_mission(mission_id)
    assert len(by_mission) == 1
    assert by_mission[0].outcome_id == outcome_id

    by_decision = outcome_service.get_outcomes_for_decision(decision_id)
    assert len(by_decision) == 1
    assert by_decision[0].outcome_id == outcome_id

    by_action = outcome_service.get_outcomes_for_action(action_id)
    assert len(by_action) == 1
    assert by_action[0].outcome_id == outcome_id

    by_result = outcome_service.get_outcomes_for_result(result_id)
    assert len(by_result) == 1
    assert by_result[0].outcome_id == outcome_id
