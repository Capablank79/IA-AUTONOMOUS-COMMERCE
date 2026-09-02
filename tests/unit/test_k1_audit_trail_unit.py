"""
Unit Tests exhaustivos para K.1 Audit Trail.

Cubre exhaustivamente los 29 aspectos (A a AC) requeridos:
A. immutable AuditRecord
B. mission audit
C. observation audit
D. evidence audit
E. decision audit
F. action audit
G. result audit
H. actor
I. timestamp
J. correlation
K. causation
L. provenance
M. chronological ordering
N. equal timestamp deterministic order
O. append-only
P. idempotency
Q. duplicate replay
R. persistence
S. restart/reload
T. query by mission
U. query by correlation
V. query by subject
W. full reconstruction
X. UNKNOWN preservation
Y. FAILED preservation
Z. security sanitization
AA. Business Memory not duplicated
AB. Event Store not duplicated
AC. no Agent Trace K.2
"""

import json

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
import tempfile
import shutil

from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
    MissionAuditTimeline,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.infrastructure.persistence.data.json.audit_repository import (
    JsonAuditRepository,
    CorruptedAuditRecordError,
)
from src.application.audit.audit_trail_service import AuditTrailService
from src.domain.mission.models import Mission, MissionType, MissionPriority, MissionStatus
from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationSourceType,
    ObservationStatus,
    NormalizedPrice,
)
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel
from src.domain.decision.models import (
    DecisionRecord,
    DecisionType,
    DecisionStatus,
    DecisionOutcome,
    DecisionEvidenceReference,
)
from src.domain.action.models import ActionRecord, ActionStatus
from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.domain.policy.models import (
    PolicyEvaluation,
    PolicyDecisionType,
    RuleEvaluationResult,
    PolicyRuleCategory,
)
from src.domain.opportunity_detection.models import OpportunityRecord, OpportunityType, OpportunityStatus
from src.domain.change_detection.models import ChangeRecord, ChangeType, ChangeSignificance, ChangeSubjectType
from src.domain.alerts.models import AlertRecord, AlertType, AlertSeverity, AlertStatus
from src.domain.continuous_mission.models import ContinuousMissionCycle, ContinuousCycleStatus


@pytest.fixture
def temp_audit_dir():
    temp_dir = tempfile.mkdtemp(prefix="test_audit_trail_")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def audit_repo(temp_audit_dir):
    return JsonAuditRepository(temp_audit_dir)


@pytest.fixture
def audit_service(audit_repo):
    return AuditTrailService(audit_repo)


# =========================================================================
# A. Immutable AuditRecord
# =========================================================================
def test_a_immutable_audit_record():
    actor = AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="sys-1")
    now = datetime.now(timezone.utc)
    record = AuditRecord(
        audit_id="aud-001",
        record_type=AuditRecordType.MISSION_CREATED,
        occurred_at=now,
        actor=actor,
        subject_type="MISSION",
        subject_id="mis-123",
        action_or_operation="CREATE",
        status="PENDING",
        correlation_id="corr-123",
        metadata={"k": "v"},
    )
    with pytest.raises(Exception):
        record.status = "COMPLETED"
    with pytest.raises(Exception):
        record.metadata["new_key"] = "forbidden"


# =========================================================================
# B. Mission Audit
# =========================================================================
def test_b_mission_audit(audit_service):
    mission = Mission.create(MissionType.MARKET_DISCOVERY, parameters={"category": "elec"})
    created_rec = audit_service.record_mission_created(mission)
    assert created_rec.record_type == AuditRecordType.MISSION_CREATED
    assert created_rec.subject_id == mission.mission_id
    assert created_rec.mission_id == mission.mission_id

    trans_rec = audit_service.record_mission_state_changed(
        mission_id=mission.mission_id,
        previous_status=MissionStatus.PENDING,
        new_status=MissionStatus.RUNNING,
    )
    assert trans_rec.record_type == AuditRecordType.MISSION_STATE_CHANGED
    assert trans_rec.status == "RUNNING"


