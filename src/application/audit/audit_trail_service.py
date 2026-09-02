"""
Servicio de Aplicación para Audit Trail (Hito K.1).

Orquesta el registro transversal e inmutable de hechos auditables:
- MISSION_CREATED / MISSION_STATE_CHANGED
- MARKET_OBSERVATION_CREATED
- EVIDENCE_RECORDED
- DECISION_CREATED
- POLICY_EVALUATED
- ACTION_CREATED / ACTION_EXECUTED
- RESULT_RECORDED
- OPPORTUNITY_DETECTED / CHANGE_DETECTED / ALERT_CREATED / CONTINUOUS_CYCLE

Permite:
- Reconstruir cronológica y causalmente misiones completas (reconstruct_mission_audit).
- Consultar hechos auditables por entidad, correlación y rango temporal.
- Garantizar idempotencia de replay y sanitización sin modificar los componentes que originan los hechos.
"""

from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Union
import uuid

from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
    MissionAuditTimeline,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.mission.models import Mission, MissionStatus
from src.domain.market_monitoring.models import MarketObservation
from src.domain.decision.models import DecisionRecord, DecisionEvidenceReference
from src.domain.action.models import ActionRecord
from src.domain.result.models import ActionResultRecord
from src.domain.policy.models import PolicyEvaluation
from src.domain.opportunity_detection.models import OpportunityRecord
from src.domain.change_detection.models import ChangeRecord
from src.domain.alerts.models import AlertRecord
from src.domain.continuous_mission.models import ContinuousMission, ContinuousMissionCycle


