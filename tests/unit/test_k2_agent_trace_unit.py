"""
Suite de Tests Unitarios para Agent Trace (Hito K.2).

Cubre exhaustivamente:
A. Immutable trace record
B. Execution grouping
C. Step numbering
D. Ordering
E. Component name
F. Operation
G. Input reference
H. Output reference
I. Status SUCCESS
J. Status FAILED
K. UNKNOWN
L. Timestamps
M. Duration
N. Correlation
O. Causation
P. Mission reference
Q. Cycle reference
R. Idempotency
S. Duplicate replay
T. Persistence
U. Restart
V. Query by execution
W. Query by mission
X. Query by component
Y. Sanitization
Z. Private prompt exclusion
AA. Chain-of-thought exclusion
AB. Audit Trail not duplicated
AC. No Cost Tracking
"""

import pytest
from datetime import datetime, timezone, timedelta
from types import MappingProxyType
from pathlib import Path
import tempfile
import shutil

from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
    ExecutionTraceTimeline,
)
from src.infrastructure.persistence.data.json.agent_trace_repository import (
    JsonAgentTraceRepository,
    CorruptedTraceRecordError,
)
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType


@pytest.fixture
def temp_trace_dir():
    dir_path = tempfile.mkdtemp()
    yield Path(dir_path)
    shutil.rmtree(dir_path, ignore_errors=True)


@pytest.fixture
def trace_repo(temp_trace_dir):
    return JsonAgentTraceRepository(temp_trace_dir)


@pytest.fixture
def trace_service(trace_repo):
    return AgentTraceService(trace_repo, isolate_failures=True)


# A. Immutable trace record
def test_a_immutable_trace_record():
    now = datetime.now(timezone.utc)
    rec = AgentTraceRecord(
        trace_id="trc-001",
        component_name="TestAgent",
        execution_id="exec-001",
        step_number=1,
        step_type=StepType.OBSERVE,
        operation="OBSERVE_MARKET",
        started_at=now,
        status=TraceStatus.SUCCESS,
        metadata={"key": "val"},
    )
    with pytest.raises(Exception):
        rec.status = TraceStatus.FAILED
    with pytest.raises(Exception):
        rec.metadata["new_key"] = "leak"
    assert isinstance(rec.metadata, MappingProxyType)


# B. Execution grouping
def test_b_execution_grouping(trace_repo):
    now = datetime.now(timezone.utc)
    r1 = AgentTraceRecord(
        trace_id="trc-001",
        component_name="AgentA",
        execution_id="exec-100",
        step_number=1,
        step_type=StepType.START,
        operation="START",
        started_at=now,
        status=TraceStatus.SUCCESS,
    )
    r2 = AgentTraceRecord(
        trace_id="trc-002",
        component_name="AgentA",
        execution_id="exec-100",
        step_number=2,
        step_type=StepType.TOOL_CALL,
        operation="TOOL",
        started_at=now + timedelta(seconds=1),
        status=TraceStatus.SUCCESS,
    )
    r3 = AgentTraceRecord(
        trace_id="trc-003",
        component_name="AgentB",
        execution_id="exec-200",
        step_number=1,
        step_type=StepType.START,
        operation="START",
        started_at=now,
        status=TraceStatus.SUCCESS,
    )
    trace_repo.append(r1)
    trace_repo.append(r2)
    trace_repo.append(r3)

    timeline_100 = trace_repo.get_execution_timeline("exec-100")
    assert timeline_100.total_steps == 2
    assert timeline_100.execution_id == "exec-100"
    assert [s.trace_id for s in timeline_100.steps] == ["trc-001", "trc-002"]