# =========================================================================
# C. Observation Audit
# =========================================================================
def test_c_observation_audit(audit_service):
    now = datetime.now(timezone.utc)
    obs = MarketObservation(
        observation_id="obs-001",
        source="MERCADOLIBRE_SEARCH",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="MLC12345",
        status=ObservationStatus.SUCCESS,
        price=NormalizedPrice(amount=Decimal("15000"), currency="CLP"),
        stock=10,
        correlation_id="corr-obs-1",
    )
    rec = audit_service.record_market_observation(obs, mission_id="mis-100")
    assert rec.record_type == AuditRecordType.MARKET_OBSERVATION_CREATED
    assert rec.entity_reference == "obs-001"
    assert rec.actor.actor_type == AuditActorType.MARKETPLACE


# =========================================================================
# D. Evidence Audit
# =========================================================================
def test_d_evidence_audit(audit_service):
    rec = audit_service.record_evidence(
        evidence_id="evi-999",
        evidence_type="SUPPLIER_CATALOG",
        source="SUPPLIER_API",
        subject_id="SUP-101",
        mission_id="mis-100",
        correlation_id="corr-100",
    )
    assert rec.record_type == AuditRecordType.EVIDENCE_RECORDED
    assert rec.evidence_reference == "evi-999"


# =========================================================================
# E. Decision Audit
# =========================================================================
def test_e_decision_audit(audit_service):
    ev_ref = DecisionEvidenceReference(
        evidence_id="evi-10",
        evidence_type="MARKET_PRICE",
        source="ML_LIVE",
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )
    decision = DecisionRecord(
        decision_id="dec-200",
        mission_id="mis-100",
        decision_type=DecisionType.MARKET_OPPORTUNITY,
        status=DecisionStatus.APPROVED,
        reason="Strong profit margin detected",
        outcome=DecisionOutcome.SUCCESS,
        evidence_references=(ev_ref,),
        correlation_id="corr-100",
    )
    rec = audit_service.record_decision(decision)
    assert rec.record_type == AuditRecordType.DECISION_CREATED
    assert rec.causation_id == "evi-10"
    assert rec.actor.actor_type == AuditActorType.AGENT


# =========================================================================
# F. Action Audit
# =========================================================================
def test_f_action_audit(audit_service):
    act = ActionRecord(
        action_id="act-300",
        decision_id="dec-200",
        mission_id="mis-100",
        action_type="PUBLISH_ITEM",
        status=ActionStatus.PENDING,
        correlation_id="corr-100",
    )
    rec_created = audit_service.record_action_created(act)
    assert rec_created.record_type == AuditRecordType.ACTION_CREATED
    assert rec_created.causation_id == "dec-200"

    act_exec = ActionRecord(
        action_id="act-300",
        decision_id="dec-200",
        mission_id="mis-100",
        action_type="PUBLISH_ITEM",
        status=ActionStatus.COMPLETED,
        correlation_id="corr-100",
    )
    rec_exec = audit_service.record_action_executed(act_exec)
    assert rec_exec.record_type == AuditRecordType.ACTION_EXECUTED
    assert rec_exec.status == "COMPLETED"


# =========================================================================
# G. Result Audit
# =========================================================================
def test_g_result_audit(audit_service):
    res = ActionResultRecord(
        result_id="res-400",
        action_id="act-300",
        decision_id="dec-200",
        mission_id="mis-100",
        outcome=ResultOutcome.SUCCESS,
        correlation_id="corr-100",
    )
    rec = audit_service.record_action_result(res)
    assert rec.record_type == AuditRecordType.RESULT_RECORDED
    assert rec.causation_id == "act-300"
    assert rec.status == "SUCCESS"


