from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
import json
import threading

import pytest

from src.application.quality_gate.quality_gate_service import QualityGateService
from src.domain.audit.models import AuditRecordType
from src.domain.evaluation.models import BatchEvaluationSummary, EvaluationResult, EvaluationStatus
from src.domain.quality_gate.models import (
    ErrorCasePolicy,
    GateDecisionStatus,
    MissingCasePolicy,
    QualityGateDefinition,
    QualityGateDecision,
    UnknownCasePolicy,
)
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.quality_gate_repository import (
    CorruptedQualityGateRecordError,
    GateDecisionConflictError,
    GateVersionConflictError,
    JsonQualityGateRepository,
)


def result(case_id, status, evaluator_version="1.0.0", result_id=None):
    now = datetime.now(timezone.utc)
    return EvaluationResult(
        result_id=result_id or f"result-{case_id}",
        case_id=case_id,
        execution_id=f"exec-{case_id}",
        evaluated_component="component",
        started_at=now,
        completed_at=now,
        status=status,
        evaluator_version=evaluator_version,
    )


@pytest.fixture
def service(tmp_path):
    return QualityGateService(JsonQualityGateRepository(tmp_path / "gates"))


def test_definition_is_immutable_deterministic_and_sanitized():
    gate = QualityGateDefinition(
        gate_id="release",
        name="Release",
        description="Critical metrics",
        required_case_ids=["b", "a", "a"],
        minimum_pass_rate=Decimal("0.90"),
        metadata={"api_token": "secret", "nested": {"password": "secret"}},
    )
    same = QualityGateDefinition(
        gate_id="release",
        name="Other label",
        description="Other description",
        required_case_ids=["a", "b"],
        minimum_pass_rate=Decimal("0.90"),
        metadata={"api_token": "secret", "nested": {"password": "secret"}},
    )
    assert gate.required_case_ids == ("a", "b")
    assert gate.checksum == same.checksum
    assert gate.metadata["api_token"] == "[REDACTED]"
    assert gate.metadata["nested"]["password"] == "[REDACTED]"
    with pytest.raises(FrozenInstanceError):
        gate.gate_id = "other"
    with pytest.raises(TypeError):
        gate.metadata["x"] = "y"


def test_definition_rejects_invalid_thresholds():
    with pytest.raises(ValueError):
        QualityGateDefinition("g", "G", "", minimum_pass_rate=Decimal("1.01"))
    with pytest.raises(ValueError):
        QualityGateDefinition("g", "G", "", max_failures=-1)


def test_pass_and_exact_decimal_pass_rate(service):
    gate = QualityGateDefinition(
        "g",
        "Gate",
        "",
        required_case_ids=("a", "b"),
        critical_case_ids=("a",),
        minimum_pass_rate=Decimal("0.6666"),
        max_failures=1,
    )
    decision = service.evaluate(
        gate,
        [
            result("a", EvaluationStatus.PASS),
            result("b", EvaluationStatus.PASS),
            result("c", EvaluationStatus.FAIL),
        ],
        "run-1",
    )
    assert decision.status == GateDecisionStatus.PASS
    assert decision.pass_rate == Decimal("0.6667")
    assert decision.failed_case_ids == ("c",)
    assert decision.deployment_allowed is True


def test_failure_threshold_and_critical_case(service):
    gate = QualityGateDefinition("g", "Gate", "", critical_case_ids=("a",), max_failures=0)
    decision = service.evaluate(gate, [result("a", EvaluationStatus.FAIL)], "run-2")
    assert decision.status == GateDecisionStatus.FAIL
    assert decision.critical_case_failures == ("a",)
    assert len(decision.reasons) == 2
    assert decision.deployment_allowed is False


