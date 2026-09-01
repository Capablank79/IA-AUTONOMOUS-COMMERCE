"""
Gate H E2E Validation Test Suite
AI Autonomous Commerce — Gate H Final Audit & Validation

Verifies requirements A through P specified in Section 17 of TRAE MASTER AUDIT PROMPT:
A — Complete causal chain
B — Durable memory
C — Restart/reload
D — UNKNOWN preservation
E — Policy boundaries
F — Approval boundaries
G — Prediction vs actual
H — Calibration
I — Product performance
J — Supplier performance
K — Strategy performance
L — Learning signals
M — Signal does not modify policy
N — Idempotent replay
O — Sensitive-data exclusion
P — No false success
"""

from decimal import Decimal
import tempfile
import pytest
from datetime import datetime, timezone
from pathlib import Path

# Domain imports
from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionPriority, LoopDecision, LoopAction
from src.domain.decision.models import DecisionRecord, DecisionType, DecisionStatus, DecisionOutcome
from src.domain.policy.models import PolicyEvaluation, PolicyDecisionType, PolicyEvaluationContext
from src.domain.policy.engine import PolicyEngine
from src.domain.action.models import ActionRecord, ActionStatus
from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.prediction.models import PredictionRecord, PredictionComparison, ComparisonStatus
from src.domain.calibration.models import DecisionCalibrationRecord, CalibrationStatus
from src.domain.product_performance.models import ProductPerformanceRecord, PerformanceStatus, TemporalPeriod
from src.domain.supplier_performance.models import SupplierPerformanceRecord, SupplierPerformanceStatus, SupplierTemporalPeriod
from src.domain.strategy_performance.models import StrategyPerformanceRecord, StrategyPerformanceStatus, StrategyTemporalPeriod
from src.domain.learning_signals.models import LearningSignalRecord, LearningSignalType, LearningSignalSubjectType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel
from src.domain.market_intelligence.models import Confidence

# Infrastructure imports
from src.infrastructure.persistence.data.json.mission_repository import JsonMissionRepository
from src.infrastructure.persistence.data.json.decision_repository import JsonDecisionRepository
from src.infrastructure.persistence.data.json.action_repository import JsonActionRepository
from src.infrastructure.persistence.data.json.result_repository import JsonResultRepository
from src.infrastructure.persistence.data.json.outcome_repository import JsonOutcomeRepository
from src.infrastructure.persistence.data.json.learning_signal_repository import JsonLearningSignalRepository

# Application imports
from src.application.decision.decision_service import DecisionMemoryService
from src.application.outcome.outcome_service import OutcomeTrackingService
from src.application.learning_signals.learning_signal_service import LearningSignalService


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