# =========================================================================
# H. Actor Distinct Types
# =========================================================================
def test_h_actor(audit_service):
    actors = [
        AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="sys-engine"),
        AuditActor(actor_type=AuditActorType.AGENT, actor_id="hunter-agent"),
        AuditActor(actor_type=AuditActorType.USER, actor_id="admin-user"),
        AuditActor(actor_type=AuditActorType.POLICY_ENGINE, actor_id="governance-guard"),
        AuditActor(actor_type=AuditActorType.ACTION_EXECUTOR, actor_id="marketplace-executor"),
        AuditActor(actor_type=AuditActorType.SCHEDULER, actor_id="cron-scheduler"),
        AuditActor(actor_type=AuditActorType.EXTERNAL_TOOL, actor_id="calc-tool"),
        AuditActor(actor_type=AuditActorType.MARKETPLACE, actor_id="mercadolibre-cl"),
    ]
    now = datetime.now(timezone.utc)
    for idx, act in enumerate(actors):
        r = AuditRecord(
            audit_id=f"aud-act-{idx}",
            record_type=AuditRecordType.MISSION_CREATED,
            occurred_at=now,
            actor=act,
            subject_type="TEST",
            subject_id=f"sub-{idx}",
            action_or_operation="TEST_ACTOR",
            status="OK",
            correlation_id=f"corr-{idx}",
        )
        saved = audit_service.audit_repository.append(r)
        assert saved.actor.actor_type == act.actor_type
        assert saved.actor.actor_id == act.actor_id


# =========================================================================
# I. Timestamp Integrity
# =========================================================================
def test_i_timestamp_integrity(audit_repo):
    t_fixed = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    record = AuditRecord(
        audit_id="aud-time-1",
        record_type=AuditRecordType.MISSION_CREATED,
        occurred_at=t_fixed,
        actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="sys"),
        subject_type="MISSION",
        subject_id="mis-t1",
        action_or_operation="CREATE",
        status="PENDING",
        correlation_id="corr-t1",
    )
    audit_repo.append(record)
    fetched = audit_repo.get_by_id("aud-time-1")
    assert fetched.occurred_at == t_fixed


# =========================================================================
# J. Correlation Preservation
# =========================================================================
def test_j_correlation(audit_service):
    corr = "corr-unique-trace-999"
    mission = Mission.create(MissionType.MARKET_DISCOVERY, parameters={}, priority=MissionPriority.HIGH)
    audit_service.record_mission_created(mission, correlation_id=corr)
    recs = audit_service.list_records(correlation_id=corr)
    assert len(recs) == 1
    assert recs[0].correlation_id == corr


# =========================================================================
# K. Causation Trace
# =========================================================================
def test_k_causation(audit_service):
    ev_rec = audit_service.record_evidence(
        evidence_id="evi-c1",
        evidence_type="PRICE",
        source="SRC",
        subject_id="SUB",
        correlation_id="corr-c",
    )
    dec = DecisionRecord(
        decision_id="dec-c1",
        mission_id="mis-c1",
        decision_type=DecisionType.MARKET_OPPORTUNITY,
        status=DecisionStatus.APPROVED,
        reason="Passed",
        evidence_references=(DecisionEvidenceReference("evi-c1", "PRICE", "SRC", Confidence.HIGH, EvidenceProvenanceType.LIVE),),
        correlation_id="corr-c",
    )
    dec_rec = audit_service.record_decision(dec)
    assert dec_rec.causation_id == "evi-c1"


# =========================================================================
# L. Provenance Preservation
# =========================================================================
def test_l_provenance(audit_service):
    obs = MarketObservation(
        observation_id="obs-prov",
        source="TEST_SOURCE",
        source_type=ObservationSourceType.FIXTURE,
        observed_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        marketplace=Marketplace.GENERIC,
        entity_id="ENT-1",
        provenance="FIXTURE_STAGING",
        correlation_id="corr-prov",
    )
    rec = audit_service.record_market_observation(obs)
    assert rec.provenance == "FIXTURE_STAGING"


