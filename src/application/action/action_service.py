from datetime import datetime, timezone
from typing import Optional, List, Mapping, Any

from src.domain.action.models import ActionRecord, ActionStatus
from src.domain.action.ports import ActionRepository
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class ActionMemoryService:
    """
    Servicio de Aplicación para gestionar el ciclo de vida y persistencia de Acciones Autónomas.
    Garantiza vincularidad estricta (Mission + Decision), idempotencia y sanitización.
    """

    def __init__(self, action_repo: ActionRepository):
        self.action_repo = action_repo

    def record_action(
        self,
        action_id: str,
        decision_id: str,
        mission_id: str,
        action_type: str,
        target_resource: Optional[str] = None,
        parameters: Optional[Mapping[str, Any]] = None,
        correlation_id: str = "default-correlation",
        idempotency_key: str = "default-idempotency",
        provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED,
        policy_reference: Optional[str] = None,
        approval_reference: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ActionRecord:
        # Check idempotency first
        if idempotency_key:
            existing = self.action_repo.get_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        now = datetime.now(timezone.utc)
        record = ActionRecord(
            action_id=action_id,
            decision_id=decision_id,
            mission_id=mission_id,
            action_type=action_type,
            status=ActionStatus.PENDING,
            target_resource=target_resource,
            parameters=parameters or {},
            created_at=now,
            updated_at=now,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            version=1,
            provenance=provenance,
            policy_reference=policy_reference,
            approval_reference=approval_reference,
            metadata=metadata or {},
        )
        self.action_repo.save(record)
        return record

    def update_action_status(
        self,
        action_id: str,
        status: ActionStatus,
    ) -> ActionRecord:
        existing = self.action_repo.get_by_id(action_id)
        if not existing:
            raise ValueError(f"ActionRecord with id '{action_id}' not found.")

        updated_record = ActionRecord(
            action_id=existing.action_id,
            decision_id=existing.decision_id,
            mission_id=existing.mission_id,
            action_type=existing.action_type,
            status=status,
            target_resource=existing.target_resource,
            parameters=existing.parameters,
            created_at=existing.created_at,
            updated_at=datetime.now(timezone.utc),
            correlation_id=existing.correlation_id,
            idempotency_key=existing.idempotency_key,
            version=existing.version + 1,
            provenance=existing.provenance,
            policy_reference=existing.policy_reference,
            approval_reference=existing.approval_reference,
            metadata=existing.metadata,
        )
        self.action_repo.save(updated_record)
        return updated_record

    def get_action(self, action_id: str) -> Optional[ActionRecord]:
        return self.action_repo.get_by_id(action_id)

    def get_actions_for_decision(self, decision_id: str) -> List[ActionRecord]:
        return self.action_repo.get_by_decision_id(decision_id)

    def get_actions_for_mission(self, mission_id: str) -> List[ActionRecord]:
        return self.action_repo.get_by_mission_id(mission_id)