# C. Step numbering
def test_c_step_numbering():
    now = datetime.now(timezone.utc)
    rec = AgentTraceRecord(
        trace_id="trc-step-005",
        component_name="Agent",
        execution_id="exec-1",
        step_number=5,
        step_type=StepType.SERVICE_CALL,
        operation="CALL_POLICY",
        started_at=now,
    )
    assert rec.step_number == 5
    with pytest.raises(ValueError):
        AgentTraceRecord(
            trace_id="trc-neg",
            component_name="Agent",
            execution_id="exec-1",
            step_number=-1,
            step_type=StepType.SERVICE_CALL,
            operation="CALL_POLICY",
            started_at=now,
        )


# D. Ordering
def test_d_deterministic_ordering(trace_repo):
    now = datetime.now(timezone.utc)
    # Insert in reverse order
    r3 = AgentTraceRecord(
        trace_id="trc-3",
        component_name="Agent",
        execution_id="exec-order",
        step_number=3,
        step_type=StepType.COMPLETE,
        operation="FINISH",
        started_at=now + timedelta(seconds=2),
        status=TraceStatus.SUCCESS,
    )
    r1 = AgentTraceRecord(
        trace_id="trc-1",
        component_name="Agent",
        execution_id="exec-order",
        step_number=1,
        step_type=StepType.START,
        operation="INIT",
        started_at=now,
        status=TraceStatus.SUCCESS,
    )
    r2 = AgentTraceRecord(
        trace_id="trc-2",
        component_name="Agent",
        execution_id="exec-order",
        step_number=2,
        step_type=StepType.TOOL_CALL,
        operation="TOOL",
        started_at=now + timedelta(seconds=1),
        status=TraceStatus.SUCCESS,
    )
    trace_repo.append(r3)
    trace_repo.append(r1)
    trace_repo.append(r2)

    timeline = trace_repo.get_execution_timeline("exec-order")
    assert [s.step_number for s in timeline.steps] == [1, 2, 3]


# E. Component name & F. Operation
def test_e_f_component_and_operation():
    now = datetime.now(timezone.utc)
    rec = AgentTraceRecord(
        trace_id="trc-op",
        component_name="PolicyEngineExecutor",
        execution_id="exec-op",
        step_number=1,
        step_type=StepType.SERVICE_CALL,
        operation="EVALUATE_COMMERCIAL_SAFETY",
        started_at=now,
    )
    assert rec.component_name == "PolicyEngineExecutor"
    assert rec.operation == "EVALUATE_COMMERCIAL_SAFETY"


# G. Input reference & H. Output reference
def test_g_h_references():
    now = datetime.now(timezone.utc)
    rec = AgentTraceRecord(
        trace_id="trc-ref",
        component_name="ProductHunterAgent",
        execution_id="exec-ref",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="SEARCH_CATALOG",
        started_at=now,
        input_reference="query:auriculares,limit:10",
        output_reference="item_count:10,snapshot_id:snap-123",
    )
    assert rec.input_reference == "query:auriculares,limit:10"
    assert rec.output_reference == "item_count:10,snapshot_id:snap-123"


# I. Status SUCCESS & J. Status FAILED
def test_i_j_statuses(trace_service):
    rec_s = trace_service.record_step(
        component_name="Agent",
        execution_id="exec-stat",
        step_number=1,
        step_type=StepType.OBSERVE,
        operation="FETCH",
        status=TraceStatus.SUCCESS,
    )
    assert rec_s.status == TraceStatus.SUCCESS

    rec_f = trace_service.record_step(
        component_name="Agent",
        execution_id="exec-stat",
        step_number=2,
        step_type=StepType.FAILURE,
        operation="FETCH_FAIL",
        status=TraceStatus.FAILED,
    )
    assert rec_f.status == TraceStatus.FAILED


# K. UNKNOWN status preservation
def test_k_unknown_status_preservation(trace_service):
    rec_u = trace_service.record_step(
        component_name="Agent",
        execution_id="exec-unk",
        step_number=1,
        step_type=StepType.SERVICE_CALL,
        operation="VERIFY_SUPPLIER",
        status=TraceStatus.UNKNOWN,
        metadata={"reason": "Ambiguous provider response"},
    )
    assert rec_u.status == TraceStatus.UNKNOWN