# =========================================================================
# M. Chronological Ordering
# =========================================================================
def test_m_chronological_ordering(audit_repo):
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    r1 = AuditRecord("aud-1", AuditRecordType.MISSION_CREATED, t0 + timedelta(seconds=10), AuditActor(AuditActorType.SYSTEM), "M", "1", "A", "OK", "c")
    r2 = AuditRecord("aud-2", AuditRecordType.MISSION_CREATED, t0 + timedelta(seconds=5), AuditActor(AuditActorType.SYSTEM), "M", "2", "A", "OK", "c")
    r3 = AuditRecord("aud-3", AuditRecordType.MISSION_CREATED, t0 + timedelta(seconds=20), AuditActor(AuditActorType.SYSTEM), "M", "3", "A", "OK", "c")

    audit_repo.append(r1)
    audit_repo.append(r2)
    audit_repo.append(r3)

    listed = audit_repo.list_records(correlation_id="c")
    assert [r.audit_id for r in listed] == ["aud-2", "aud-1", "aud-3"]


# =========================================================================
# N. Equal Timestamp Deterministic Order
# =========================================================================
def test_n_equal_timestamp_deterministic_order(audit_repo):
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
    r_b = AuditRecord("aud-b", AuditRecordType.MISSION_CREATED, t0, AuditActor(AuditActorType.SYSTEM), "M", "b", "A", "OK", "c-tie")
    r_a = AuditRecord("aud-a", AuditRecordType.MISSION_CREATED, t0, AuditActor(AuditActorType.SYSTEM), "M", "a", "A", "OK", "c-tie")

    audit_repo.append(r_b)
    audit_repo.append(r_a)

    listed = audit_repo.list_records(correlation_id="c-tie")
    assert [r.audit_id for r in listed] == ["aud-a", "aud-b"]


# =========================================================================
# O. Append-Only Semantics
# =========================================================================
def test_o_append_only(audit_service):
    # Un cambio de estado genera un nuevo registro, no muta el anterior
    m_id = "mis-append-test"
    r1 = audit_service.record_mission_state_changed(m_id, MissionStatus.PENDING, MissionStatus.RUNNING)
    r2 = audit_service.record_mission_state_changed(m_id, MissionStatus.RUNNING, MissionStatus.COMPLETED)

    timeline = audit_service.reconstruct_mission_audit(m_id)
    assert len(timeline.records) == 2
    assert timeline.records[0].status == "RUNNING"
    assert timeline.records[1].status == "COMPLETED"


# =========================================================================
# P. Idempotency Key Preservation
# =========================================================================
def test_p_idempotency(audit_repo):
    now = datetime.now(timezone.utc)
    r = AuditRecord(
        audit_id="aud-idemp-1",
        record_type=AuditRecordType.ACTION_CREATED,
        occurred_at=now,
        actor=AuditActor(AuditActorType.AGENT),
        subject_type="ACTION",
        subject_id="act-1",
        action_or_operation="CREATE",
        status="PENDING",
        correlation_id="corr-i",
        idempotency_key="idemp-key-custom-123",
    )
    audit_repo.append(r)
    found = audit_repo.get_by_idempotency_key("idemp-key-custom-123")
    assert found is not None
    assert found.audit_id == "aud-idemp-1"


# =========================================================================
# Q. Duplicate Replay
# =========================================================================
def test_q_duplicate_replay(audit_repo):
    now = datetime.now(timezone.utc)
    r1 = AuditRecord(
        audit_id="aud-dup-1",
        record_type=AuditRecordType.RESULT_RECORDED,
        occurred_at=now,
        actor=AuditActor(AuditActorType.ACTION_EXECUTOR),
        subject_type="ACTION_RESULT",
        subject_id="res-1",
        action_or_operation="RECORD",
        status="SUCCESS",
        correlation_id="corr-dup",
        idempotency_key="same-event-key",
    )
    r2 = AuditRecord(
        audit_id="aud-dup-2-different-id",
        record_type=AuditRecordType.RESULT_RECORDED,
        occurred_at=now,
        actor=AuditActor(AuditActorType.ACTION_EXECUTOR),
        subject_type="ACTION_RESULT",
        subject_id="res-1",
        action_or_operation="RECORD",
        status="SUCCESS",
        correlation_id="corr-dup",
        idempotency_key="same-event-key",
    )
    audit_repo.append(r1)
    res_second = audit_repo.append(r2)
    # Debe retornar el registro existente r1
    assert res_second.audit_id == "aud-dup-1"
    assert len(audit_repo.list_records(correlation_id="corr-dup")) == 1


