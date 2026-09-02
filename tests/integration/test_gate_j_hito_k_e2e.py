"""Validación E2E transversal de Gate J sobre los servicios reales K.1-K.8."""

import threading
from decimal import Decimal

import pytest

from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.application.audit.audit_trail_service import AuditTrailService
from src.application.cost.cost_tracking_service import CostTrackingService
from src.application.cost.pricing_catalog import InMemoryPricingCatalog
from src.application.evaluation.evaluation_harness_service import EvaluationHarnessService
from src.application.golden_dataset.dataset_service import GoldenDatasetService
from src.application.golden_dataset.dataset_validator import DeterministicGoldenDatasetValidator
from src.application.quality_gate.quality_gate_service import QualityGateService
from src.application.reliability.reliability_engine import ReliabilityEngine
from src.application.security.security_check_service import SecurityCheckService
from src.domain.agent_trace.models import StepType, TraceStatus
from src.domain.cost.models import PricingRate
from src.domain.evaluation.models import EvaluationCase, EvaluationStatus, EvaluationType
from src.domain.quality_gate.models import GateDecisionStatus, QualityGateDefinition
from src.domain.reliability.models import RetryPolicy
from src.domain.security.models import SecurityCheckStatus
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.audit_repository import (
    CorruptedAuditRecordError,
    JsonAuditRepository,
)
from src.infrastructure.persistence.data.json.cost_repository import JsonCostRepository
from src.infrastructure.persistence.data.json.evaluation_repository import JsonEvaluationRepository
from src.infrastructure.persistence.data.json.golden_dataset_repository import JsonGoldenDatasetRepository
from src.infrastructure.persistence.data.json.quality_gate_repository import JsonQualityGateRepository
from src.infrastructure.reliability.reliability_infrastructure import JsonIdempotencyStore, VirtualClock


def _build_stack(base):
    audit_repo = JsonAuditRepository(base / "audit")
    trace_repo = JsonAgentTraceRepository(base / "traces")
    cost_repo = JsonCostRepository(base / "costs")
    evaluation_repo = JsonEvaluationRepository(base / "evaluations")
    dataset_repo = JsonGoldenDatasetRepository(base / "datasets")
    gate_repo = JsonQualityGateRepository(base / "gates")

    audit = AuditTrailService(audit_repo)
    trace = AgentTraceService(trace_repo)
    catalog = InMemoryPricingCatalog()
    catalog.register_rate(PricingRate(
        provider="marketplace",
        service_or_model="publish_listing",
        currency="USD",
        flat_rate=Decimal("0.01"),
        rate_scale=Decimal("1"),
        version="1.0.0",
    ))
    cost = CostTrackingService(cost_repo, catalog, audit_repo)
    evaluation = EvaluationHarnessService(evaluation_repo, audit_repository=audit_repo)
    datasets = GoldenDatasetService(
        dataset_repo,
        evaluation_repo,
        DeterministicGoldenDatasetValidator(),
        evaluation,
    )
    gates = QualityGateService(gate_repo, audit_repository=audit_repo)
    reliability = ReliabilityEngine(
        idempotency_store=JsonIdempotencyStore(str(base / "idempotency")),
        clock=VirtualClock(),
        audit_trail_service=audit,
        agent_trace_service=trace,
    )
    security = SecurityCheckService(
        audit_trail_service=audit,
        agent_trace_service=trace,
        allowed_actors=("commerce-agent",),
        prohibited_actions=("DELETE_DATABASE",),
    )
    return {
        "audit_repo": audit_repo,
        "trace": trace,
        "cost": cost,
        "evaluation": evaluation,
        "datasets": datasets,
        "gates": gates,
        "reliability": reliability,
        "security": security,
    }


def _create_release_dataset(stack):
    cases = (
        EvaluationCase(
            "authorized-action",
            "Authorized action",
            "The protected action completes successfully",
            EvaluationType.EXACT_MATCH,
            expected_criteria={"status": "SUCCESS"},
            tags=("gate-j", "security"),
        ),
        EvaluationCase(
            "observable-cost",
            "Observable cost",
            "The operation emits linked observability",
            EvaluationType.EXACT_MATCH,
            expected_criteria={"observable": True},
            tags=("gate-j", "observability"),
        ),
    )
    return stack["datasets"].create_dataset_from_cases(
        "gate-j-k-release",
        "Gate J K release",
        "Cross-K release baseline",
        "1.0.0",
        cases,
        domain_scope="gate-j",
        metadata={"api_token": "must-not-persist"},
    )