class AuditTrailService:
    """
    Servicio de aplicación para registrar y consultar hechos de auditoría en K.1.
    """

    def __init__(self, audit_repository: AuditRepositoryPort):
        self.audit_repository = audit_repository

    def record_mission_created(
        self,
        mission: Mission,
        actor: Optional[AuditActor] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> AuditRecord:
        """Registra la creación de una misión comercial."""
        act = actor or AuditActor(actor_type=AuditActorType.USER, actor_id="commercial-operator")
        corr = correlation_id or f"mission-{mission.mission_id}"
        
        # Determinar fecha con timezone aware
        occ_at = mission.created_at
        if occ_at.tzinfo is None:
            occ_at = occ_at.replace(tzinfo=timezone.utc)

        record = AuditRecord(
            audit_id=f"aud-mis-cre-{mission.mission_id}",
            record_type=AuditRecordType.MISSION_CREATED,
            occurred_at=occ_at,
            actor=act,
            subject_type="MISSION",
            subject_id=mission.mission_id,
            action_or_operation="CREATE_MISSION",
            status=mission.status.value,
            correlation_id=corr,
            causation_id=causation_id,
            mission_id=mission.mission_id,
            entity_reference=mission.mission_id,
            provenance="MISSION_SERVICE",
            metadata={
                "mission_type": mission.type.value if hasattr(mission.type, "value") else str(mission.type),
                "priority": mission.priority.value if hasattr(mission.priority, "value") else str(mission.priority),
            },
        )
        return self.audit_repository.append(record)

    def record_mission_state_changed(
        self,
        mission_id: str,
        previous_status: MissionStatus,
        new_status: MissionStatus,
        actor: Optional[AuditActor] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
    ) -> AuditRecord:
        """Registra una transición de estado en una misión."""
        act = actor or AuditActor(actor_type=AuditActorType.AGENT, actor_id="autonomous-operator")
        corr = correlation_id or f"mission-{mission_id}"
        occ = occurred_at or datetime.now(timezone.utc)
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        record = AuditRecord(
            audit_id=f"aud-mis-sta-{mission_id}-{new_status.value}",
            record_type=AuditRecordType.MISSION_STATE_CHANGED,
            occurred_at=occ,
            actor=act,
            subject_type="MISSION",
            subject_id=mission_id,
            action_or_operation=f"TRANSITION_TO_{new_status.value}",
            status=new_status.value,
            correlation_id=corr,
            causation_id=causation_id,
            mission_id=mission_id,
            entity_reference=mission_id,
            provenance="MISSION_SERVICE",
            metadata={
                "previous_status": previous_status.value,
                "new_status": new_status.value,
            },
        )
        return self.audit_repository.append(record)

    def record_market_observation(
        self,
        observation: MarketObservation,
        mission_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra la captura de una observación del mercado."""
        act = actor or AuditActor(
            actor_type=AuditActorType.MARKETPLACE,
            actor_id=observation.source,
            details={"source_type": observation.source_type.value},
        )
        record = AuditRecord(
            audit_id=f"aud-obs-{observation.observation_id}",
            record_type=AuditRecordType.MARKET_OBSERVATION_CREATED,
            occurred_at=observation.observed_at,
            actor=act,
            subject_type="MARKET_OBSERVATION",
            subject_id=observation.entity_id,
            action_or_operation="COLLECT_MARKET_OBSERVATION",
            status=observation.status.value,
            correlation_id=observation.correlation_id,
            causation_id=None,
            mission_id=mission_id,
            entity_reference=observation.observation_id,
            provenance=observation.provenance,
            metadata={
                "source": observation.source,
                "price": str(observation.price.amount) if observation.price else None,
                "currency": observation.price.currency if observation.price else None,
                "stock": observation.stock,
                "marketplace": observation.marketplace.value,
            },
        )
        return self.audit_repository.append(record)

    def record_evidence(
        self,
        evidence_id: str,
        evidence_type: str,
        source: str,
        subject_id: str,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
        occurred_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditRecord:
        """Registra la fijación de evidencia de negocio."""
        act = actor or AuditActor(actor_type=AuditActorType.AGENT, actor_id="evidence-collector")
        occ = occurred_at or datetime.now(timezone.utc)
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)
        corr = correlation_id or f"corr-{evidence_id}"

        record = AuditRecord(
            audit_id=f"aud-evi-{evidence_id}",
            record_type=AuditRecordType.EVIDENCE_RECORDED,
            occurred_at=occ,
            actor=act,
            subject_type="EVIDENCE",
            subject_id=subject_id,
            action_or_operation=f"RECORD_EVIDENCE_{evidence_type}",
            status="RECORDED",
            correlation_id=corr,
            causation_id=causation_id,
            mission_id=mission_id,
            entity_reference=evidence_id,
            evidence_reference=evidence_id,
            provenance="EVIDENCE_COLLECTOR",
            metadata=metadata or {"evidence_type": evidence_type, "source": source},
        )
        return self.audit_repository.append(record)

    def record_decision(
        self,
        decision: DecisionRecord,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra la toma de una decisión autónoma de negocio."""
        act = actor or AuditActor(actor_type=AuditActorType.AGENT, actor_id="autonomous-decision-loop")
        occ = decision.created_at
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        ev_ref = decision.evidence_references[0].evidence_id if decision.evidence_references else None

        record = AuditRecord(
            audit_id=f"aud-dec-{decision.decision_id}",
            record_type=AuditRecordType.DECISION_CREATED,
            occurred_at=occ,
            actor=act,
            subject_type="DECISION",
            subject_id=decision.decision_id,
            action_or_operation=f"DECIDE_{decision.decision_type.value}",
            status=decision.status.value,
            correlation_id=decision.correlation_id,
            causation_id=ev_ref or decision.mission_id,
            mission_id=decision.mission_id,
            entity_reference=decision.decision_id,
            evidence_reference=ev_ref,
            provenance=decision.provenance.value if hasattr(decision.provenance, "value") else str(decision.provenance),
            metadata={
                "decision_type": decision.decision_type.value,
                "outcome": decision.outcome.value,
                "reason": decision.reason,
                "confidence": decision.confidence.value if hasattr(decision.confidence, "value") else str(decision.confidence),
            },
        )
        return self.audit_repository.append(record)

    def record_policy_evaluation(
        self,
        evaluation: PolicyEvaluation,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra la evaluación de políticas de gobernanza sobre una decisión/acción."""
        act = actor or AuditActor(actor_type=AuditActorType.POLICY_ENGINE, actor_id=evaluation.actor_id or "policy-engine")
        occ = evaluation.timestamp
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        record = AuditRecord(
            audit_id=f"aud-pol-{evaluation.evaluation_id}",
            record_type=AuditRecordType.POLICY_EVALUATED,
            occurred_at=occ,
            actor=act,
            subject_type="POLICY_EVALUATION",
            subject_id=evaluation.evaluation_id,
            action_or_operation=f"EVALUATE_POLICY_{evaluation.action_type}",
            status=evaluation.decision.value,
            correlation_id=evaluation.correlation_id,
            causation_id=evaluation.mission_id,
            mission_id=evaluation.mission_id,
            entity_reference=evaluation.evaluation_id,
            provenance="POLICY_ENGINE",
            metadata={
                "action_type": evaluation.action_type,
                "decision": evaluation.decision.value,
                "is_allowed": evaluation.is_allowed,
                "is_denied": evaluation.is_denied,
                "requires_approval": evaluation.requires_approval,
                "rules_evaluated_count": len(evaluation.rules_evaluated),
                "violations_count": len(evaluation.violations),
            },
        )
        return self.audit_repository.append(record)

    def record_action_created(
        self,
        action: ActionRecord,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra la creación / planificación de una acción operativa."""
        act = actor or AuditActor(actor_type=AuditActorType.AGENT, actor_id="action-planner")
        occ = action.created_at
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        record = AuditRecord(
            audit_id=f"aud-act-cre-{action.action_id}",
            record_type=AuditRecordType.ACTION_CREATED,
            occurred_at=occ,
            actor=act,
            subject_type="ACTION",
            subject_id=action.action_id,
            action_or_operation=f"CREATE_ACTION_{action.action_type}",
            status=action.status.value,
            correlation_id=action.correlation_id,
            causation_id=action.decision_id,
            mission_id=action.mission_id,
            entity_reference=action.action_id,
            provenance=action.provenance.value if hasattr(action.provenance, "value") else str(action.provenance),
            metadata={
                "action_type": action.action_type,
                "target_resource": action.target_resource,
            },
        )
        return self.audit_repository.append(record)

    def record_action_executed(
        self,
        action: ActionRecord,
        actor: Optional[AuditActor] = None,
        occurred_at: Optional[datetime] = None,
    ) -> AuditRecord:
        """Registra la ejecución efectiva de una acción por el ActionExecutor."""
        act = actor or AuditActor(actor_type=AuditActorType.ACTION_EXECUTOR, actor_id="action-executor")
        occ = occurred_at or action.updated_at
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        record = AuditRecord(
            audit_id=f"aud-act-exe-{action.action_id}-{action.status.value}",
            record_type=AuditRecordType.ACTION_EXECUTED,
            occurred_at=occ,
            actor=act,
            subject_type="ACTION",
            subject_id=action.action_id,
            action_or_operation=f"EXECUTE_ACTION_{action.action_type}",
            status=action.status.value,
            correlation_id=action.correlation_id,
            causation_id=action.decision_id,
            mission_id=action.mission_id,
            entity_reference=action.action_id,
            provenance=action.provenance.value if hasattr(action.provenance, "value") else str(action.provenance),
            metadata={
                "action_type": action.action_type,
                "target_resource": action.target_resource,
            },
        )
        return self.audit_repository.append(record)

    def record_action_result(
        self,
        result: ActionResultRecord,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra el resultado observado tras la ejecución de una acción."""
        act = actor or AuditActor(actor_type=AuditActorType.ACTION_EXECUTOR, actor_id="result-observer")
        occ = result.observed_at
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        record = AuditRecord(
            audit_id=f"aud-res-{result.result_id}",
            record_type=AuditRecordType.RESULT_RECORDED,
            occurred_at=occ,
            actor=act,
            subject_type="ACTION_RESULT",
            subject_id=result.result_id,
            action_or_operation="RECORD_ACTION_RESULT",
            status=result.outcome.value,
            correlation_id=result.correlation_id,
            causation_id=result.action_id,
            mission_id=result.mission_id,
            entity_reference=result.result_id,
            evidence_reference=result.evidence_reference,
            provenance=result.provenance.value if hasattr(result.provenance, "value") else str(result.provenance),
            metadata={
                "action_id": result.action_id,
                "decision_id": result.decision_id,
                "outcome": result.outcome.value,
                "error_message": result.error_message,
            },
        )
        return self.audit_repository.append(record)

    def record_opportunity_detected(
        self,
        opportunity: OpportunityRecord,
        mission_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra la detección de una oportunidad de mercado."""
        act = actor or AuditActor(actor_type=AuditActorType.AGENT, actor_id="opportunity-engine")
        
        causation = None
        if hasattr(opportunity, "source_observation_ids") and opportunity.source_observation_ids:
            causation = opportunity.source_observation_ids[0]
        elif hasattr(opportunity, "observation_ids") and opportunity.observation_ids:
            causation = opportunity.observation_ids[0]

        record = AuditRecord(
            audit_id=f"aud-opp-{opportunity.opportunity_id}",
            record_type=AuditRecordType.OPPORTUNITY_DETECTED,
            occurred_at=opportunity.detected_at,
            actor=act,
            subject_type="OPPORTUNITY",
            subject_id=opportunity.canonical_product_id,
            action_or_operation="DETECT_OPPORTUNITY",
            status=opportunity.status.value,
            correlation_id=opportunity.correlation_id,
            causation_id=causation,
            mission_id=mission_id,
            entity_reference=opportunity.opportunity_id,
            provenance=opportunity.provenance,
            metadata={
                "opportunity_type": opportunity.opportunity_type.value,
                "confidence": opportunity.confidence.value,
            },
        )
        return self.audit_repository.append(record)

    def record_change_detected(
        self,
        change: ChangeRecord,
        mission_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra un cambio de mercado detectado entre observaciones."""
        act = actor or AuditActor(actor_type=AuditActorType.AGENT, actor_id="change-detection-engine")
        record = AuditRecord(
            audit_id=f"aud-chg-{change.change_id}",
            record_type=AuditRecordType.CHANGE_DETECTED,
            occurred_at=change.detected_at,
            actor=act,
            subject_type=change.subject_type.value,
            subject_id=change.subject_id,
            action_or_operation=f"DETECT_CHANGE_{change.change_type.value}",
            status=change.significance.value,
            correlation_id=change.correlation_id,
            causation_id=change.current_reference,
            mission_id=mission_id,
            entity_reference=change.change_id,
            provenance="CHANGE_DETECTION",
            metadata={
                "change_type": change.change_type.value,
                "significance": change.significance.value,
                "changed_fields": list(change.changed_fields),
            },
        )
        return self.audit_repository.append(record)

    def record_alert_created(
        self,
        alert: AlertRecord,
        mission_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra la emisión de una alerta autónoma."""
        act = actor or AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="alert-rules-engine")
        record = AuditRecord(
            audit_id=f"aud-alt-{alert.alert_id}",
            record_type=AuditRecordType.ALERT_CREATED,
            occurred_at=alert.created_at,
            actor=act,
            subject_type="ALERT",
            subject_id=alert.alert_id,
            action_or_operation=f"RAISE_ALERT_{alert.alert_type.value}",
            status=alert.status.value,
            correlation_id=alert.correlation_id,
            causation_id=alert.source_reference,
            mission_id=mission_id,
            entity_reference=alert.alert_id,
            provenance=alert.provenance,
            metadata={
                "alert_type": alert.alert_type.value,
                "severity": alert.severity.value,
                "title": alert.title,
            },
        )
        return self.audit_repository.append(record)

    def record_continuous_mission(
        self,
        mission: ContinuousMission,
        correlation_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra la creación / activación de una misión continua (J.7)."""
        act = actor or AuditActor(actor_type=AuditActorType.SCHEDULER, actor_id="continuous-coordinator")
        occ = mission.created_at
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        corr = correlation_id or f"corr-{mission.continuous_mission_id}"
        record = AuditRecord(
            audit_id=f"aud-cm-{mission.continuous_mission_id}",
            record_type=AuditRecordType.CONTINUOUS_CYCLE,
            occurred_at=occ,
            actor=act,
            subject_type="CONTINUOUS_MISSION",
            subject_id=mission.continuous_mission_id,
            action_or_operation="CREATE_CONTINUOUS_MISSION",
            status=mission.status.value,
            correlation_id=corr,
            causation_id=mission.schedule_id,
            mission_id=None,
            entity_reference=mission.continuous_mission_id,
            provenance="CONTINUOUS_AUTONOMY",
            metadata={
                "schedule_id": mission.schedule_id,
                "mission_type": mission.mission_type.value,
                "goal": mission.goal,
            },
        )
        return self.audit_repository.append(record)

    def record_continuous_cycle(
        self,
        cycle: ContinuousMissionCycle,
        continuous_mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Registra la ejecución de un ciclo continuo."""
        act = actor or AuditActor(actor_type=AuditActorType.SCHEDULER, actor_id="continuous-executor")
        occ = cycle.started_at
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        c_m_id = continuous_mission_id or cycle.continuous_mission_id
        corr = correlation_id or cycle.correlation_id or f"corr-{c_m_id}-{cycle.cycle_id}"

        record = AuditRecord(
            audit_id=f"aud-cyc-{cycle.cycle_id}",
            record_type=AuditRecordType.CONTINUOUS_CYCLE,
            occurred_at=occ,
            actor=act,
            subject_type="CONTINUOUS_CYCLE",
            subject_id=cycle.cycle_id,
            action_or_operation=f"EXECUTE_CYCLE_{cycle.cycle_number}",
            status=cycle.status.value,
            correlation_id=corr,
            causation_id=c_m_id,
            mission_id=cycle.mission_id,
            entity_reference=cycle.cycle_id,
            provenance=cycle.provenance or "CONTINUOUS_AUTONOMY",
            metadata={
                "continuous_mission_id": c_m_id,
                "cycle_number": cycle.cycle_number,
                "mission_id": cycle.mission_id,
                "status": cycle.status.value,
            },
        )
        return self.audit_repository.append(record)

    def record_opportunity(
        self,
        opportunity: OpportunityRecord,
        mission_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
    ) -> AuditRecord:
        """Alias para registrar oportunidad de mercado."""
        return self.record_opportunity_detected(opportunity, mission_id=mission_id, actor=actor)

    def reconstruct_mission_audit(self, mission_id: str) -> MissionAuditTimeline:
        """
        API Principal de Reconstrucción de Auditoría:
        Reconstruye cronológica y causalmente todos los hechos auditables de una misión.
        """
        return self.audit_repository.reconstruct_mission_timeline(mission_id)

    def get_by_id(self, audit_id: str) -> Optional[AuditRecord]:
        return self.audit_repository.get_by_id(audit_id)

    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        return self.audit_repository.list_records(
            mission_id=mission_id,
            correlation_id=correlation_id,
            subject_type=subject_type,
            subject_id=subject_id,
            record_type=record_type,
            from_time=from_time,
            to_time=to_time,
            limit=limit,
        )