@pytest.mark.parametrize(
    "missing_policy,unknown_policy,error_policy,case_status,expected,expected_deploy",
    [
        (MissingCasePolicy.UNKNOWN, UnknownCasePolicy.UNKNOWN, ErrorCasePolicy.ERROR, None, GateDecisionStatus.UNKNOWN, False),
        (MissingCasePolicy.FAIL, UnknownCasePolicy.UNKNOWN, ErrorCasePolicy.ERROR, None, GateDecisionStatus.FAIL, False),
        (MissingCasePolicy.ERROR, UnknownCasePolicy.UNKNOWN, ErrorCasePolicy.ERROR, None, GateDecisionStatus.ERROR, False),
        (MissingCasePolicy.FAIL, UnknownCasePolicy.UNKNOWN, ErrorCasePolicy.ERROR, EvaluationStatus.UNKNOWN, GateDecisionStatus.UNKNOWN, False),
        (MissingCasePolicy.FAIL, UnknownCasePolicy.FAIL, ErrorCasePolicy.ERROR, EvaluationStatus.UNKNOWN, GateDecisionStatus.FAIL, False),
        (MissingCasePolicy.FAIL, UnknownCasePolicy.UNKNOWN, ErrorCasePolicy.ERROR, EvaluationStatus.ERROR, GateDecisionStatus.ERROR, False),
        (MissingCasePolicy.FAIL, UnknownCasePolicy.UNKNOWN, ErrorCasePolicy.FAIL, EvaluationStatus.ERROR, GateDecisionStatus.FAIL, False),
    ],
)
def test_explicit_non_pass_policies(service, missing_policy, unknown_policy, error_policy, case_status, expected, expected_deploy):
    gate = QualityGateDefinition(
        "g",
        "Gate",
        "",
        required_case_ids=("required",),
        missing_case_policy=missing_policy,
        unknown_case_policy=unknown_policy,
        error_case_policy=error_policy,
    )
    results = [] if case_status is None else [result("required", case_status)]
    decision = service.evaluate(gate, results, f"run-{expected}-{case_status}-{missing_policy}")
    assert decision.status == expected
    assert decision.deployment_allowed == expected_deploy


def test_rejects_duplicate_cases_wrong_dataset_and_evaluator_version(service):
    gate = QualityGateDefinition(
        "g",
        "Gate",
        "",
        target_dataset_id="golden",
        target_dataset_version="1.0.0",
        allowed_evaluator_versions=("2.0.0",),
        max_failures=1,
    )
    with pytest.raises(ValueError):
        service.evaluate(gate, [result("a", EvaluationStatus.PASS)], "run-ds", dataset_id="other", dataset_version="1.0.0")
    with pytest.raises(ValueError):
        service.evaluate(gate, [result("a", EvaluationStatus.PASS)] * 2, "run-dupe", dataset_id="golden", dataset_version="1.0.0")
    decision = service.evaluate(gate, [result("a", EvaluationStatus.PASS)], "run-version", dataset_id="golden", dataset_version="1.0.0")
    assert decision.status == GateDecisionStatus.ERROR
    assert decision.deployment_allowed is False


def test_target_dataset_manifest_checksum_validation(service):
    gate = QualityGateDefinition(
        "g",
        "Gate",
        "",
        target_dataset_id="golden",
        target_dataset_version="1.0.0",
        target_dataset_manifest_checksum="manifest-sha-123456",
    )
    # 1. Manifest mismatch -> ValueError
    with pytest.raises(ValueError, match="does not match expected definition manifest checksum"):
        service.evaluate(
            gate,
            [result("a", EvaluationStatus.PASS)],
            "run-manifest-wrong",
            dataset_id="golden",
            dataset_version="1.0.0",
            dataset_manifest_checksum="manifest-sha-DIFFERENT",
        )
    # 2. Missing manifest checksum when required -> Status ERROR
    decision_missing = service.evaluate(
        gate,
        [result("a", EvaluationStatus.PASS)],
        "run-manifest-missing",
        dataset_id="golden",
        dataset_version="1.0.0",
        dataset_manifest_checksum=None,
    )
    assert decision_missing.status == GateDecisionStatus.ERROR
    assert decision_missing.deployment_allowed is False

    # 3. Matching manifest checksum -> Status PASS
    decision_ok = service.evaluate(
        gate,
        [result("a", EvaluationStatus.PASS)],
        "run-manifest-ok",
        dataset_id="golden",
        dataset_version="1.0.0",
        dataset_manifest_checksum="manifest-sha-123456",
    )
    assert decision_ok.status == GateDecisionStatus.PASS
    assert decision_ok.deployment_allowed is True


def test_service_replay_and_idempotency_conflict_detection(service):
    gate = QualityGateDefinition("g", "Gate", "")
    # First evaluation
    first = service.evaluate(gate, [result("a", EvaluationStatus.PASS)], "same-run")
    
    # Replay with identical input -> idempotent return
    replay = service.evaluate(gate, [result("a", EvaluationStatus.PASS)], "same-run")
    assert replay == first

    # Divergent input for the same evaluation_run_id -> GateDecisionConflictError
    with pytest.raises(GateDecisionConflictError, match="Idempotency conflict"):
        service.evaluate(gate, [result("a", EvaluationStatus.FAIL)], "same-run")


def test_repository_detects_conflict_on_direct_save(service):
    gate = QualityGateDefinition("g", "Gate", "")
    dec1 = service.evaluate(gate, [result("a", EvaluationStatus.PASS)], "run-collision-1")
    
    # Intentionally craft a modified decision with same decision_id but different status
    modified_dec = QualityGateDecision(
        decision_id=dec1.decision_id,
        gate_id=dec1.gate_id,
        gate_version=dec1.gate_version,
        status=GateDecisionStatus.FAIL,
        evaluation_run_id=dec1.evaluation_run_id,
        reasons=("Tampered reason",),
    )
    with pytest.raises(GateDecisionConflictError):
        service.repository.save_decision(modified_dec)


