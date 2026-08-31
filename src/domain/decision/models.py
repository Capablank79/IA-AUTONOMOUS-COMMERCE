from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Tuple, Mapping, Any, Dict
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel
from src.domain.policy.models import PolicyDecisionType, PolicyEvaluation


class DecisionType(str, Enum):
    """
    Categoría/tipo de decisión autónoma realizada dentro del sistema.
    """
    MARKET_OPPORTUNITY = "MARKET_OPPORTUNITY"
    SUPPLIER_SELECTION = "SUPPLIER_SELECTION"
    PROFIT_FEASIBILITY = "PROFIT_FEASIBILITY"
    CAPITAL_ALLOCATION = "CAPITAL_ALLOCATION"
    OPERATING_MODEL = "OPERATING_MODEL"
    PUBLICATION_STRATEGY = "PUBLICATION_STRATEGY"
    PRICING_ADJUSTMENT = "PRICING_ADJUSTMENT"
    INVENTORY_REALLOCATION = "INVENTORY_REALLOCATION"
    GENERIC_LOOP = "GENERIC_LOOP"


class DecisionStatus(str, Enum):
    """
    Estado del ciclo de vida de la decisión conservada.
    """
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    UNKNOWN = "UNKNOWN"


class DecisionOutcome(str, Enum):
    """
    Resultado conceptual cualitativo de la decisión.
    """
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"
    PENDING_EXECUTION = "PENDING_EXECUTION"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DecisionEvidenceReference:
    """
    Referencia inmutable a una evidencia que respalda la decisión.
    """
    evidence_id: str
    evidence_type: str
    source: str
    confidence: Confidence
    provenance: EvidenceProvenanceType
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class DecisionRecord:
    """
    Registro de dominio principal inmutable de una Decisión Autónoma de Negocio.

    Aislamiento y Reglas de Dominio:
    - Sin dependencias de HTTP, JSON, SQL, filesystem, SDKs ni APIs externas.
    - Preserva vincularidad fuerte con Mission (`mission_id`).
    - Conserva trazabilidad (`correlation_id`, `idempotency_key`, `version`).
    - Almacena de forma desacoplada PolicyDecision / PolicyEvaluation si existe.
    - Garantiza no persistencia de PII sensible ni credenciales.
    """
    decision_id: str
    mission_id: str
    decision_type: DecisionType
    status: DecisionStatus
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    outcome: DecisionOutcome = DecisionOutcome.PENDING_EXECUTION
    target_resource: Optional[str] = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.MEDIUM
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    risk_level: Optional[RiskLevel] = None
    policy_evaluation: Optional[PolicyEvaluation] = None
    policy_decision_type: Optional[PolicyDecisionType] = None
    evidence_references: Tuple[DecisionEvidenceReference, ...] = field(default_factory=tuple)
    future_action_type: Optional[str] = None
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.parameters, MappingProxyType):
            object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        if not isinstance(self.evidence_references, tuple):
            object.__setattr__(self, "evidence_references", tuple(self.evidence_references))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def update_status(self, new_status: DecisionStatus, outcome: Optional[DecisionOutcome] = None) -> 'DecisionRecord':
        """
        Crea un nuevo DecisionRecord con estado actualizado e incrementando versión.
        """
        return DecisionRecord(
            decision_id=self.decision_id,
            mission_id=self.mission_id,
            decision_type=self.decision_type,
            status=new_status,
            reason=self.reason,
            created_at=self.created_at,
            updated_at=datetime.now(timezone.utc),
            outcome=outcome if outcome is not None else self.outcome,
            target_resource=self.target_resource,
            parameters=self.parameters,
            confidence=self.confidence,
            provenance=self.provenance,
            risk_level=self.risk_level,
            policy_evaluation=self.policy_evaluation,
            policy_decision_type=self.policy_decision_type,
            evidence_references=self.evidence_references,
            future_action_type=self.future_action_type,
            correlation_id=self.correlation_id,
            idempotency_key=self.idempotency_key,
            version=self.version + 1,
            metadata=self.metadata,
        )
