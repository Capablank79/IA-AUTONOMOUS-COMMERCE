import pytest
import tempfile
from pathlib import Path

from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.prediction.models import PredictionComparison, ComparisonStatus
from src.domain.learning_signals.models import LearningSignalType, LearningSignalSubjectType
from src.infrastructure.persistence.data.json.learning_signal_repository import JsonLearningSignalRepository
from src.application.learning_signals.learning_signal_service import LearningSignalService


def test_learning_signal_service_end_to_end_flow():
    with tempfile.TemporaryDirectory() as tmp_dir:
        json_path = Path(tmp_dir) / "signals.json"
        repo = JsonLearningSignalRepository(json_path)
        service = LearningSignalService(repo)

        # 1. Outcome SUCCESS -> POSITIVE_OUTCOME signal
        outcome = OutcomeRecord(
            outcome_id="out-e2e-1",
            mission_id="m-e2e-1",
            decision_id="dec-e2e-1",
            action_id="act-e2e-1",
            status=OutcomeStatus.SUCCESS,
            idempotency_key="idemp-out-e2e-1",
        )
        sig1 = service.process_outcome(outcome)
        assert sig1 is not None
        assert sig1.signal_type == LearningSignalType.POSITIVE_OUTCOME

        # 2. Idempotency test (replay same outcome)
        sig1_replay = service.process_outcome(outcome)
        assert sig1_replay.signal_id == sig1.signal_id
        assert len(service.list_all_signals()) == 1

        # 3. Prediction MISS -> PREDICTION_MISS signal
        comp = PredictionComparison(
            comparison_id="comp-e2e-1",
            prediction_id="pred-e2e-1",
            outcome_id="out-e2e-1",
            mission_id="m-e2e-1",
            decision_id="dec-e2e-1",
            delta=0.15,
            status=ComparisonStatus.MISS,
            idempotency_key="idemp-comp-e2e-1",
        )
        sig2 = service.process_prediction_comparison(comp)
        assert sig2 is not None
        assert sig2.signal_type == LearningSignalType.PREDICTION_MISS
        assert len(service.list_all_signals()) == 2

        # 4. Restart / Reload test
        repo_reloaded = JsonLearningSignalRepository(json_path)
        service_reloaded = LearningSignalService(repo_reloaded)
        signals = service_reloaded.list_all_signals()
        assert len(signals) == 2

        by_subj = service_reloaded.get_signals_by_subject(LearningSignalSubjectType.ACTION, "act-e2e-1")
        assert len(by_subj) == 1
        assert by_subj[0].signal_id == sig1.signal_id

        by_type = service_reloaded.get_signals_by_type(LearningSignalType.PREDICTION_MISS)
        assert len(by_type) == 1
        assert by_type[0].signal_id == sig2.signal_id
