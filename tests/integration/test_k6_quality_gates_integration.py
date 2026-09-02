from datetime import datetime, timezone
from decimal import Decimal
import json
import threading

from src.application.audit.audit_trail_service import AuditTrailService
from src.application.evaluation.evaluation_harness_service import EvaluationHarnessService
from src.application.golden_dataset.dataset_service import GoldenDatasetService
from src.application.golden_dataset.dataset_validator import DeterministicGoldenDatasetValidator
from src.application.quality_gate.quality_gate_service import QualityGateService
from src.domain.audit.models import AuditRecordType
from src.domain.evaluation.models import EvaluationCase, EvaluationStatus, EvaluationType, EvaluationResult
from src.domain.golden_dataset.models import GoldenDataset
from src.domain.quality_gate.models import GateDecisionStatus, QualityGateDefinition
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.evaluation_repository import JsonEvaluationRepository
from src.infrastructure.persistence.data.json.golden_dataset_repository import JsonGoldenDatasetRepository
from src.infrastructure.persistence.data.json.quality_gate_repository import (
    GateDecisionConflictError,
    JsonQualityGateRepository,
)


def test_k6_e2e_k4_k5_batch_to_persisted_gate_decision(tmp_path):
    eval_repo = JsonEvaluationRepository(tmp_path / "evaluation")
    dataset_service = GoldenDatasetService(
        JsonGoldenDatasetRepository(tmp_path / "datasets"), eval_repo,
        DeterministicGoldenDatasetValidator(), EvaluationHarnessService(eval_repo),
    )
    cases = [
        EvaluationCase("price", "Price", "", EvaluationType.EXACT_MATCH, expected_criteria={"value": 10}),
        EvaluationCase("stock", "Stock", "", EvaluationType.EXACT_MATCH, expected_criteria={"value": True}),
    ]
    dataset = dataset_service.create_dataset_from_cases(
        "release-golden", "Release", "", "1.0.0", cases, domain_scope="release",
    )
    batch = dataset_service.evaluate_dataset(
        dataset.dataset_id,
        lambda case: 10 if case.case_id == "price" else True,
        version=dataset.version,
    )
    assert batch.passed_count == 2

    repo_dir = tmp_path / "quality-gates"
    audit_repo = JsonAuditRepository(tmp_path / "audit")
    service = QualityGateService(JsonQualityGateRepository(repo_dir), audit_repository=audit_repo)
    gate = service.register_definition(QualityGateDefinition(
        "release-gate", "Release gate", "", version="1.0.0",
        target_dataset_id=dataset.dataset_id, target_dataset_version=dataset.version,
        target_dataset_manifest_checksum=dataset.checksum,
        required_case_ids=dataset.case_ids, critical_case_ids=("price",),
        minimum_pass_rate=Decimal("1.0"), allowed_evaluator_versions=("1.0.0",),
    ))
    decision = service.evaluate_registered(
        gate.gate_id, batch, "evaluation-run-1", version=gate.version,
        dataset_id=dataset.dataset_id, dataset_version=dataset.version,
        dataset_manifest_checksum=dataset.checksum,
    )
    assert decision.status == GateDecisionStatus.PASS
    assert decision.pass_rate == Decimal("1.0000")
    assert decision.deployment_allowed is True

    # Check Audit Record emitted in K.1
    audit_records = audit_repo.list_records(subject_id=gate.gate_id)
    assert len(audit_records) == 1
    assert audit_records[0].record_type == AuditRecordType.DECISION_CREATED
    assert audit_records[0].metadata["deployment_allowed"] is True
    assert audit_records[0].status == "PASS"

    # Restart and verify replay does NOT duplicate audit records
    restarted = QualityGateService(JsonQualityGateRepository(repo_dir), audit_repository=audit_repo)
    assert restarted.get_definition(gate.gate_id, gate.version) == gate
    assert restarted.repository.get_decision(decision.decision_id) == decision
    replay = restarted.evaluate_registered(
        gate.gate_id, batch, "evaluation-run-1", version=gate.version,
        dataset_id=dataset.dataset_id, dataset_version=dataset.version,
        dataset_manifest_checksum=dataset.checksum,
    )
    assert replay == decision
    assert len(audit_repo.list_records(subject_id=gate.gate_id)) == 1


def test_k6_detects_regression_without_reexecuting_target(tmp_path):
    now = datetime.now(timezone.utc)

    results = [
        EvaluationResult("r1", "critical", "e1", "component", now, now, EvaluationStatus.FAIL),
        EvaluationResult("r2", "normal", "e2", "component", now, now, EvaluationStatus.PASS),
    ]
    service = QualityGateService(JsonQualityGateRepository(tmp_path / "gates"))
    gate = QualityGateDefinition(
        "regression", "Regression", "", required_case_ids=("critical", "normal"),
        critical_case_ids=("critical",), minimum_pass_rate=Decimal("1.0"), max_failures=0,
    )
    decision = service.evaluate(gate, results, "historical-run")
    assert decision.status == GateDecisionStatus.FAIL
    assert decision.deployment_allowed is False
    assert decision.critical_case_failures == ("critical",)
    assert decision.evidence["result_ids"] == ("r1", "r2")


def test_k6_repository_skips_corrupt_decision_when_listing(tmp_path):
    repository = JsonQualityGateRepository(tmp_path / "gates")
    service = QualityGateService(repository)
    gate = QualityGateDefinition("g", "Gate", "")
    now = datetime.now(timezone.utc)
    good = service.evaluate(gate, [EvaluationResult("r", "c", "e", "x", now, now, EvaluationStatus.PASS)], "run")
    (repository.decisions_dir / "corrupt.json").write_text("{broken", encoding="utf-8")
    assert repository.list_decisions() == [good]


def test_k6_concurrent_evaluations_and_saves(tmp_path):
    repo = JsonQualityGateRepository(tmp_path / "gates-concurrent")
    service = QualityGateService(repo)
    gate = service.register_definition(QualityGateDefinition("concurrent-gate", "Gate", ""))
    
    now = datetime.now(timezone.utc)
    results = [EvaluationResult("r1", "c1", "e1", "x", now, now, EvaluationStatus.PASS)]

    errors = []

    def worker(worker_id):
        try:
            # Different run IDs save concurrently
            service.evaluate(gate, results, f"concurrent-run-{worker_id}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    saved_decisions = repo.list_decisions(gate_id=gate.gate_id)
    assert len(saved_decisions) == 10
