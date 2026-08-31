import pytest
from datetime import datetime, timezone
from decimal import Decimal

from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionPriority
from src.infrastructure.persistence.data.json.mission_repository import JsonMissionRepository
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
)
from src.infrastructure.persistence.data.json.decision_repository import JsonDecisionRepository
from src.application.decision.decision_service import DecisionMemoryService


@pytest.fixture
def temp_storage_dir(tmp_path):
    return tmp_path / "h2_integration_storage"


def test_h2_decision_memory_end_to_end_pipeline(temp_storage_dir):
    """
    Integración E2E completa H.2:
    MISSION -> DECISION -> DECISION REPOSITORY -> PERSIST -> LOAD -> VERIFY MISSION LINK -> VERIFY POLICY/EVIDENCE/CONFIDENCE -> UPDATE -> PERSIST -> RESTART/RELOAD -> CONTINUE
    """
    mission_repo = JsonMissionRepository(temp_storage_dir)
    decision_repo = JsonDecisionRepository(temp_storage_dir)
    decision_service = DecisionMemoryService(decision_repo)

    # 1. Crear y persistir Mission (H.1)
    mission = Mission.create(
        mission_type=MissionType.FULL_OPPORTUNITY_ANALYSIS,
        parameters={"query": "aspiradora robot", "budget": 500000},
        priority=MissionPriority.HIGH,
    )
    mission_repo.save(mission)

    # 2. Simular generación de decisión de negocio ligada a la misión
    evidence = DecisionEvidenceReference(
        evidence_id="ev-robot-01",
        evidence_type="SUPPLIER_QUOTE",
        source="AlibabaLiveAPI",
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        metadata={"unit_cost_usd": 120, "moq": 50},
    )

    policy_eval = PolicyEvaluation(
        evaluation_id="eval-h2-01",
        decision=PolicyDecisionType.ALLOW,
        action_type="ALLOCATE_CAPITAL",
        actor_id="autonomous-agent-01",
        mission_id=mission.mission_id,
        correlation_id="corr-h2-pipeline",
        rules_evaluated=("RULE_BUDGET_CAP", "RULE_RISK_LIMIT"),
        rule_results=(),
        reasons=("Within capital budget limit",),
        violations=(),
        is_allowed=True,
        requires_approval=False,
        is_unknown=False,
        is_denied=False,
        is_deferred=False,
        budget_impact=Decimal("350000"),
        risk_level=RiskLevel.LOW,
        idempotency_key="idemp-h2-step1",
    )

    recorded_decision = decision_service.record_decision(
        mission_id=mission.mission_id,
        decision_type=DecisionType.CAPITAL_ALLOCATION,
        reason="Allocating 350,000 CLP for initial inventory trial",
        status=DecisionStatus.PROPOSED,
        outcome=DecisionOutcome.PENDING_EXECUTION,
        target_resource="OPP-ROBOT-001",
        parameters={"allocated_amount": 350000, "operating_model": "INVENTORY"},
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        risk_level=RiskLevel.LOW,
        policy_evaluation=policy_eval,
        evidence_references=[evidence],
        future_action_type="CREATE_PURCHASE_ORDER",
        correlation_id="corr-h2-pipeline",
        idempotency_key="idemp-h2-step1",
    )

    # 3. Verificar que la decisión persistida está vinculada a la misión
    mission_decisions = decision_service.get_mission_decisions(mission.mission_id)
    assert len(mission_decisions) == 1
    assert mission_decisions[0].decision_id == recorded_decision.decision_id
    assert mission_decisions[0].mission_id == mission.mission_id
    assert mission_decisions[0].future_action_type == "CREATE_PURCHASE_ORDER"
    assert mission_decisions[0].policy_decision_type == PolicyDecisionType.ALLOW

    # 4. Actualizar estado de la decisión
    decision_service.update_decision_status(
        decision_id=recorded_decision.decision_id,
        new_status=DecisionStatus.VALIDATED,
        outcome=DecisionOutcome.PENDING_EXECUTION,
    )

    # 5. RESTART / RECOVERY SIMULATION
    # Recrear repository y service desde cero sobre el mismo directorio de almacenamiento
    new_decision_repo = JsonDecisionRepository(temp_storage_dir)
    new_decision_service = DecisionMemoryService(new_decision_repo)
    new_mission_repo = JsonMissionRepository(temp_storage_dir)

    # 6. Cargar y verificar integridad post-reinicio
    reloaded_mission = new_mission_repo.get_by_id(mission.mission_id)
    assert reloaded_mission is not None
    assert reloaded_mission.mission_id == mission.mission_id

    reloaded_decisions = new_decision_service.get_mission_decisions(mission.mission_id)
    assert len(reloaded_decisions) == 1
    d_reloaded = reloaded_decisions[0]

    assert d_reloaded.decision_id == recorded_decision.decision_id
    assert d_reloaded.status == DecisionStatus.VALIDATED
    assert d_reloaded.outcome == DecisionOutcome.PENDING_EXECUTION
    assert d_reloaded.version == 2
    assert d_reloaded.confidence == Confidence.HIGH
    assert d_reloaded.provenance == EvidenceProvenanceType.LIVE
    assert d_reloaded.policy_evaluation is not None
    assert d_reloaded.policy_evaluation.evaluation_id == "eval-h2-01"
    assert d_reloaded.policy_evaluation.budget_impact == Decimal("350000")
    assert len(d_reloaded.evidence_references) == 1
    assert d_reloaded.evidence_references[0].evidence_id == "ev-robot-01"
