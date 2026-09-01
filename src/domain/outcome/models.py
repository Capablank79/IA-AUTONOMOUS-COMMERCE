from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Mapping, Any
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class OutcomeStatus(str, Enum):
    """
    Estado/Tipo del Outcome observado del negocio.
    Representa el resultado final/posterior observado en el negocio reales o simulaciones controladas.
    """
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OutcomeRecord:
    """
    Registro de dominio inmutable para la observación y persistencia de Outcomes de Negocio (Task I.1).
    Conserva la relación causal estricta:
    MISSION -> DECISION -> ACTION -> RESULT -> OUTCOME OBSERVED
    """
    outcome_id: str
    mission_id: str
    decision_id: str
    action_id: str
    result_id: Optional[str] = None
    outcome_type: str = "BUSINESS_OBSERVATION"
    status: OutcomeStatus = OutcomeStatus.UNKNOWN
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    value_metrics: Mapping[str, Any] = field(default_factory=dict)
    evidence_reference: Optional[str] = None
    error_message: Optional[str] = None
    confidence: Confidence = Confidence.MEDIUM
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.outcome_id or not isinstance(self.outcome_id, str):
            raise ValueError("OutcomeRecord.outcome_id must be a non-empty string")
        if not self.mission_id or not isinstance(self.mission_id, str):
            raise ValueError("OutcomeRecord.mission_id must be a non-empty string")
        if not self.decision_id or not isinstance(self.decision_id, str):
            raise ValueError("OutcomeRecord.decision_id must be a non-empty string")
        if not self.action_id or not isinstance(self.action_id, str):
            raise ValueError("OutcomeRecord.action_id must be a non-empty string")

        if not isinstance(self.value_metrics, MappingProxyType):
            object.__setattr__(self, "value_metrics", MappingProxyType(dict(self.value_metrics)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
