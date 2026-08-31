from datetime import datetime, timezone
from typing import Optional, List, Mapping, Any

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.domain.result.ports import ResultRepository


class ResultMemoryService:
    """
    Servicio de Aplicación para persistir y consultar Resultados de Acciones Observados.
    """

    def __init__(self, result_repo: ResultRepository):
        self.result_repo = result_repo

    def record_result(
        self,
        result_id: str,
        action_id: str,
        decision_id: str,
        mission_id: str,
        outcome: ResultOutcome,
        response_summary: Optional[Mapping[str, Any]] = None,
        evidence_reference: Optional[str] = None,
        error_message: Optional[str] = None,
        confidence: Confidence = Confidence.MEDIUM,
        provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ActionResultRecord:
        if idempotency_key:
            existing = self.result_repo.get_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        now = datetime.now(timezone.utc)
        record = ActionResultRecord(
            result_id=result_id,
            action_id=action_id,
            decision_id=decision_id,
            mission_id=mission_id,
            outcome=outcome,
            observed_at=now,
            response_summary=response_summary or {},
            evidence_reference=evidence_reference,
            error_message=error_message,
            confidence=confidence,
            provenance=provenance,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            metadata=metadata or {},
        )
        self.result_repo.save(record)
        return record

    def get_result(self, result_id: str) -> Optional[ActionResultRecord]:
        return self.result_repo.get_by_id(result_id)

    def get_result_for_action(self, action_id: str) -> Optional[ActionResultRecord]:
        return self.result_repo.get_by_action_id(action_id)

    def get_results_for_decision(self, decision_id: str) -> List[ActionResultRecord]:
        return self.result_repo.get_by_decision_id(decision_id)

    def get_results_for_mission(self, mission_id: str) -> List[ActionResultRecord]:
        return self.result_repo.get_by_mission_id(mission_id)
