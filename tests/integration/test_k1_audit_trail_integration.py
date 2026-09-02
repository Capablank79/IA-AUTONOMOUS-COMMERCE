"""
Tests de integración y E2E para Hito K - Task K.1 (Audit Trail).

Verifica los escenarios solicitados en el Trae Master Execution Prompt:
- Escenario A — Complete Mission Audit: Ejecutar misión -> reconstruir timeline completo de extremo a extremo.
- Escenario B — Replay: Reprocesar mismo fact/event -> no duplicate audit record.
- Escenario C — Restart: Persist -> restart proceso/repo -> full timeline retained.
- Escenario D — UNKNOWN: Ambiguous result -> UNKNOWN visible in audit.
- Escenario E — Policy DENY: Decision -> Policy DENY -> no Action execution -> audit muestra claramente bloqueo.
- Escenario F — Security: Metadata con secretos -> persisted audit redacted.
- Escenario G — Continuous Mission: Cycle execution -> audit trace linked to continuous mission/cycle.

Garantiza:
1. Reconstrucción cronológica y causal completa.
2. Preservación de correlation_id, causation_id, actor y timestamp.
3. Semántica Append-Only e Idempotencia duradera.
4. Cero acoplamiento/invasión a K.2 Agent Trace o Cost Tracking.
"""

from datetime import datetime, timezone, timedelta
import os
import time
from pathlib import Path
import shutil
import tempfile
import uuid
import pytest

from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
    MissionAuditTimeline,
)
from src.domain.mission.models import Mission, MissionType, MissionPriority, MissionStatus
from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationSourceType,
    ObservationStatus,
    Marketplace,
)
from src.domain.decision.models import DecisionRecord, DecisionType, DecisionStatus
from src.domain.policy.models import (
    PolicyEvaluation,
    PolicyDecisionType,
    PolicyRuleCategory,
    RuleEvaluationResult,
)
from src.domain.action.models import ActionRecord, ActionStatus
from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.domain.market_intelligence.models import Confidence
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
)
from src.domain.change_detection.models import (
    ChangeRecord,
    ChangeType,
    ChangeSignificance,
    ChangeSubjectType,
)
from src.domain.alerts.models import AlertRecord, AlertSeverity, AlertStatus
from src.domain.continuous_mission.models import (
    ContinuousMission,
    ContinuousMissionStatus,
    ContinuousMissionCycle,
    ContinuousCycleStatus,
)
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.audit.audit_trail_service import AuditTrailService


