from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Mapping, Any, Tuple
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class LearningSignalType(str, Enum):
    """
    Categorías tipadas y deterministas de señales de aprendizaje (Task I.7).
    Representan evidencia histórica observada/derivada, no recomendaciones.
    """
    POSITIVE_OUTCOME = "POSITIVE_OUTCOME"
    NEGATIVE_OUTCOME = "NEGATIVE_OUTCOME"
    PARTIAL_OUTCOME = "PARTIAL_OUTCOME"
    PREDICTION_MATCH = "PREDICTION_MATCH"
    PREDICTION_MISS = "PREDICTION_MISS"
    OVER_CONFIDENCE = "OVER_CONFIDENCE"
    UNDER_CONFIDENCE = "UNDER_CONFIDENCE"
    PRODUCT_PERFORMANCE = "PRODUCT_PERFORMANCE"
    SUPPLIER_PERFORMANCE = "SUPPLIER_PERFORMANCE"
    STRATEGY_PERFORMANCE = "STRATEGY_PERFORMANCE"
    OPPORTUNITY = "OPPORTUNITY"
    RISK = "RISK"
    DATA_QUALITY = "DATA_QUALITY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class LearningSignalSubjectType(str, Enum):
    """
    Sujeto u objeto al que hace referencia la señal.
    """
    MISSION = "MISSION"
    DECISION = "DECISION"
    ACTION = "ACTION"
    PRODUCT = "PRODUCT"
    SUPPLIER = "SUPPLIER"
    STRATEGY = "STRATEGY"
    PREDICTION = "PREDICTION"
    SYSTEM = "SYSTEM"


class LearningSignalSourceType(str, Enum):
    """
    Origen/Fuente primaria de evidencia que generó la señal.
    """
    OUTCOME_TRACKING = "OUTCOME_TRACKING"
    PREDICTION_COMPARISON = "PREDICTION_COMPARISON"
    DECISION_CALIBRATION = "DECISION_CALIBRATION"
    PRODUCT_PERFORMANCE = "PRODUCT_PERFORMANCE"
    SUPPLIER_PERFORMANCE = "SUPPLIER_PERFORMANCE"
    STRATEGY_PERFORMANCE = "STRATEGY_PERFORMANCE"
    BUSINESS_MEMORY = "BUSINESS_MEMORY"


class SignalEvidenceClassification(str, Enum):
    """
    Clasificación estricta de la calidad de la evidencia según el Prompt I.7:
    - OBSERVED: Hecho directo verificado en el negocio (e.g. Outcome SUCCESS/FAILURE).
    - DERIVED: Métrica calculada determinísticamente sin inferencia (e.g. delta, rates).
    - INFERRED: Evaluación con suposiciones explícitas o modelos sintéticos.
    """
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"


class SignalStatus(str, Enum):
    """
    Estado del contrato de la señal.
    """
    VALID = "VALID"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DATA_QUALITY_WARNING = "DATA_QUALITY_WARNING"
    DEPRECATED = "DEPRECATED"


@dataclass(frozen=True)
class LearningSignalRecord:
    """
    Registro de dominio inmutable para representar una Señal de Aprendizaje Estructurada (Task I.7).

    Reglas de Dominio:
    - Dominio puro: Sin dependencias de DB, HTTP, JSON, SQL, SDKs ni APIs externas.
    - Preserva identidad canónica de la señal (`signal_id`).
    - Distingue explícitamente entre OBSERVED, DERIVED e INFERRED.
    - Mantiene referencias causales completas (`mission_id`, `decision_id`, `action_id`, `result_id`, `outcome_id`, etc.).
    - Preserva proveniencia, auditoría y excluye datos sensibles (PII/credenciales).
    - Inmutable (`frozen=True`).
    """
    signal_id: str
    signal_type: LearningSignalType
    subject_type: LearningSignalSubjectType
    subject_id: str
    source_type: LearningSignalSourceType
    source_id: str
    evidence_classification: SignalEvidenceClassification = SignalEvidenceClassification.DERIVED
    status: SignalStatus = SignalStatus.VALID
    
    # Causal References
    mission_id: Optional[str] = None
    decision_id: Optional[str] = None
    action_id: Optional[str] = None
    result_id: Optional[str] = None
    outcome_id: Optional[str] = None
    prediction_id: Optional[str] = None
    comparison_id: Optional[str] = None
    calibration_id: Optional[str] = None
    product_performance_id: Optional[str] = None
    supplier_performance_id: Optional[str] = None
    strategy_performance_id: Optional[str] = None
    
    # Payload / Value (sin PII/credenciales)
    signal_value: Mapping[str, Any] = field(default_factory=dict)
    summary: str = ""
    
    # Temporalidad
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Provenance y Auditoría
    evidence_reference: Optional[str] = None
    confidence: Confidence = Confidence.MEDIUM
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.signal_id or not isinstance(self.signal_id, str):
            raise ValueError("LearningSignalRecord.signal_id must be a non-empty string")
        if not self.subject_id or not isinstance(self.subject_id, str):
            raise ValueError("LearningSignalRecord.subject_id must be a non-empty string")
        if not self.source_id or not isinstance(self.source_id, str):
            raise ValueError("LearningSignalRecord.source_id must be a non-empty string")

        if not isinstance(self.signal_value, MappingProxyType):
            object.__setattr__(self, "signal_value", MappingProxyType(dict(self.signal_value)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