# =========================================================================
# R. Persistence to Disk
# =========================================================================
def test_r_persistence(temp_audit_dir, audit_repo):
    now = datetime.now(timezone.utc)
    r = AuditRecord(
        audit_id="aud-disk-1",
        record_type=AuditRecordType.MISSION_CREATED,
        occurred_at=now,
        actor=AuditActor(AuditActorType.SYSTEM),
        subject_type="MISSION",
        subject_id="mis-disk",
        action_or_operation="CREATE",
        status="PENDING",
        correlation_id="corr-disk",
    )
    audit_repo.append(r)
    file_on_disk = temp_audit_dir / "audit_records" / "aud-disk-1.json"
    assert file_on_disk.exists()


# =========================================================================
# S. Restart / Reload Durability
# =========================================================================
def test_s_restart_reload(temp_audit_dir, audit_repo):
    now = datetime.now(timezone.utc)
    r = AuditRecord(
        audit_id="aud-restart-1",
        record_type=AuditRecordType.MISSION_CREATED,
        occurred_at=now,
        actor=AuditActor(AuditActorType.SYSTEM),
        subject_type="MISSION",
        subject_id="mis-restart",
        mission_id="mis-restart",
        action_or_operation="CREATE",
        status="PENDING",
        correlation_id="corr-restart",
    )
    audit_repo.append(r)

    # Simular reinicio creando una nueva instancia contra el mismo directorio
    new_repo = JsonAuditRepository(temp_audit_dir)
    loaded = new_repo.get_by_id("aud-restart-1")
    assert loaded is not None
    assert loaded.subject_id == "mis-restart"

    # Permite seguir añadiendo registros post-reinicio
    r2 = AuditRecord(
        audit_id="aud-restart-2",
        record_type=AuditRecordType.MISSION_STATE_CHANGED,
        occurred_at=now + timedelta(seconds=1),
        actor=AuditActor(AuditActorType.SYSTEM),
        subject_type="MISSION",
        subject_id="mis-restart",
        mission_id="mis-restart",
        action_or_operation="TRANSITION",
        status="COMPLETED",
        correlation_id="corr-restart",
    )
    new_repo.append(r2)
    assert len(new_repo.list_records(mission_id="mis-restart")) == 2


# =========================================================================
# T. Query by Mission
# =========================================================================
def test_t_query_by_mission(audit_service):
    m1 = "mis-m1"
    m2 = "mis-m2"
    audit_service.record_evidence("evi-1", "PRICE", "SRC", "SUB1", mission_id=m1)
    audit_service.record_evidence("evi-2", "PRICE", "SRC", "SUB2", mission_id=m2)

    recs = audit_service.list_records(mission_id=m1)
    assert len(recs) == 1
    assert recs[0].subject_id == "SUB1"


# =========================================================================
# U. Query by Correlation
# =========================================================================
def test_u_query_by_correlation(audit_service):
    audit_service.record_evidence("evi-u1", "P", "S", "SUB", correlation_id="c-alpha")
    audit_service.record_evidence("evi-u2", "P", "S", "SUB", correlation_id="c-beta")

    recs = audit_service.list_records(correlation_id="c-alpha")
    assert len(recs) == 1
    assert recs[0].entity_reference == "evi-u1"


# =========================================================================
# V. Query by Subject
# =========================================================================
def test_v_query_by_subject(audit_service):
    audit_service.record_evidence("evi-v1", "P", "S", subject_id="PROD-999")
    audit_service.record_evidence("evi-v2", "P", "S", subject_id="PROD-888")

    recs = audit_service.list_records(subject_type="EVIDENCE", subject_id="PROD-999")
    assert len(recs) == 1
    assert recs[0].subject_id == "PROD-999"