def _register_gate(stack, dataset, gate_id="gate-j"):
    return stack["gates"].register_definition(QualityGateDefinition(
        gate_id,
        "Gate J",
        "Release decision over the K baseline",
        target_dataset_id=dataset.dataset_id,
        target_dataset_version=dataset.version,
        target_dataset_manifest_checksum=dataset.checksum,
        required_case_ids=dataset.case_ids,
        critical_case_ids=("authorized-action",),
        minimum_pass_rate=Decimal("1.0"),
        allowed_evaluator_versions=("1.0.0",),
    ))


def test_gate_j_cross_k_happy_reliability_replay_and_restart(tmp_path):
    stack = _build_stack(tmp_path)
    mission_id = "mission-gate-j-001"
    execution_id = "execution-gate-j-001"

    security = stack["security"].evaluate_action_security(
        "PUBLISH_LISTING",
        "commerce-agent",
        {"listing_id": "MLC-100", "mission_id": mission_id},
        correlation_id=execution_id,
        context_metadata={"refresh_token": "secret-never-visible", "channel": "release"},
    )
    assert security.allowed is True

    trace = stack["trace"].record_step(
        "GateJRunner",
        execution_id,
        1,
        StepType.TOOL_CALL,
        "publish_listing",
        TraceStatus.SUCCESS,
        tool_or_service="marketplace/publish_listing",
        mission_id=mission_id,
        correlation_id=execution_id,
        metadata={"authorization": "Bearer hidden", "safe": "retained"},
    )
    cost = stack["cost"].record_tool_cost(
        execution_id,
        "publish_listing",
        provider="marketplace",
        trace_id=trace.trace_id,
        mission_id=mission_id,
        correlation_id=execution_id,
    )
    assert cost.total_cost == Decimal("0.010000")

    attempts = 0

    def transient_publish():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary marketplace connection failure")
        return {"status": "SUCCESS", "observable": True}

    reliable = stack["reliability"].execute_with_reliability(
        "publish-release",
        transient_publish,
        is_side_effect=False,
        retry_policy=RetryPolicy(max_attempts=2),
        correlation_id=execution_id,
    )
    assert reliable.is_success is True
    assert reliable.attempts_executed == 2

    dataset = _create_release_dataset(stack)
    batch = stack["datasets"].evaluate_dataset(
        dataset.dataset_id,
        lambda case: (
            {"status": reliable.output["status"]}
            if case.case_id == "authorized-action"
            else {"observable": reliable.output["observable"]}
        ),
        version=dataset.version,
    )
    assert batch.passed_count == 2

    gate = _register_gate(stack, dataset)
    decision = stack["gates"].evaluate_registered(
        gate.gate_id,
        batch,
        "release-run-001",
        version=gate.version,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_manifest_checksum=dataset.checksum,
        correlation_id=execution_id,
    )
    assert decision.status == GateDecisionStatus.PASS
    assert decision.deployment_allowed is True

    effect_count = 0

    def side_effect():
        nonlocal effect_count
        effect_count += 1
        return {"publication_id": "PUB-001"}

    first = stack["reliability"].execute_with_reliability(
        "commit-publication",
        side_effect,
        is_side_effect=True,
        idempotency_key="gate-j-publication-001",
        payload={"listing_id": "MLC-100"},
    )
    replay = stack["reliability"].execute_with_reliability(
        "commit-publication-replay",
        side_effect,
        is_side_effect=True,
        idempotency_key="gate-j-publication-001",
        payload={"listing_id": "MLC-100"},
    )
    assert replay.output == first.output
    assert effect_count == 1

    restarted = _build_stack(tmp_path)
    assert restarted["trace"].get_execution_timeline(execution_id).total_steps >= 1
    assert restarted["cost"].get_summary(mission_id=mission_id).by_currency["USD"].known_total == Decimal("0.010000")
    assert restarted["datasets"].get_dataset(dataset.dataset_id, dataset.version) == dataset
    assert restarted["gates"].repository.get_decision(decision.decision_id) == decision

    replay_after_restart = restarted["reliability"].execute_with_reliability(
        "commit-publication-after-restart",
        side_effect,
        is_side_effect=True,
        idempotency_key="gate-j-publication-001",
        payload={"listing_id": "MLC-100"},
    )
    assert replay_after_restart.output == first.output
    assert effect_count == 1
    persisted = str(restarted["audit_repo"].list_records())
    assert "secret-never-visible" not in persisted
    assert "Bearer hidden" not in str(restarted["trace"].list_records())


