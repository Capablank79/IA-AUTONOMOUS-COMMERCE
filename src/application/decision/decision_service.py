import uuid
from typing import Optional, List, Dict, Any

from src.domain.decision.models import (
    DecisionRecord,
    DecisionType,
    DecisionStatus,
    DecisionOutcome,
    DecisionEvidenceReference,
)
from src.domain.decision.ports import DecisionRepository
from src.domain.policy.models import PolicyEvaluation, PolicyDecisionType
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel


class DecisionMemoryError(Exception):
    """Exception base para errores en DecisionMemoryService."""
    pass


class DecisionNotFoundError(DecisionMemoryError):
    """Lanzada cuando se solicita una decisión que no existe."""
    pass


class DecisionMemoryService:
    """
    Servicio de aplicación para gestionar la memoria de decisiones autónomas del sistema.

    Responsabilidades:
    - Registrar y persistir decisiones generadas por motores del sistema o por el AutonomousLoop.
    - Garantizar la vinculación bi-direccional con la Misión (`mission_id`).
    - Permitir la recuperación, consulta por idempotencia y actualización del estado de las decisiones.
    - Preservar trazabilidad, evaluación de políticas (PolicyEvaluation), proveniencia y nivel de riesgo.
    - NUNCA ejecuta acciones ni efectos secundarios externos.
    """

    def __init__(self, repository: DecisionRepository):
        self.repository = repository

    def record_decision(
        self,
        mission_id: str,
        decision_type: DecisionType,
        reason: str,
        status: DecisionStatus = DecisionStatus.PROPOSED,
        outcome: DecisionOutcome = DecisionOutcome.PENDING_EXECUTION,
        target_resource: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        confidence: Confidence = Confidence.MEDIUM,
        provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED,
        risk_level: Optional[RiskLevel] = None,
        policy_evaluation: Optional[PolicyEvaluation] = None,
        evidence_references: Optional[List[DecisionEvidenceReference]] = None,
        future_action_type: Optional[str] = None,
        correlation_id: str = "default-correlation",
        idempotency_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DecisionRecord:
        """
        Registra y persiste una decisión. Maneja la verificación de idempotencia.
        """
        key = idempotency_key or f"idemp-{uuid.uuid4()}"

        # Replay / Idempotencia
        existing = self.repository.get_by_idempotency_key(key)
        if existing:
            return existing

        decision_id = f"dec-{uuid.uuid4()}"

        policy_decision_type = policy_evaluation.decision if policy_evaluation else None

        record = DecisionRecord(
            decision_id=decision_id,
            mission_id=mission_id,
            decision_type=decision_type,
            status=status,
            reason=reason,
            outcome=outcome,
            target_resource=target_resource,
            parameters=parameters or {},
            confidence=confidence,
            provenance=provenance,
            risk_level=risk_level,
            policy_evaluation=policy_evaluation,
            policy_decision_type=policy_decision_type,
            evidence_references=tuple(evidence_references) if evidence_references else (),
            future_action_type=future_action_type,
            correlation_id=correlation_id,
            idempotency_key=key,
            version=1,
            metadata=metadata or {},
        )

        self.repository.save(record)
        return record

    def get_decision(self, decision_id: str) -> DecisionRecord:
        """
        Recupera una decisión por su ID. Lanza DecisionNotFoundError si no se encuentra.
        """
        record = self.repository.get_by_id(decision_id)
        if not record:
            raise DecisionNotFoundError(f"Decision {decision_id} not found in memory.")
        return record

    def get_mission_decisions(self, mission_id: str) -> List[DecisionRecord]:
        """
        Recupera todas las decisiones asociadas a una misión.
        """
        return self.repository.get_by_mission_id(mission_id)

    def update_decision_status(
        self,
        decision_id: str,
        new_status: DecisionStatus,
        outcome: Optional[DecisionOutcome] = None,
    ) -> DecisionRecord:
        """
        Actualiza el estado de una decisión preservando la inmutabilidad y la historia.
        """
        record = self.get_decision(decision_id)
        updated = record.update_status(new_status, outcome=outcome)
        self.repository.save(updated)
        return updated