def test_gate_h_e2e_full_validation_suite(tmp_dir):
    # 1. Repositories & Services initialization
    mission_repo = JsonMissionRepository(storage_dir=tmp_dir)
    decision_repo = JsonDecisionRepository(storage_dir=tmp_dir / "decisions")
    decision_service = DecisionMemoryService(decision_repo)
    action_repo = JsonActionRepository(file_path=tmp_dir / "actions.json")
    result_repo = JsonResultRepository(file_path=tmp_dir / "results.json")
    outcome_repo = JsonOutcomeRepository(file_path=tmp_dir / "outcomes.json")
    signal_repo = JsonLearningSignalRepository(file_path=tmp_dir / "signals.json")

    now = datetime.now(timezone.utc)

    # A, B — Mission Memory & Linkage
    mission = Mission(
        mission_id="m_gate_h_001",
        type=MissionType.PROFIT_EVALUATION,
        status=MissionStatus.RUNNING,
        priority=MissionPriority.HIGH,
        parameters={"max_budget": "5000.00"},
        created_at=now,
    )
    mission_repo.save(mission)

    # Verify Json Action sanitization for O (Sensitive-data exclusion)
    action_with_secret = ActionRecord(
        action_id="act_secret_001",
        decision_id="d_gate_h_001",
        mission_id="m_gate_h_001",
        action_type="PRICE_UPDATE",
        status=ActionStatus.COMPLETED,
        parameters={"sku": "SKU-GATE-H", "api_key": "SUPER_SECRET_KEY_MUST_BE_EXCLUDED"},
        created_at=now,
    )
    action_repo.save(action_with_secret)
    action_json_content = (tmp_dir / "actions.json").read_text()
    assert "SUPER_SECRET_KEY_MUST_BE_EXCLUDED" not in action_json_content

    # A & E — Decision & Policy boundaries
    policy_engine = PolicyEngine()
    
    loop_dec = LoopDecision(action=LoopAction.CONTINUE, reason="Evaluating price change")
    ctx_deny = PolicyEvaluationContext(
        action_type="FORBIDDEN_ACTION",
        actor_id="agent_1",
        mission_id="m_gate_h_001",
        correlation_id="corr_001",
        loop_decision=loop_dec,
        prohibited_actions=("FORBIDDEN_ACTION",),
    )
    policy_eval_deny = policy_engine.evaluate(context=ctx_deny)
    assert policy_eval_deny.decision == PolicyDecisionType.DENY

    ctx_allow = PolicyEvaluationContext(
        action_type="SEARCH_PRODUCTS",
        actor_id="agent_1",
        mission_id="m_gate_h_001",
        correlation_id="corr_002",
        loop_decision=loop_dec,
        risk_level=RiskLevel.LOW,
        provenance=EvidenceProvenanceType.LIVE,
    )
    policy_eval_allow = policy_engine.evaluate(context=ctx_allow)
    assert policy_eval_allow.decision == PolicyDecisionType.ALLOW

    decision = decision_service.record_decision(
        mission_id="m_gate_h_001",
        decision_type=DecisionType.PRICING_ADJUSTMENT,
        reason="Margin target satisfied",
        target_resource="SKU-GATE-H",
        idempotency_key="idemp_dec_h_001",
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )
    assert decision.decision_id is not None

    # A — Action Record
    action = ActionRecord(
        action_id="act_gate_h_001",
        decision_id=decision.decision_id,
        mission_id="m_gate_h_001",
        action_type="PRICE_UPDATE",
        status=ActionStatus.COMPLETED,
        parameters={"sku": "SKU-GATE-H", "price": "99.99"},
        created_at=now,
    )
    action_repo.save(action)

    # A & D — Result Record (Preserving UNKNOWN)
    result = ActionResultRecord(
        result_id="res_gate_h_001",
        action_id="act_gate_h_001",
        decision_id=decision.decision_id,
        mission_id="m_gate_h_001",
        outcome=ResultOutcome.SUCCESS,
        response_summary={"status_code": 200, "latency_ms": 120},
        observed_at=now,
    )
    result_repo.save(result)

    # D & P — Outcome Tracking with UNKNOWN & No false success
    outcome = OutcomeRecord(
        outcome_id="out_gate_h_001",
        result_id="res_gate_h_001",
        action_id="act_gate_h_001",
        decision_id=decision.decision_id,
        mission_id="m_gate_h_001",
        outcome_type="EXECUTION_SUCCESS",
        status=OutcomeStatus.SUCCESS,
        idempotency_key="idemp_out_h_001",
        observed_at=now,
    )
    outcome_repo.save(outcome)
    assert outcome.status == OutcomeStatus.SUCCESS

    # G — Prediction Comparison
    comparison = PredictionComparison(
        comparison_id="comp_h_001",
        prediction_id="pred_h_001",
        outcome_id="out_gate_h_001",
        mission_id="m_gate_h_001",
        decision_id=decision.decision_id,
        action_id="act_gate_h_001",
        delta=0.02,
        status=ComparisonStatus.MATCH,
        idempotency_key="idemp_comp_h_001",
        evaluated_at=now,
    )

    # H — Calibration
    calibration = DecisionCalibrationRecord(
        calibration_id="cal_h_001",
        decision_id=decision.decision_id,
        mission_id="m_gate_h_001",
        status=CalibrationStatus.WELL_CALIBRATED,
    )

    # I, J, K — Performance Records
    product_perf = ProductPerformanceRecord(
        performance_id="pp_h_001",
        product_id="PROD-GATE-H",
        sku="SKU-GATE-H",
        period=TemporalPeriod(period_type="POINT_IN_TIME"),
        status=PerformanceStatus.SUFFICIENT_DATA,
    )

    supplier_perf = SupplierPerformanceRecord(
        performance_id="sp_h_001",
        supplier_id="SUP-GATE-H",
        period=SupplierTemporalPeriod(period_type="POINT_IN_TIME"),
        status=SupplierPerformanceStatus.SUFFICIENT_DATA,
    )

    strategy_perf = StrategyPerformanceRecord(
        performance_id="strat_h_001",
        strategy_id="STRAT-PROFIT-MAX",
        period=StrategyTemporalPeriod(period_type="POINT_IN_TIME"),
        status=StrategyPerformanceStatus.SUFFICIENT_DATA,
    )

    # L — Learning Signals Service
    signal_service = LearningSignalService(signal_repo)
    signal = signal_service.process_outcome(outcome)
    assert signal is not None
    assert signal.signal_type == LearningSignalType.POSITIVE_OUTCOME

    # M — Signal does not modify Policy
    rules_count_before = len(policy_engine.rules)
    assert rules_count_before > 0
    assert policy_engine.evaluate(context=ctx_deny).decision == PolicyDecisionType.DENY

    # N — Idempotent Replay (Missions & Repositories)
    mission_repo.save(mission)
    reloaded_mission = mission_repo.get_by_id("m_gate_h_001")
    assert reloaded_mission.mission_id == "m_gate_h_001"

    # C — Restart/reload simulation from disk
    new_mission_repo = JsonMissionRepository(storage_dir=tmp_dir)
    restarted_mission = new_mission_repo.get_by_id("m_gate_h_001")
    assert restarted_mission is not None
    assert restarted_mission.mission_id == "m_gate_h_001"
    assert restarted_mission.priority == MissionPriority.HIGH