# =========================================================================
# W. Full Reconstruction
# =========================================================================
def test_w_full_reconstruction(audit_service):
    m_id = "mis-recon-full"
    corr = f"corr-{m_id}"

    # 1. Mission created
    mis = Mission.create(MissionType.MARKET_DISCOVERY, parameters={}, priority=MissionPriority.HIGH)
    object.__setattr__(mis, "mission_id", m_id)
    audit_service.record_mission_created(mis, correlation_id=corr)

    # 2. Observation
    now = datetime.now(timezone.utc)
    obs = MarketObservation(
        observation_id="obs-rf-1",
        source="ML_CL",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-1",
        status=ObservationStatus.SUCCESS,
        correlation_id=corr,
    )
    audit_service.record_market_observation(obs, mission_id=m_id)

    # 3. Evidence
    audit_service.record_evidence("evi-rf-1", "MARKET_DATA", "ML", "ITEM-1", mission_id=m_id, correlation_id=corr)

    # 4. Decision
    dec = DecisionRecord(
        decision_id="dec-rf-1",
        mission_id=m_id,
        decision_type=DecisionType.MARKET_OPPORTUNITY,
        status=DecisionStatus.APPROVED,
        reason="Profitable opportunity",
        correlation_id=corr,
    )
    audit_service.record_decision(dec)

    # 5. Policy
    pol = PolicyEvaluation(
        evaluation_id="pol-rf-1",
        decision=PolicyDecisionType.ALLOW,
        action_type="PUBLISH_ITEM",
        actor_id="policy-guard",
        mission_id=m_id,
        correlation_id=corr,
        rules_evaluated=("SAFETY",),
        rule_results=(RuleEvaluationResult("SAFETY", PolicyRuleCategory.SAFETY, True, PolicyDecisionType.ALLOW),),
        reasons=("Passed",),
        violations=(),
        is_allowed=True,
        requires_approval=False,
        is_unknown=False,
        is_denied=False,
        is_deferred=False,
    )
    audit_service.record_policy_evaluation(pol)

    # 6. Action
    act = ActionRecord(
        action_id="act-rf-1",
        decision_id="dec-rf-1",
        mission_id=m_id,
        action_type="PUBLISH_ITEM",
        status=ActionStatus.COMPLETED,
        correlation_id=corr,
    )
    audit_service.record_action_created(act)
    audit_service.record_action_executed(act)

    # 7. Result
    res = ActionResultRecord(
        result_id="res-rf-1",
        action_id="act-rf-1",
        decision_id="dec-rf-1",
        mission_id=m_id,
        outcome=ResultOutcome.SUCCESS,
        correlation_id=corr,
    )
    audit_service.record_action_result(res)

    # Reconstruct timeline
    timeline = audit_service.reconstruct_mission_audit(m_id)
    assert not timeline.is_empty
    # Son 8 eventos: MISSION_CREATED, MARKET_OBSERVATION, EVIDENCE, DECISION, POLICY_EVALUATED, ACTION_CREATED, ACTION_EXECUTED, RESULT_RECORDED
    assert len(timeline.records) == 8
    types = timeline.record_types_present
    assert AuditRecordType.MISSION_CREATED in types
    assert AuditRecordType.MARKET_OBSERVATION_CREATED in types
    assert AuditRecordType.EVIDENCE_RECORDED in types
    assert AuditRecordType.DECISION_CREATED in types
    assert AuditRecordType.POLICY_EVALUATED in types
    assert AuditRecordType.ACTION_CREATED in types
    assert AuditRecordType.ACTION_EXECUTED in types
    assert AuditRecordType.RESULT_RECORDED in types


# =========================================================================
# X. UNKNOWN Preservation
# =========================================================================
def test_x_unknown_preservation(audit_service):
    res = ActionResultRecord(
        result_id="res-unk-1",
        action_id="act-unk-1",
        decision_id="dec-unk-1",
        mission_id="mis-unk-1",
        outcome=ResultOutcome.UNKNOWN,
        correlation_id="corr-unk",
    )
    rec = audit_service.record_action_result(res)
    assert rec.status == "UNKNOWN"
    fetched = audit_service.get_by_id(rec.audit_id)
    assert fetched.status == "UNKNOWN"


