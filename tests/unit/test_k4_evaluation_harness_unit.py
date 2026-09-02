"""
Tests Unitarios Exhaustivos para K.4 Evaluation Harness.

Cubre:
A. Immutable EvaluationCase
B. Immutable EvaluationResult
C. PASS
D. FAIL
E. UNKNOWN
F. ERROR
G. Exact match evaluator
H. Structural evaluator
I. Numeric evaluator
J. Status evaluator
K. Trace evaluator
L. Expected criteria
M. Metrics structured
N. Evaluator version
O. Case version
P. Deterministic replay
Q. Idempotency
R. Persistence
S. Restart / Reload
T. Run single case
U. Run batch
V. Batch failure isolation
W. Audit link
X. Trace link
Y. Cost link
Z. Sanitization / Security
AA. No Golden Dataset (K.5 boundary check)
AB. No Quality Gate (K.6 boundary check)
AC. No LLM judge.
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
import shutil
import tempfile
import pytest

from src.domain.evaluation.models import (
    EvaluationType,
    EvaluationStatus,
    EvaluationMetric,
    EvaluationCase,
    EvaluationResult,
    BatchEvaluationSummary,
)
from src.domain.evaluation.evaluators import (
    ExactMatchEvaluator,
    StructuralEvaluator,
    NumericToleranceEvaluator,
    StatusEvaluator,
    PolicyEvaluator,
    SafetyEvaluator,
    TraceEvaluator,
    IdempotencyEvaluator,
    EndToEndEvaluator,
    EvaluatorRegistry,
)
from src.infrastructure.persistence.data.json.evaluation_repository import (
    JsonEvaluationRepository,
    CorruptedEvaluationRecordError,
)
from src.application.evaluation.evaluation_harness_service import (
    EvaluationHarnessService,
    CallableTargetAdapter,
)
from src.domain.audit.models import AuditRecordType, AuditActor, AuditActorType
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository


@pytest.fixture
def tmp_eval_repo(tmp_path):
    repo_dir = tmp_path / "eval_data"
    return JsonEvaluationRepository(repo_dir)


def test_a_immutable_evaluation_case():
    case = EvaluationCase(
        case_id="case-100",
        name="Exact Match Test",
        description="Verify status field",
        evaluation_type=EvaluationType.EXACT_MATCH,
        input_reference={"sku": "SKU-1"},
        expected_criteria={"status": "ACTIVE"},
        tags=("listing", "smoke"),
    )
    with pytest.raises(Exception):
        case.name = "Modified"  # type: ignore
    with pytest.raises(Exception):
        case.input_reference["sku"] = "SKU-2"  # type: ignore
    assert case.case_id == "case-100"
    assert case.tags == ("listing", "smoke")


def test_b_immutable_evaluation_result():
    now = datetime.now(timezone.utc)
    res = EvaluationResult(
        result_id="res-100",
        case_id="case-100",
        execution_id="exec-100",
        evaluated_component="TestComponent",
        started_at=now,
        completed_at=now,
        status=EvaluationStatus.PASS,
        expected_reference={"status": "ACTIVE"},
        actual_reference={"status": "ACTIVE"},
    )
    with pytest.raises(Exception):
        res.status = EvaluationStatus.FAIL  # type: ignore
    assert res.result_id == "res-100"
    assert res.status == EvaluationStatus.PASS
    assert res.duration_ms >= 0.0


def test_c_pass_status_and_g_exact_match():
    evaluator = ExactMatchEvaluator()
    case = EvaluationCase(
        case_id="case-exact-1",
        name="Exact Match",
        description="Test exact match",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"price": "100.00", "currency": "USD"},
    )
    actual = {"price": "100.00", "currency": "USD"}
    res = evaluator.evaluate(
        case=case,
        actual_output=actual,
        execution_id="exec-1",
        evaluated_component="PricingEngine",
    )
    assert res.status == EvaluationStatus.PASS
    assert len(res.metrics) == 2
    assert all(m.status == EvaluationStatus.PASS for m in res.metrics)


def test_d_fail_status():
    evaluator = ExactMatchEvaluator()
    case = EvaluationCase(
        case_id="case-exact-2",
        name="Exact Match Fail",
        description="Test exact match mismatch",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"price": "100.00"},
    )
    actual = {"price": "95.00"}
    res = evaluator.evaluate(
        case=case,
        actual_output=actual,
        execution_id="exec-1",
        evaluated_component="PricingEngine",
    )
    assert res.status == EvaluationStatus.FAIL
    assert res.metrics[0].status == EvaluationStatus.FAIL


def test_e_unknown_status_preservation():
    evaluator = ExactMatchEvaluator()
    case = EvaluationCase(
        case_id="case-exact-3",
        name="Exact Match Unknown",
        description="Test unknown propagation",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"price": "100.00"},
    )
    actual = {"price": "UNKNOWN"}
    res = evaluator.evaluate(
        case=case,
        actual_output=actual,
        execution_id="exec-1",
        evaluated_component="PricingEngine",
    )
    assert res.status == EvaluationStatus.UNKNOWN


def test_f_error_status_in_harness(tmp_eval_repo):
    harness = EvaluationHarnessService(repository=tmp_eval_repo, isolate_failures=True)
    case = EvaluationCase(
        case_id="case-err-1",
        name="Error case",
        description="Target raises exception",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"status": "OK"},
    )

    def failing_target(c):
        raise RuntimeError("Service timeout connecting to database")

    res = harness.run_case(case, failing_target)
    assert res.status == EvaluationStatus.ERROR
    assert "Service timeout" in res.actual_reference.get("error", "")


def test_h_structural_evaluator():
    evaluator = StructuralEvaluator()
    case = EvaluationCase(
        case_id="case-struct-1",
        name="Structural schema check",
        description="Required fields presence",
        evaluation_type=EvaluationType.STRUCTURAL,
        expected_criteria={
            "required_fields": ["item_id", "title", "price"],
            "forbidden_fields": ["internal_cost", "supplier_secret"],
        },
    )
    actual = {
        "item_id": "MLA-123",
        "title": "Kingston SSD 480GB",
        "price": 45000,
    }
    res = evaluator.evaluate(case, actual, "exec-struct-1", "ListingGenerator")
    assert res.status == EvaluationStatus.PASS

    # Fail case: missing required field
    bad_actual = {"item_id": "MLA-123", "supplier_secret": "xyz"}
    res_fail = evaluator.evaluate(case, bad_actual, "exec-struct-2", "ListingGenerator")
    assert res_fail.status == EvaluationStatus.FAIL


def test_i_numeric_tolerance_evaluator():
    evaluator = NumericToleranceEvaluator()
    case = EvaluationCase(
        case_id="case-num-1",
        name="Numeric Range Check",
        description="Margin and price within limits",
        evaluation_type=EvaluationType.NUMERIC,
        expected_criteria={
            "margin_pct": {"min": "0.15", "max": "0.40"},
            "price": {"expected": "100.00", "tolerance": "2.00"},
        },
    )
    # Pass
    actual_ok = {"margin_pct": Decimal("0.25"), "price": Decimal("101.50")}
    res_ok = evaluator.evaluate(case, actual_ok, "exec-num-1", "PricingService")
    assert res_ok.status == EvaluationStatus.PASS

    # Fail
    actual_bad = {"margin_pct": Decimal("0.10"), "price": Decimal("110.00")}
    res_bad = evaluator.evaluate(case, actual_bad, "exec-num-2", "PricingService")
    assert res_bad.status == EvaluationStatus.FAIL


def test_j_status_evaluator():
    evaluator = StatusEvaluator()
    case = EvaluationCase(
        case_id="case-stat-1",
        name="Status Check",
        description="Verify allowed terminal statuses",
        evaluation_type=EvaluationType.STATUS,
        expected_criteria={"allowed_statuses": ["COMPLETED", "SUCCESS"]},
    )
    res_pass = evaluator.evaluate(case, {"status": "SUCCESS"}, "exec-st-1", "Mission")
    assert res_pass.status == EvaluationStatus.PASS

    res_fail = evaluator.evaluate(case, {"status": "FAILED"}, "exec-st-2", "Mission")
    assert res_fail.status == EvaluationStatus.FAIL

    res_unk = evaluator.evaluate(case, {"status": "UNKNOWN"}, "exec-st-3", "Mission")
    assert res_unk.status == EvaluationStatus.UNKNOWN


def test_k_trace_evaluator():
    evaluator = TraceEvaluator()
    case = EvaluationCase(
        case_id="case-trace-1",
        name="Trace Steps Verification",
        description="Verify required operational steps in trace",
        evaluation_type=EvaluationType.TRACE,
        expected_criteria={
            "required_step_types": ["START", "OBSERVE", "POLICY_EVALUATION", "COMPLETE"],
            "expected_final_status": "SUCCESS",
        },
    )
    actual_trace = [
        {"step_type": "START", "status": "SUCCESS"},
        {"step_type": "OBSERVE", "status": "SUCCESS"},
        {"step_type": "POLICY_EVALUATION", "status": "SUCCESS"},
        {"step_type": "COMPLETE", "status": "SUCCESS"},
    ]
    res = evaluator.evaluate(case, actual_trace, "exec-tr-1", "AgentTraceService")
    assert res.status == EvaluationStatus.PASS

    # Fail if missing POLICY_EVALUATION
    incomplete_trace = [
        {"step_type": "START", "status": "SUCCESS"},
        {"step_type": "COMPLETE", "status": "SUCCESS"},
    ]
    res_fail = evaluator.evaluate(case, incomplete_trace, "exec-tr-2", "AgentTraceService")
    assert res_fail.status == EvaluationStatus.FAIL


def test_policy_evaluator():
    evaluator = PolicyEvaluator()
    case = EvaluationCase(
        case_id="case-pol-1",
        name="Policy Deny Check",
        description="Verify policy engine decision is DENY",
        evaluation_type=EvaluationType.POLICY,
        expected_criteria={
            "expected_decision": "DENY",
            "expected_violations_count": 1,
        },
    )
    actual = {
        "decision_type": "DENY",
        "violations": [{"rule_id": "PRICE_FLOOR_VIOLATION"}],
    }
    res = evaluator.evaluate(case, actual, "exec-pol-1", "PolicyEngine")
    assert res.status == EvaluationStatus.PASS


def test_safety_evaluator():
    evaluator = SafetyEvaluator()
    case = EvaluationCase(
        case_id="case-safe-1",
        name="Safety secret exclusion",
        description="No API keys or passwords in payload",
        evaluation_type=EvaluationType.SAFETY,
        expected_criteria={"forbidden_substrings": ["secret_key", "bearer_token", "password123"]},
    )
    safe_out = {"status": "OK", "user": "alice"}
    res_safe = evaluator.evaluate(case, safe_out, "exec-safe-1", "APIGateway")
    assert res_safe.status == EvaluationStatus.PASS

    leaked_out = {"status": "OK", "dump": "secret_key=xyz123"}
    res_leaked = evaluator.evaluate(case, leaked_out, "exec-safe-2", "APIGateway")
    assert res_leaked.status == EvaluationStatus.FAIL


def test_idempotency_evaluator():
    evaluator = IdempotencyEvaluator()
    case = EvaluationCase(
        case_id="case-idem-1",
        name="Idempotent Replay Check",
        description="Run 1 and Run 2 must match exactly",
        evaluation_type=EvaluationType.IDEMPOTENCY,
        expected_criteria={"match": True},
    )
    actual_match = {
        "run_1": {"action_id": "ACT-1", "status": "COMPLETED"},
        "run_2": {"action_id": "ACT-1", "status": "COMPLETED"},
    }
    res = evaluator.evaluate(case, actual_match, "exec-idem-1", "ContinuousMissionService")
    assert res.status == EvaluationStatus.PASS

    actual_mismatch = {
        "run_1": {"action_id": "ACT-1", "status": "COMPLETED"},
        "run_2": {"action_id": "ACT-2", "status": "COMPLETED"},
    }
    res_mismatch = evaluator.evaluate(case, actual_mismatch, "exec-idem-2", "ContinuousMissionService")
    assert res_mismatch.status == EvaluationStatus.FAIL


def test_l_expected_criteria_and_m_metrics():
    evaluator = ExactMatchEvaluator()
    case = EvaluationCase(
        case_id="case-metrics-1",
        name="Metrics test",
        description="Verify metrics structured object",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"field_a": 10, "field_b": 20},
    )
    res = evaluator.evaluate(case, {"field_a": 10, "field_b": 99}, "exec-1", "Component")
    assert len(res.metrics) == 2
    metric_a = next(m for m in res.metrics if m.metric_name == "exact_match_field_a")
    metric_b = next(m for m in res.metrics if m.metric_name == "exact_match_field_b")
    assert metric_a.status == EvaluationStatus.PASS
    assert metric_b.status == EvaluationStatus.FAIL
    assert metric_b.to_dict()["status"] == "FAIL"


def test_n_evaluator_version_and_o_case_version():
    case = EvaluationCase(
        case_id="case-ver-1",
        name="Versioned case",
        description="Case with explicit version",
        evaluation_type=EvaluationType.EXACT_MATCH,
        version="2.1.0",
        expected_criteria={"a": 1},
    )
    evaluator = ExactMatchEvaluator()
    res = evaluator.evaluate(case, {"a": 1}, "exec-ver-1", "Comp")
    assert case.version == "2.1.0"
    assert res.evaluator_version == "1.0.0"


def test_p_deterministic_replay_and_q_idempotency(tmp_eval_repo):
    harness = EvaluationHarnessService(repository=tmp_eval_repo)
    case = EvaluationCase(
        case_id="case-rep-1",
        name="Replay Test",
        description="Deterministic replay",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"x": "val"},
    )
    target = lambda c: {"x": "val"}
    res_1 = harness.run_case(case, target)
    res_2 = harness.run_case(case, target)
    assert res_1.status == EvaluationStatus.PASS
    assert res_2.status == EvaluationStatus.PASS


def test_r_persistence_and_s_restart(tmp_path):
    repo_dir = tmp_path / "restart_eval_data"
    repo1 = JsonEvaluationRepository(repo_dir)

    case = EvaluationCase(
        case_id="case-restart-1",
        name="Persistent Case",
        description="Testing reload",
        evaluation_type=EvaluationType.EXACT_MATCH,
        input_reference={"input": 42},
        expected_criteria={"output": 84},
    )
    repo1.save_case(case)

    now = datetime.now(timezone.utc)
    result = EvaluationResult(
        result_id="res-restart-1",
        case_id=case.case_id,
        execution_id="exec-restart-1",
        evaluated_component="Multiplier",
        started_at=now,
        completed_at=now,
        status=EvaluationStatus.PASS,
        metrics=(
            EvaluationMetric(metric_name="exact_match_output", metric_value=84, expected_value=84, status=EvaluationStatus.PASS),
        ),
        expected_reference={"output": 84},
        actual_reference={"output": 84},
        trace_reference="trace-123",
        audit_reference="audit-456",
        cost_reference="cost-789",
    )
    repo1.save_result(result)

    # Simular reinicio creando una nueva instancia del repositorio en el mismo path
    repo2 = JsonEvaluationRepository(repo_dir)
    loaded_case = repo2.get_case("case-restart-1")
    loaded_result = repo2.get_result("res-restart-1")

    assert loaded_case is not None
    assert loaded_case.name == "Persistent Case"
    assert loaded_case.expected_criteria["output"] == 84

    assert loaded_result is not None
    assert loaded_result.status == EvaluationStatus.PASS
    assert loaded_result.trace_reference == "trace-123"
    assert loaded_result.audit_reference == "audit-456"
    assert loaded_result.cost_reference == "cost-789"
    assert len(loaded_result.metrics) == 1


def test_t_run_single_case_and_u_run_batch_and_v_failure_isolation(tmp_eval_repo):
    harness = EvaluationHarnessService(repository=tmp_eval_repo, isolate_failures=True)

    case_pass = EvaluationCase(
        case_id="c-pass",
        name="Pass Case",
        description="Will pass",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"val": 1},
    )
    case_fail = EvaluationCase(
        case_id="c-fail",
        name="Fail Case",
        description="Will fail",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"val": 1},
    )
    case_unk = EvaluationCase(
        case_id="c-unk",
        name="Unknown Case",
        description="Will be unknown",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"val": 1},
    )
    case_err = EvaluationCase(
        case_id="c-err",
        name="Error Case",
        description="Will error",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"val": 1},
    )

    def target(case: EvaluationCase):
        if case.case_id == "c-pass":
            return {"val": 1}
        elif case.case_id == "c-fail":
            return {"val": 2}
        elif case.case_id == "c-unk":
            return {"val": "UNKNOWN"}
        elif case.case_id == "c-err":
            raise RuntimeError("Database connection reset")

    summary = harness.run_batch([case_pass, case_fail, case_unk, case_err], target)
    assert summary.total_cases == 4
    assert summary.passed_count == 1
    assert summary.failed_count == 1
    assert summary.unknown_count == 1
    assert summary.error_count == 1


def test_w_audit_link(tmp_path):
    audit_dir = tmp_path / "audit_dir"
    eval_dir = tmp_path / "eval_dir"
    audit_repo = JsonAuditRepository(audit_dir)
    eval_repo = JsonEvaluationRepository(eval_dir)

    harness = EvaluationHarnessService(
        repository=eval_repo,
        audit_repository=audit_repo,
    )
    case = EvaluationCase(
        case_id="case-audit-1",
        name="Audit link check",
        description="Emit audit record",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"ok": True},
    )
    res = harness.run_case(case, lambda c: {"ok": True})
    assert res.status == EvaluationStatus.PASS

    audit_records = audit_repo.list_records(limit=10)
    assert len(audit_records) >= 1
    matching = [r for r in audit_records if r.subject_id == res.result_id]
    assert len(matching) == 1
    assert matching[0].action_or_operation == "EXECUTE_EVALUATION_CASE"


def test_x_trace_link_and_y_cost_link(tmp_eval_repo):
    harness = EvaluationHarnessService(repository=tmp_eval_repo)
    case = EvaluationCase(
        case_id="case-links-1",
        name="Trace & Cost link check",
        description="Preserve trace_ref and cost_ref",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"status": "DONE"},
    )
    target = lambda c: {
        "output": {"status": "DONE"},
        "execution_id": "exec-999",
        "trace_reference": "trace-trace-999",
        "cost_reference": "cost-cost-999",
        "correlation_id": "corr-123",
    }
    res = harness.run_case(case, target)
    assert res.status == EvaluationStatus.PASS
    assert res.trace_reference == "trace-trace-999"
    assert res.cost_reference == "cost-cost-999"
    assert res.correlation_id == "corr-123"


def test_z_sanitization_and_security(tmp_eval_repo):
    case = EvaluationCase(
        case_id="case-sec-1",
        name="Security Case",
        description="Sanitization test",
        evaluation_type=EvaluationType.EXACT_MATCH,
        input_reference={"api_key": "SECRET-123", "normal": "abc"},
        expected_criteria={"password": "PASSWORD-456", "expected_status": "OK"},
        metadata={"token": "BEARER-789"},
    )
    assert case.input_reference["api_key"] == "[REDACTED]"
    assert case.expected_criteria["password"] == "[REDACTED]"
    assert case.metadata["token"] == "[REDACTED]"

    evaluator = ExactMatchEvaluator()
    res = evaluator.evaluate(
        case=case,
        actual_output={"status": "OK"},
        execution_id="exec-sec",
        evaluated_component="AuthService",
    )
    d = res.to_dict()
    assert d["expected_reference"]["password"] == "[REDACTED]"


def test_aa_no_golden_dataset_and_ab_no_quality_gate_boundaries():
    # Verificar que el módulo de evaluation NO importa ni define QualityGates ni GoldenDatasets
    import src.domain.evaluation as eval_domain
    import src.application.evaluation as eval_app

    for attr in dir(eval_domain):
        assert "GoldenDataset" not in attr
        assert "QualityGate" not in attr
        assert "BlockRelease" not in attr

    for attr in dir(eval_app):
        assert "GoldenDataset" not in attr
        assert "QualityGate" not in attr
        assert "BlockRelease" not in attr