# L. Timestamps & M. Duration
def test_l_m_timing_and_duration():
    start = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 1, 12, 0, 5, 500000, tzinfo=timezone.utc)
    rec = AgentTraceRecord(
        trace_id="trc-time",
        component_name="Agent",
        execution_id="exec-time",
        step_number=1,
        step_type=StepType.SERVICE_CALL,
        operation="WORK",
        started_at=start,
        completed_at=end,
        status=TraceStatus.SUCCESS,
    )
    assert rec.duration_seconds == 5.5


# N. Correlation & O. Causation & P. Mission reference & Q. Cycle reference
def test_n_o_p_q_causal_and_business_links(trace_service):
    rec = trace_service.record_step(
        component_name="ContinuousMissionService",
        execution_id="exec-links",
        step_number=1,
        step_type=StepType.SERVICE_CALL,
        operation="CYCLE_STEP",
        status=TraceStatus.SUCCESS,
        correlation_id="corr-999",
        causation_id="occ-888",
        mission_id="mis-777",
        cycle_id="cyc-666",
    )
    assert rec.correlation_id == "corr-999"
    assert rec.causation_id == "occ-888"
    assert rec.mission_id == "mis-777"
    assert rec.cycle_id == "cyc-666"


# R. Idempotency & S. Duplicate replay
def test_r_s_idempotency_and_duplicate_replay(trace_repo):
    now = datetime.now(timezone.utc)
    r1 = AgentTraceRecord(
        trace_id="trc-idem-1",
        component_name="Agent",
        execution_id="exec-idem",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="CALL_API",
        started_at=now,
        status=TraceStatus.SUCCESS,
    )
    res1 = trace_repo.append(r1)
    # Exact replay
    res2 = trace_repo.append(r1)
    assert res1.trace_id == res2.trace_id
    assert res1.checksum == res2.checksum
    records = trace_repo.list_records(execution_id="exec-idem")
    assert len(records) == 1


# T. Persistence & U. Restart
def test_t_u_persistence_and_restart(temp_trace_dir):
    repo1 = JsonAgentTraceRepository(temp_trace_dir)
    now = datetime.now(timezone.utc)
    r1 = AgentTraceRecord(
        trace_id="trc-perm-1",
        component_name="Agent",
        execution_id="exec-perm",
        step_number=1,
        step_type=StepType.START,
        operation="BOOT",
        started_at=now,
        status=TraceStatus.SUCCESS,
    )
    repo1.append(r1)

    # Destroy instance, recreate new repo pointing to same directory
    del repo1
    repo2 = JsonAgentTraceRepository(temp_trace_dir)
    loaded = repo2.get_by_id("trc-perm-1")
    assert loaded is not None
    assert loaded.operation == "BOOT"
    assert loaded.status == TraceStatus.SUCCESS

    # Append next step after restart
    r2 = AgentTraceRecord(
        trace_id="trc-perm-2",
        component_name="Agent",
        execution_id="exec-perm",
        step_number=2,
        step_type=StepType.COMPLETE,
        operation="DONE",
        started_at=now + timedelta(seconds=1),
        status=TraceStatus.SUCCESS,
    )
    repo2.append(r2)
    timeline = repo2.get_execution_timeline("exec-perm")
    assert timeline.total_steps == 2