# =========================================================================
# Y. FAILED Preservation
# =========================================================================
def test_y_failed_preservation(audit_service):
    res = ActionResultRecord(
        result_id="res-fail-1",
        action_id="act-fail-1",
        decision_id="dec-fail-1",
        mission_id="mis-fail-1",
        outcome=ResultOutcome.FAILURE,
        error_message="API Gateway Timeout 504",
        correlation_id="corr-fail",
    )
    rec = audit_service.record_action_result(res)
    assert rec.status == "FAILURE"
    assert rec.metadata["error_message"] == "API Gateway Timeout 504"


# =========================================================================
# Z. Security Sanitization (No sensitive data in audit files)
# =========================================================================
def test_z_security_sanitization(temp_audit_dir, audit_repo):
    now = datetime.now(timezone.utc)
    raw_cvv = "9876"
    rec = AuditRecord(
        audit_id="aud-sec-1",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=now,
        actor=AuditActor(AuditActorType.ACTION_EXECUTOR, details={"oauth_token": "secret_oauth_12345"}),
        subject_type="ACTION",
        subject_id="act-sec",
        action_or_operation="EXECUTE",
        status="COMPLETED",
        correlation_id="corr-sec",
        metadata={
            "api_key": "sk-live-secret-999",
            "password": "my-secret-pass",
            "user_data": {
                "pan": "4111111111111111",
                "cvv": raw_cvv,
                "safe_note": "item published",
            },
        },
    )
    audit_repo.append(rec)

    # Leer el archivo JSON crudo desde disco para comprobar que los secretos fueron redactados
    file_path = temp_audit_dir / "audit_records" / "aud-sec-1.json"
    content = file_path.read_text(encoding="utf-8")
    persisted = json.loads(content)
    assert "sk-live-secret-999" not in content
    assert "secret_oauth_12345" not in content
    assert "my-secret-pass" not in content
    assert "4111111111111111" not in content
    assert persisted["metadata"]["user_data"]["cvv"] == "[REDACTED]"
    assert raw_cvv not in content
    assert "[REDACTED]" in content
    assert "item published" in content


# =========================================================================
# AA. Business Memory Not Duplicated
# =========================================================================
def test_aa_business_memory_not_duplicated(audit_service):
    # Audit trail guarda metadata referencial estandarizada y no reemplaza los repositorios de entidades
    rec = audit_service.record_evidence(
        evidence_id="evi-ref-only",
        evidence_type="CATALOG",
        source="DIR",
        subject_id="SKU-1",
        mission_id="mis-aa",
    )
    assert rec.entity_reference == "evi-ref-only"
    assert rec.evidence_reference == "evi-ref-only"


# =========================================================================
# AB. Event Store Not Duplicated
# =========================================================================
def test_ab_event_store_not_duplicated(audit_service):
    # Audit trail no reemplaza Event Store ni actúa como broker de pub/sub
    obs = MarketObservation(
        observation_id="obs-ab-1",
        source="TEST",
        source_type=ObservationSourceType.FIXTURE,
        observed_at=datetime.now(timezone.utc),
        collected_at=datetime.now(timezone.utc),
        marketplace=Marketplace.GENERIC,
        entity_id="ENT-AB",
        correlation_id="corr-ab",
    )
    rec = audit_service.record_market_observation(obs)
    assert rec.record_type == AuditRecordType.MARKET_OBSERVATION_CREATED
    assert rec.subject_type == "MARKET_OBSERVATION"


# =========================================================================
# AC. No Agent Trace K.2 Contamination
# =========================================================================
def test_ac_no_agent_trace_k2(audit_service):
    # No contiene campos de chain-of-thought, reasoning privado, ni trace de loops internos
    dec = DecisionRecord(
        decision_id="dec-ac-1",
        mission_id="mis-ac-1",
        decision_type=DecisionType.MARKET_OPPORTUNITY,
        status=DecisionStatus.APPROVED,
        reason="Standard auditable business rationale",
        correlation_id="corr-ac",
    )
    rec = audit_service.record_decision(dec)
    assert "chain_of_thought" not in rec.metadata
    assert "private_reasoning" not in rec.metadata
    assert "internal_prompt" not in rec.metadata