def test_definition_checksum_covers_allowed_evaluator_versions_and_provenance():
    first = QualityGateDefinition("g", "Gate", "", allowed_evaluator_versions=("1.0.0",))
    second = QualityGateDefinition("g", "Gate", "", allowed_evaluator_versions=("2.0.0",))
    third = QualityGateDefinition("g", "Gate", "", allowed_evaluator_versions=("1.0.0",), provenance="MANUAL_REVIEW")
    assert first.checksum != second.checksum
    assert first.checksum != third.checksum


def test_repository_detects_tampered_decision(service):
    decision = service.evaluate(
        QualityGateDefinition("g", "Gate", ""),
        [result("a", EvaluationStatus.PASS)],
        "tamper-run",
    )
    decision_file = service.repository.decisions_dir / f"{decision.decision_id}.json"
    payload = json.loads(decision_file.read_text(encoding="utf-8"))
    payload["status"] = GateDecisionStatus.FAIL.value
    decision_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CorruptedQualityGateRecordError):
        service.repository.get_decision(decision.decision_id)


def test_repository_rejects_path_traversal(service):
    with pytest.raises(ValueError):
        service.repository.get_definition("../outside")
    with pytest.raises(ValueError):
        service.repository.get_decision("../outside")
    with pytest.raises(ValueError):
        service.repository.list_definition_versions("../outside")


def test_decision_checksum_covers_evidence_and_dataset(service):
    gate = QualityGateDefinition("g", "Gate", "")
    first = service.evaluate(gate, [result("a", EvaluationStatus.PASS)], "run-a", dataset_id="dataset-a")
    second = service.evaluate(gate, [result("a", EvaluationStatus.PASS)], "run-b", dataset_id="dataset-b")
    assert first.checksum != second.checksum


def test_decision_deep_immutability(service):
    gate = QualityGateDefinition("g", "Gate", "")
    decision = service.evaluate(
        gate,
        [result("a", EvaluationStatus.PASS)],
        "deep-freeze-run",
    )
    with pytest.raises(TypeError):
        decision.evidence["new_key"] = "value"
    with pytest.raises(TypeError):
        decision.metadata["new_key"] = "value"
    with pytest.raises(FrozenInstanceError):
        decision.status = GateDecisionStatus.FAIL


def test_definition_uses_semver_and_requires_complete_dataset_target():
    with pytest.raises(ValueError, match="semantic version"):
        QualityGateDefinition("g", "Gate", "", version="1.0")
    with pytest.raises(ValueError, match="provided together"):
        QualityGateDefinition("g", "Gate", "", target_dataset_id="golden")


def test_repository_selects_latest_definition_by_semver(service):
    for version in ("1.9.0", "1.10.0", "1.10.0-rc.1"):
        service.register_definition(QualityGateDefinition("release", "Release", "", version=version))
    assert service.get_definition("release").version == "1.10.0"
    assert service.repository.list_definition_versions("release") == ["1.10.0", "1.10.0-rc.1", "1.9.0"]


def test_rejects_inconsistent_batch_summary(service):
    evaluation = BatchEvaluationSummary(
        total_cases=2,
        passed_count=2,
        failed_count=0,
        unknown_count=0,
        error_count=0,
        results=(result("a", EvaluationStatus.PASS),),
    )
    with pytest.raises(ValueError, match="counts do not match"):
        service.evaluate(QualityGateDefinition("g", "Gate", ""), evaluation, "bad-summary")


def test_index_recovery(tmp_path):
    repo = JsonQualityGateRepository(tmp_path / "gates-index")
    service = QualityGateService(repo)
    gate = service.register_definition(QualityGateDefinition("g1", "Gate 1", "", version="1.0.0"))
    dec = service.evaluate(gate, [result("a", EvaluationStatus.PASS)], "run-rec-1")

    # Delete index files
    repo.definitions_index_file.unlink()
    repo.decisions_index_file.unlink()
    assert not repo.definitions_index_file.exists()
    assert not repo.decisions_index_file.exists()

    # Recover index
    recovered_count = repo.recover_index()
    assert recovered_count == 2
    assert repo.definitions_index_file.exists()
    assert repo.decisions_index_file.exists()

    # Check that lookup by idempotency key works via recovered index
    found = repo.get_decision_by_idempotency_key(dec.idempotency_key)
    assert found == dec