# V. Query by execution & W. Query by mission & X. Query by component
def test_v_w_x_queries(trace_service):
    trace_service.record_step(
        component_name="CompA",
        execution_id="exec-1",
        step_number=1,
        step_type=StepType.START,
        operation="OP",
        status=TraceStatus.SUCCESS,
        mission_id="mis-1",
    )
    trace_service.record_step(
        component_name="CompB",
        execution_id="exec-2",
        step_number=1,
        step_type=StepType.START,
        operation="OP",
        status=TraceStatus.SUCCESS,
        mission_id="mis-2",
    )

    by_exec = trace_service.list_records(execution_id="exec-1")
    assert len(by_exec) == 1
    assert by_exec[0].execution_id == "exec-1"

    by_mis = trace_service.list_records(mission_id="mis-2")
    assert len(by_mis) == 1
    assert by_mis[0].mission_id == "mis-2"

    by_comp = trace_service.list_records(component_name="CompA")
    assert len(by_comp) == 1
    assert by_comp[0].component_name == "CompA"


# Y. Sanitization
def test_y_sanitization(trace_service):
    rec = trace_service.record_step(
        component_name="Agent",
        execution_id="exec-sec",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="AUTH_TOOL",
        status=TraceStatus.SUCCESS,
        metadata={
            "api_key": "secret-12345",
            "password": "my-password",
            "user_token": "token-xyz",
            "safe_field": "visible_data",
        }
    )
    assert rec.metadata["api_key"] == "[REDACTED]"
    assert rec.metadata["password"] == "[REDACTED]"
    assert rec.metadata["user_token"] == "[REDACTED]"
    assert rec.metadata["safe_field"] == "visible_data"


# Z. Private prompt exclusion & AA. Chain-of-thought exclusion
def test_z_aa_no_cot_no_prompts(trace_service):
    rec = trace_service.record_step(
        component_name="LLMAgent",
        execution_id="exec-cot",
        step_number=1,
        step_type=StepType.SERVICE_CALL,
        operation="DECIDE",
        status=TraceStatus.SUCCESS,
        metadata={
            "chain_of_thought": "I should first check if product is profitable...",
            "reasoning": "Thinking step by step...",
            "reasoning_tokens": 120,
            "internal_scratchpad": "Drafting plan...",
            "decision_name": "SELECT_PRODUCT",
        }
    )
    assert rec.metadata["chain_of_thought"] == "[REDACTED]"
    assert rec.metadata["reasoning"] == "[REDACTED]"
    assert rec.metadata["reasoning_tokens"] == "[REDACTED]"
    assert rec.metadata["internal_scratchpad"] == "[REDACTED]"
    assert rec.metadata["decision_name"] == "SELECT_PRODUCT"


# AB. Audit Trail not duplicated
def test_ab_audit_trail_not_duplicated():
    # Demonstrar que AgentTraceRecord y AuditRecord son entidades de dominio ortogonales y no duplicadas
    now = datetime.now(timezone.utc)
    audit = AuditRecord(
        audit_id="aud-100",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=now,
        actor=AuditActor(actor_type=AuditActorType.ACTION_EXECUTOR, actor_id="action-exec"),
        subject_type="ACTION",
        subject_id="act-100",
        action_or_operation="EXECUTE_ACTION",
        status="COMPLETED",
        correlation_id="corr-100",
    )
    trace = AgentTraceRecord(
        trace_id="trc-100",
        component_name="AutonomousLoop",
        execution_id="exec-100",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="EXECUTE_ACTION_DISCOVER",
        started_at=now,
        status=TraceStatus.SUCCESS,
        correlation_id="corr-100",
    )
    assert audit.audit_id != trace.trace_id
    assert audit.subject_type == "ACTION"
    assert trace.step_type == StepType.TOOL_CALL
    assert trace.correlation_id == audit.correlation_id


# AC. No Cost Tracking
def test_ac_no_cost_tracking(trace_service):
    rec = trace_service.record_step(
        component_name="Agent",
        execution_id="exec-cost",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="EXEC",
        status=TraceStatus.SUCCESS,
    )
    # Agent Trace doesn't compute token budgets or dollar cost (reserved to K.3)
    assert not hasattr(rec, "cost_usd")
    assert not hasattr(rec, "token_count")
    assert not hasattr(rec, "prompt_tokens")