def test_gate_j_cross_k_regression_is_release_blocking(tmp_path):
    stack = _build_stack(tmp_path)
    dataset = _create_release_dataset(stack)
    batch = stack["datasets"].evaluate_dataset(
        dataset.dataset_id,
        lambda case: (
            {"status": "FAILED"}
            if case.case_id == "authorized-action"
            else {"observable": True}
        ),
        version=dataset.version,
    )
    gate = _register_gate(stack, dataset, gate_id="gate-j-regression")
    decision = stack["gates"].evaluate_registered(
        gate.gate_id,
        batch,
        "regression-run",
        version=gate.version,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_manifest_checksum=dataset.checksum,
    )
    assert batch.failed_count == 1
    assert decision.status == GateDecisionStatus.FAIL
    assert decision.deployment_allowed is False
    assert decision.critical_case_failures == ("authorized-action",)


def test_gate_j_unknown_is_preserved_and_blocks_release(tmp_path):
    stack = _build_stack(tmp_path)
    case = EvaluationCase(
        "uncertain-marketplace",
        "Uncertain marketplace",
        "An ambiguous external result stays UNKNOWN",
        EvaluationType.EXACT_MATCH,
        expected_criteria={"status": "SUCCESS"},
    )
    dataset = stack["datasets"].create_dataset_from_cases(
        "gate-j-unknown-data", "Unknown baseline", "", "1.0.0", (case,),
    )
    batch = stack["datasets"].evaluate_dataset(
        dataset.dataset_id, lambda _: {"status": "UNKNOWN"}, version=dataset.version,
    )
    gate = stack["gates"].register_definition(QualityGateDefinition(
        "gate-j-unknown", "Unknown gate", "",
        target_dataset_id=dataset.dataset_id,
        target_dataset_version=dataset.version,
        target_dataset_manifest_checksum=dataset.checksum,
        required_case_ids=dataset.case_ids,
    ))
    decision = stack["gates"].evaluate_registered(
        gate.gate_id, batch, "unknown-run", version=gate.version,
        dataset_id=dataset.dataset_id, dataset_version=dataset.version,
        dataset_manifest_checksum=dataset.checksum,
    )
    assert batch.results[0].status == EvaluationStatus.UNKNOWN
    assert decision.status == GateDecisionStatus.UNKNOWN
    assert decision.deployment_allowed is False


def test_gate_j_security_corruption_and_altered_replay_fail_secure(tmp_path):
    stack = _build_stack(tmp_path)
    denied = stack["security"].evaluate_action_security(
        "EXPORT_DATA",
        "intruder",
        {"filename": "../../secrets.json", "chain_of_thought": "private"},
        context_metadata={"api_key": "raw-secret"},
    )
    assert denied.status == SecurityCheckStatus.FAIL
    assert denied.allowed is False

    executed = 0

    def mutate():
        nonlocal executed
        executed += 1
        return {"status": "UPDATED"}

    first = stack["reliability"].execute_with_reliability(
        "price-update", mutate, True,
        idempotency_key="price-key", payload={"price": 100},
    )
    conflict = stack["reliability"].execute_with_reliability(
        "price-update-tampered", mutate, True,
        idempotency_key="price-key", payload={"price": 1},
    )
    assert first.is_success is True
    assert conflict.status == "IDEMPOTENCY_CONFLICT"
    assert executed == 1

    audit_file = next((tmp_path / "audit" / "audit_records").glob("*.json"))
    content = audit_file.read_text(encoding="utf-8")
    audit_file.write_text(content.replace('"status":', '"status": "TAMPERED", "original_status":', 1), encoding="utf-8")
    with pytest.raises(CorruptedAuditRecordError):
        JsonAuditRepository(tmp_path / "audit")


def test_gate_j_concurrent_same_operation_has_one_effect(tmp_path):
    stack = _build_stack(tmp_path)
    effect_count = 0
    effect_lock = threading.Lock()
    outputs = []
    errors = []

    def effect():
        nonlocal effect_count
        with effect_lock:
            effect_count += 1
        return {"result_id": "ONE"}

    def worker(worker_id):
        try:
            result = stack["reliability"].execute_with_reliability(
                f"concurrent-{worker_id}", effect, True,
                idempotency_key="shared-operation", payload={"sku": "SKU-1"},
            )
            outputs.append(result.output)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert effect_count == 1
    assert outputs == [{"result_id": "ONE"}] * 12