@pytest.fixture
def temp_audit_dir():
    d = tempfile.mkdtemp(prefix="test_k1_integration_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def audit_repo(temp_audit_dir):
    return JsonAuditRepository(temp_audit_dir)


@pytest.fixture
def audit_service(audit_repo):
    return AuditTrailService(audit_repo)


# =========================================================================
# ESCENARIO A — Complete Mission Audit
# =========================================================================
def test_scenario_a_complete_mission_audit(audit_service):
    """
    Escenario A:
    Flujo completo:
    Mission Created -> Observation -> Evidence -> Decision -> Policy Evaluation (ALLOW) ->
    Action Created -> Action Executed -> Result Recorded -> Reconstruir timeline completo.
    """
    mission_id = f"mis-scenario-a-{uuid.uuid4().hex[:6]}"
    corr_id = f"corr-{mission_id}"

    # 1. Mission
    mission = Mission.create(MissionType.MARKET_DISCOVERY, parameters={"category": "electronics"}, priority=MissionPriority.HIGH)
    object.__setattr__(mission, "mission_id", mission_id)
    rec_mis = audit_service.record_mission_created(mission, correlation_id=corr_id)
    assert rec_mis.record_type == AuditRecordType.MISSION_CREATED
    assert rec_mis.mission_id == mission_id

    # 2. Market Observation
    now = datetime.now(timezone.utc)
    obs = MarketObservation(
        observation_id=f"obs-{uuid.uuid4().hex[:6]}",
        source="ML_CL",
        source_type=ObservationSourceType.MARKETPLACE_API,
        observed_at=now,
        collected_at=now,
        marketplace=Marketplace.MERCADO_LIBRE,
        entity_id="ITEM-100",
        status=ObservationStatus.SUCCESS,
        correlation_id=corr_id,
    )
    rec_obs = audit_service.record_market_observation(obs, mission_id=mission_id)
    assert rec_obs.record_type == AuditRecordType.MARKET_OBSERVATION_CREATED

    # 3. Evidence
    rec_evi = audit_service.record_evidence(
        evidence_id="evi-100",
        evidence_type="COMPETITOR_PRICE",
        source="ML_CL",
        subject_id="ITEM-100",
        mission_id=mission_id,
        correlation_id=corr_id,
        causation_id=obs.observation_id,
        metadata={"price": 19990, "currency": "CLP"},
    )
    assert rec_evi.record_type == AuditRecordType.EVIDENCE_RECORDED

    # 4. Decision
    dec = DecisionRecord(
        decision_id=f"dec-{uuid.uuid4().hex[:6]}",
        mission_id=mission_id,
        decision_type=DecisionType.PRICING_ADJUSTMENT,
        status=DecisionStatus.APPROVED,
        reason="Match competitor undercutting threshold",
        correlation_id=corr_id,
    )
    rec_dec = audit_service.record_decision(dec)
    assert rec_dec.record_type == AuditRecordType.DECISION_CREATED

    # 5. Policy (ALLOW)
    pol = PolicyEvaluation(
        evaluation_id=f"pol-{uuid.uuid4().hex[:6]}",
        decision=PolicyDecisionType.ALLOW,
        action_type="UPDATE_PRICE",
        actor_id="policy-guard-v1",
        mission_id=mission_id,
        correlation_id=corr_id,
        rules_evaluated=("MIN_PRICE_FLOOR", "MAX_DAILY_CHANGE"),
        rule_results=(
            RuleEvaluationResult("MIN_PRICE_FLOOR", PolicyRuleCategory.SAFETY, True, PolicyDecisionType.ALLOW),
            RuleEvaluationResult("MAX_DAILY_CHANGE", PolicyRuleCategory.SAFETY, True, PolicyDecisionType.ALLOW),
        ),
        reasons=("Within price bounds",),
        violations=(),
        is_allowed=True,
        requires_approval=False,
        is_unknown=False,
        is_denied=False,
        is_deferred=False,
    )
    rec_pol = audit_service.record_policy_evaluation(pol)
    assert rec_pol.record_type == AuditRecordType.POLICY_EVALUATED
    assert rec_pol.status == "ALLOW"

    # 6. Action Created & Executed
    act = ActionRecord(
        action_id=f"act-{uuid.uuid4().hex[:6]}",
        decision_id=dec.decision_id,
        mission_id=mission_id,
        action_type="UPDATE_PRICE",
        status=ActionStatus.COMPLETED,
        correlation_id=corr_id,
    )
    rec_act_cre = audit_service.record_action_created(act)
    rec_act_exe = audit_service.record_action_executed(act)
    assert rec_act_cre.record_type == AuditRecordType.ACTION_CREATED
    assert rec_act_exe.record_type == AuditRecordType.ACTION_EXECUTED

    # 7. Result
    res = ActionResultRecord(
        result_id=f"res-{uuid.uuid4().hex[:6]}",
        action_id=act.action_id,
        decision_id=dec.decision_id,
        mission_id=mission_id,
        outcome=ResultOutcome.SUCCESS,
        correlation_id=corr_id,
    )
    rec_res = audit_service.record_action_result(res)
    assert rec_res.record_type == AuditRecordType.RESULT_RECORDED
    assert rec_res.status == "SUCCESS"

    # 8. Reconstruir timeline
    timeline = audit_service.reconstruct_mission_audit(mission_id)
    assert timeline.mission_id == mission_id
    assert timeline.correlation_id == corr_id
    assert not timeline.is_empty
    assert len(timeline.records) == 8

    # Verificar cadena causal
    types = timeline.record_types_present
    assert AuditRecordType.MISSION_CREATED in types
    assert AuditRecordType.MARKET_OBSERVATION_CREATED in types
    assert AuditRecordType.EVIDENCE_RECORDED in types
    assert AuditRecordType.DECISION_CREATED in types
    assert AuditRecordType.POLICY_EVALUATED in types
    assert AuditRecordType.ACTION_CREATED in types
    assert AuditRecordType.ACTION_EXECUTED in types
    assert AuditRecordType.RESULT_RECORDED in types

    # Verificar orden cronológico
    records = timeline.records
    for i in range(len(records) - 1):
        assert records[i].occurred_at <= records[i + 1].occurred_at


# =========================================================================
# ESCENARIO B — Replay Idempotency
# =========================================================================
def test_scenario_b_replay_idempotency(audit_service, audit_repo):
    """
    Escenario B:
    Reprocesar el mismo hecho/evento -> No se duplica el AuditRecord en el repositorio.
    """
    mission_id = "mis-scenario-b-replay"
    corr_id = "corr-b"

    dec = DecisionRecord(
        decision_id="dec-b-1",
        mission_id=mission_id,
        decision_type=DecisionType.INVENTORY_REALLOCATION,
        status=DecisionStatus.APPROVED,
        reason="Stock deficit",
        correlation_id=corr_id,
    )

    rec1 = audit_service.record_decision(dec)
    rec2 = audit_service.record_decision(dec)

    assert rec1.audit_id == rec2.audit_id
    assert rec1.checksum == rec2.checksum

    all_records = audit_repo.list_records(mission_id=mission_id)
    assert len(all_records) == 1


# =========================================================================
# ESCENARIO C — Restart / Reload Durability
# =========================================================================
def test_scenario_c_restart_reload_durability(temp_audit_dir):
    """
    Escenario C:
    Persistir secuencia completa -> Destruir objeto / instanciar nuevo repo ->
    Recargar -> Historial íntegro preservado y timeline reconstruible.
    """
    # 1. Instancia inicial
    repo1 = JsonAuditRepository(temp_audit_dir)
    service1 = AuditTrailService(repo1)

    m_id = "mis-scenario-c-restart"
    corr = "corr-c"

    mis = Mission.create(MissionType.MARKET_DISCOVERY, parameters={}, priority=MissionPriority.MEDIUM)
    object.__setattr__(mis, "mission_id", m_id)
    service1.record_mission_created(mis, correlation_id=corr)

    time.sleep(0.01)
    service1.record_evidence("evi-c-1", "PRICE", "SRC_C", "ITEM-C", mission_id=m_id, correlation_id=corr)

    # 2. Destruir proceso / repo1 y crear repo2
    del service1
    del repo1

    repo2 = JsonAuditRepository(temp_audit_dir)
    service2 = AuditTrailService(repo2)

    timeline = service2.reconstruct_mission_audit(m_id)
    assert len(timeline.records) == 2
    assert timeline.records[0].record_type == AuditRecordType.MISSION_CREATED
    assert timeline.records[1].record_type == AuditRecordType.EVIDENCE_RECORDED

    # 3. Seguir agregando en repo2
    service2.record_evidence("evi-c-2", "STOCK", "SRC_C", "ITEM-C", mission_id=m_id, correlation_id=corr)
    timeline_after = service2.reconstruct_mission_audit(m_id)
    assert len(timeline_after.records) == 3


# =========================================================================
# ESCENARIO D — UNKNOWN Result Preservation
# =========================================================================
def test_scenario_d_unknown_preservation(audit_service):
    """
    Escenario D:
    Resultado ambiguo (UNKNOWN) de ejecución de acción -> El Audit Trail
    lo preserva como UNKNOWN sin transformarlo falsamente a SUCCESS ni FAILED.
    """
    mission_id = "mis-scenario-d-unk"
    corr = "corr-d"

    res_unk = ActionResultRecord(
        result_id="res-d-unk",
        action_id="act-d-1",
        decision_id="dec-d-1",
        mission_id=mission_id,
        outcome=ResultOutcome.UNKNOWN,
        correlation_id=corr,
        error_message="Gateway timeout during confirmation; state is indeterminate",
    )

    rec = audit_service.record_action_result(res_unk)
    assert rec.status == "UNKNOWN"
    assert rec.metadata["outcome"] == "UNKNOWN"
    assert "indeterminate" in rec.metadata["error_message"]

    timeline = audit_service.reconstruct_mission_audit(mission_id)
    assert len(timeline.records) == 1
    assert timeline.records[0].status == "UNKNOWN"


# =========================================================================
# ESCENARIO E — Policy DENY (Bloqueo en Audit Trail)
# =========================================================================
def test_scenario_e_policy_deny(audit_service):
    """
    Escenario E:
    Decision -> Policy DENY -> No hay Action execution ->
    El Audit Trail refleja el bloqueo de política claramente sin avanzar a ejecución.
    """
    mission_id = "mis-scenario-e-deny"
    corr = "corr-e"

    # 1. Decisión generada
    dec = DecisionRecord(
        decision_id="dec-e-1",
        mission_id=mission_id,
        decision_type=DecisionType.PRICING_ADJUSTMENT,
        status=DecisionStatus.APPROVED,
        reason="Aggressive discount proposed",
        correlation_id=corr,
    )
    audit_service.record_decision(dec)

    # 2. Evaluación de Policy DENY (violación de margen mínimo)
    pol_deny = PolicyEvaluation(
        evaluation_id="pol-e-deny",
        decision=PolicyDecisionType.DENY,
        action_type="UPDATE_PRICE",
        actor_id="policy-engine-strict",
        mission_id=mission_id,
        correlation_id=corr,
        rules_evaluated=("MIN_PROFIT_MARGIN_GUARD",),
        rule_results=(
            RuleEvaluationResult("MIN_PROFIT_MARGIN_GUARD", PolicyRuleCategory.SAFETY, False, PolicyDecisionType.DENY),
        ),
        reasons=("Price update violates 15% minimum margin rule",),
        violations=("MARGIN_UNDERFLOW_VIOLATION",),
        is_allowed=False,
        requires_approval=False,
        is_unknown=False,
        is_denied=True,
        is_deferred=False,
    )
    rec_pol = audit_service.record_policy_evaluation(pol_deny)

    assert rec_pol.status == "DENY"
    assert rec_pol.metadata["is_denied"] is True
    assert rec_pol.metadata["violations_count"] == 1

    # 3. Reconstruir timeline y verificar que no hay acciones ejecutadas
    timeline = audit_service.reconstruct_mission_audit(mission_id)
    assert len(timeline.records) == 2
    types = timeline.record_types_present
    assert AuditRecordType.DECISION_CREATED in types
    assert AuditRecordType.POLICY_EVALUATED in types
    assert AuditRecordType.ACTION_CREATED not in types
    assert AuditRecordType.ACTION_EXECUTED not in types
    assert AuditRecordType.RESULT_RECORDED not in types


# =========================================================================
# ESCENARIO F — Security & Credential Redaction
# =========================================================================
def test_scenario_f_security_redaction(temp_audit_dir, audit_service):
    """
    Escenario F:
    Metadatos con secretos (tokens OAuth, passwords, api keys, CVV) ->
    El Audit Trail persistido en disco y en memoria redacta recursivamente los valores sensibles.
    """
    mission_id = "mis-scenario-f-sec"
    corr = "corr-f"

    rec = audit_service.record_evidence(
        evidence_id="evi-sec-1",
        evidence_type="PAYMENT_AUTH",
        source="GATEWAY",
        subject_id="SUB-SEC",
        mission_id=mission_id,
        correlation_id=corr,
        metadata={
            "api_key": "live_sk_secret_99999",
            "password": "super_secret_pw",
            "oauth_token": "bearer eyJhbGciOi...",
            "nested": {
                "refresh_token": "rfr_12345",
                "cvv": "123",
                "pan": "4111222233334444",
                "safe_item_id": "ITEM-SAFE-777",
            },
        },
    )

    # Verificar en memoria
    meta = rec.metadata
    assert meta["api_key"] == "[REDACTED]"
    assert meta["password"] == "[REDACTED]"
    assert meta["oauth_token"] == "[REDACTED]"
    assert meta["nested"]["refresh_token"] == "[REDACTED]"
    assert meta["nested"]["cvv"] == "[REDACTED]"
    assert meta["nested"]["pan"] == "[REDACTED]"
    assert meta["nested"]["safe_item_id"] == "ITEM-SAFE-777"

    # Verificar directamente en el archivo JSON en disco
    record_file = temp_audit_dir / "audit_records" / f"{rec.audit_id}.json"
    assert record_file.exists()
    content = record_file.read_text(encoding="utf-8")
    assert "live_sk_secret_99999" not in content
    assert "super_secret_pw" not in content
    assert "rfr_12345" not in content
    assert "4111222233334444" not in content
    assert "[REDACTED]" in content


# =========================================================================
# ESCENARIO G — Continuous Autonomy Integration (Hito J)
# =========================================================================
def test_scenario_g_continuous_autonomy_integration(audit_service):
    """
    Escenario G:
    ContinuousMission -> Cycle Execution -> Mission -> Downstream Facts -> Audit Trail.
    El Audit Trail observa y preserva la trazabilidad completa hacia la misión continua
    y su ciclo de ejecución sin modificar ni controlar el flujo de Hito J.
    """
    cm_id = f"cm-{uuid.uuid4().hex[:6]}"
    cycle_id = f"cyc-{uuid.uuid4().hex[:6]}"
    mission_id = f"mis-from-cycle-{uuid.uuid4().hex[:6]}"
    corr_id = f"corr-{cm_id}-{cycle_id}"

    # 1. Continuous Mission
    cm = ContinuousMission(
        continuous_mission_id=cm_id,
        schedule_id="sch-sync-1",
        mission_type=MissionType.MARKET_DISCOVERY,
        goal="Nightly Competitor Sync",
    )
    rec_cm = audit_service.record_continuous_mission(cm, correlation_id=corr_id)
    assert rec_cm.record_type == AuditRecordType.CONTINUOUS_CYCLE
    assert rec_cm.subject_id == cm_id

    # 2. Cycle Execution
    now = datetime.now(timezone.utc)
    cycle = ContinuousMissionCycle(
        cycle_id=cycle_id,
        continuous_mission_id=cm_id,
        cycle_number=1,
        scheduled_at=now,
        started_at=now,
        completed_at=now + timedelta(minutes=2),
        status=ContinuousCycleStatus.SUCCESS,
        mission_id=mission_id,
        result_summary={"observations_count": 10, "decisions_count": 1},
    )
    rec_cyc = audit_service.record_continuous_cycle(cycle, correlation_id=corr_id)
    assert rec_cyc.subject_id == cycle_id

    # 3. Mission triggered by cycle
    mis = Mission.create(MissionType.MARKET_DISCOVERY, parameters={"cycle_id": cycle_id}, priority=MissionPriority.HIGH)
    object.__setattr__(mis, "mission_id", mission_id)
    audit_service.record_mission_created(mis, correlation_id=corr_id, causation_id=cycle_id)

    # 4. Opportunity & Decision
    opp = OpportunityRecord(
        opportunity_id=f"opp-{uuid.uuid4().hex[:6]}",
        canonical_product_id="ITEM-CYCLE-OPP",
        marketplace=Marketplace.MERCADO_LIBRE,
        detected_at=now,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-cyc-1",),
        observed_metrics=ObservedOpportunityMetrics(),
        derived_metrics=DerivedOpportunityMetrics(),
        correlation_id=corr_id,
    )
    audit_service.record_opportunity(opp, mission_id=mission_id)

    dec = DecisionRecord(
        decision_id=f"dec-{uuid.uuid4().hex[:6]}",
        mission_id=mission_id,
        decision_type=DecisionType.PRICING_ADJUSTMENT,
        status=DecisionStatus.APPROVED,
        reason="Automated cycle opportunity accepted",
        correlation_id=corr_id,
    )
    audit_service.record_decision(dec)

    # 5. Reconstruir timeline de la misión
    timeline = audit_service.reconstruct_mission_audit(mission_id)
    # Incluye CONTINUOUS_CYCLE (vinculado a mission_id), OPPORTUNITY, MISSION_CREATED y DECISION_CREATED
    assert len(timeline.records) == 4
    types = timeline.record_types_present
    assert AuditRecordType.CONTINUOUS_CYCLE in types
    assert AuditRecordType.MISSION_CREATED in types
    assert AuditRecordType.OPPORTUNITY_DETECTED in types
    assert AuditRecordType.DECISION_CREATED in types

    # 6. Consultar registros vinculados a la correlación del ciclo completo
    cycle_audit = audit_service.list_records(correlation_id=corr_id)
    assert len(cycle_audit) == 5
    cycle_subject_types = {r.subject_type for r in cycle_audit}
    assert "CONTINUOUS_MISSION" in cycle_subject_types
    assert "CONTINUOUS_CYCLE" in cycle_subject_types
    assert "MISSION" in cycle_subject_types
    assert "OPPORTUNITY" in cycle_subject_types
    assert "DECISION" in cycle_subject_types
