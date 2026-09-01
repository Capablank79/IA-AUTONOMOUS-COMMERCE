from datetime import datetime, timezone
from typing import Optional, List, Mapping, Any

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.outcome.ports import OutcomeRepository


class OutcomeTrackingService:
    """
    Servicio de Aplicación para la observación, captura y persistencia de Outcomes de Negocio (Task I.1).
    Garantiza idempotencia estricta, desacoplamiento y preservación de la trazabilidad causal:
    MISSION -> DECISION -> ACTION -> RESULT -> OUTCOME OBSERVED
    """

    def __init__(self, outcome_repo: OutcomeRepository):
        self.outcome_repo = outcome_repo

    def record_outcome(
        self,
        outcome_id: str,
        mission_id: str,
        decision_id: str,
        action_id: str,
        status: OutcomeStatus,
        result_id: Optional[str] = None,
        outcome_type: str = "BUSINESS_OBSERVATION",
        observed_at: Optional[datetime] = None,
        value_metrics: Optional[Mapping[str, Any]] = None,
        evidence_reference: Optional[str] = None,
        error_message: Optional[str] = None,
        confidence: Confidence = Confidence.MEDIUM,
        provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> OutcomeRecord:
        """
        Registra un outcome observado garantizando idempotencia.
        Si la idempotency_key ya fue persistida, retorna el registro existente sin duplicaciones.
        """
        if idempotency_key:
            existing = self.outcome_repo.get_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        now = observed_at or datetime.now(timezone.utc)
        record = OutcomeRecord(
            outcome_id=outcome_id,
            mission_id=mission_id,
            decision_id=decision_id,
            action_id=action_id,
            result_id=result_id,
            outcome_type=outcome_type,
            status=status,
            observed_at=now,
            value_metrics=value_metrics or {},
            evidence_reference=evidence_reference,
            error_message=error_message,
            confidence=confidence,
            provenance=provenance,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            metadata=metadata or {},
        )
        self.outcome_repo.save(record)
        return record

    def get_outcome(self, outcome_id: str) -> Optional[OutcomeRecord]:
        return self.outcome_repo.get_by_id(outcome_id)

    def get_outcomes_for_action(self, action_id: str) -> List[OutcomeRecord]:
        return self.outcome_repo.get_by_action_id(action_id)

    def get_outcomes_for_decision(self, decision_id: str) -> List[OutcomeRecord]:
        return self.outcome_repo.get_by_decision_id(decision_id)

    def get_outcomes_for_mission(self, mission_id: str) -> List[OutcomeRecord]:
        return self.outcome_repo.get_by_mission_id(mission_id)

    def get_outcomes_for_result(self, result_id: str) -> List[OutcomeRecord]:
        return self.outcome_repo.get_by_result_id(result_id)
