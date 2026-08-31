from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Mapping, Any
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class ResultOutcome(str, Enum):
    """
    Resultado observado de la ejecución de una Acción.
    """
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ActionResultRecord:
    """
    Registro de dominio inmutable para la persistencia de Resultados de Acciones.
    Representa un resultado observado en la realidad o en integración, no vuelve a ejecutar la acción.
    Vinculado con Action, Decision y Mission.
    """
    result_id: str
    action_id: str
    decision_id: str
    mission_id: str
    outcome: ResultOutcome
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    response_summary: Mapping[str, Any] = field(default_factory=dict)
    evidence_reference: Optional[str] = None
    error_message: Optional[str] = None
    confidence: Confidence = Confidence.MEDIUM
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.result_id or not isinstance(self.result_id, str):
            raise ValueError("ActionResultRecord.result_id must be a non-empty string")
        if not self.action_id or not isinstance(self.action_id, str):
            raise ValueError("ActionResultRecord.action_id must be a non-empty string")
        if not self.decision_id or not isinstance(self.decision_id, str):
            raise ValueError("ActionResultRecord.decision_id must be a non-empty string")
        if not self.mission_id or not isinstance(self.mission_id, str):
            raise ValueError("ActionResultRecord.mission_id must be a non-empty string")

        if not isinstance(self.response_summary, MappingProxyType):
            object.__setattr__(self, "response_summary", MappingProxyType(dict(self.response_summary)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
