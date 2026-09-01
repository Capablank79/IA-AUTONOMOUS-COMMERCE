from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Mapping, Any
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class ComparisonStatus(str, Enum):
    """
    Estado de la comparación entre la predicción y el outcome real.
    """
    MATCH = "MATCH"
    MISS = "MISS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PredictionRecord:
    """
    Registro de dominio inmutable para la captura de Predicciones del sistema (Task I.2).
    Registra el estado previsto/esperado antes de la observación del outcome real.
    """
    prediction_id: str
    mission_id: str
    decision_id: str
    action_id: Optional[str] = None
    target_metric: str = "general"
    predicted_value: Optional[Any] = None
    predicted_class: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expected_at: Optional[datetime] = None
    confidence: Confidence = Confidence.MEDIUM
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    evidence_reference: Optional[str] = None
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.prediction_id or not isinstance(self.prediction_id, str):
            raise ValueError("PredictionRecord.prediction_id must be a non-empty string")
        if not self.mission_id or not isinstance(self.mission_id, str):
            raise ValueError("PredictionRecord.mission_id must be a non-empty string")
        if not self.decision_id or not isinstance(self.decision_id, str):
            raise ValueError("PredictionRecord.decision_id must be a non-empty string")

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class PredictionComparison:
    """
    Registro de dominio inmutable para la comparación entre una Predicción y su Outcome real.
    Garantiza trazabilidad causal, temporalidad y auditabilidad.
    """
    comparison_id: str
    prediction_id: str
    outcome_id: str
    mission_id: str
    decision_id: str
    action_id: Optional[str] = None
    target_metric: str = "general"
    expected_value: Optional[Any] = None
    actual_value: Optional[Any] = None
    delta: Optional[float] = None
    status: ComparisonStatus = ComparisonStatus.UNKNOWN
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    prediction_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    outcome_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    prediction_provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    outcome_provenance: EvidenceProvenanceType = EvidenceProvenanceType.LIVE
    prediction_confidence: Confidence = Confidence.MEDIUM
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.comparison_id or not isinstance(self.comparison_id, str):
            raise ValueError("PredictionComparison.comparison_id must be a non-empty string")
        if not self.prediction_id or not isinstance(self.prediction_id, str):
            raise ValueError("PredictionComparison.prediction_id must be a non-empty string")
        if not self.outcome_id or not isinstance(self.outcome_id, str):
            raise ValueError("PredictionComparison.outcome_id must be a non-empty string")
        if not self.mission_id or not isinstance(self.mission_id, str):
            raise ValueError("PredictionComparison.mission_id must be a non-empty string")
        if not self.decision_id or not isinstance(self.decision_id, str):
            raise ValueError("PredictionComparison.decision_id must be a non-empty string")

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
