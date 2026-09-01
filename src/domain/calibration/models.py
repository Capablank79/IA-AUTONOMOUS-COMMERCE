from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Mapping, Any, Tuple
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.prediction.models import ComparisonStatus


class CalibrationStatus(str, Enum):
    """
    Estado del resultado de calibración de una decisión o conjunto de predicciones.
    """
    WELL_CALIBRATED = "WELL_CALIBRATED"
    OVER_CONFIDENT = "OVER_CONFIDENT"
    UNDER_CONFIDENT = "UNDER_CONFIDENT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_CALIBRATED = "NOT_CALIBRATED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ConfidenceBin:
    """
    Estadística de bin/bucket de confianza para análisis de calibración.
    """
    confidence_level: Confidence
    sample_count: int
    match_count: int
    miss_count: int
    unknown_count: int
    observed_success_rate: float
    expected_confidence_score: float
    calibration_gap: float

    def __post_init__(self):
        if self.sample_count < 0:
            raise ValueError("ConfidenceBin.sample_count cannot be negative")
        if self.match_count < 0:
            raise ValueError("ConfidenceBin.match_count cannot be negative")
        if self.miss_count < 0:
            raise ValueError("ConfidenceBin.miss_count cannot be negative")
        if self.unknown_count < 0:
            raise ValueError("ConfidenceBin.unknown_count cannot be negative")


@dataclass(frozen=True)
class DecisionCalibrationRecord:
    """
    Registro de dominio inmutable para la medición de Calibración de Decisiones (Task I.3).
    Representa métricas agregadas y verificables de calibración de predicciones comparadas
    con outcomes reales observados.
    
    Aislamiento y Reglas de Dominio:
    - Sin dependencias de DB, HTTP, JSON, SQL, SDKs ni APIs externas.
    - Preserva referencias por IDs (decision_id, mission_id, comparison_ids, prediction_ids, outcome_ids).
    - Preserva muestra explicita (total_samples, valid_samples, unknown_excluded_samples).
    - Maneja de manera segura la insuficiencia de datos (INSUFFICIENT_DATA).
    - Excluye UNKNOWN sin convertirlos en aciertos ni fallos.
    - Soporta Brier Score, Accuracy, Calibration Error, Bin Statistics.
    """
    calibration_id: str
    decision_id: Optional[str] = None
    mission_id: Optional[str] = None
    target_metric: str = "general"
    status: CalibrationStatus = CalibrationStatus.UNKNOWN
    
    # Muestras
    total_samples: int = 0
    valid_samples: int = 0
    unknown_excluded_samples: int = 0
    match_count: int = 0
    miss_count: int = 0
    
    # Métricas agregadas
    accuracy: float = 0.0
    error_rate: float = 0.0
    expected_confidence_score: float = 0.0
    brier_score: Optional[float] = None
    calibration_error: float = 0.0
    
    # Bins por nivel de confianza
    confidence_bins: Tuple[ConfidenceBin, ...] = field(default_factory=tuple)
    
    # Trazabilidad / Links
    comparison_ids: Tuple[str, ...] = field(default_factory=tuple)
    prediction_ids: Tuple[str, ...] = field(default_factory=tuple)
    outcome_ids: Tuple[str, ...] = field(default_factory=tuple)
    
    calculated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.calibration_id or not isinstance(self.calibration_id, str):
            raise ValueError("DecisionCalibrationRecord.calibration_id must be a non-empty string")
        if self.total_samples < 0 or self.valid_samples < 0 or self.unknown_excluded_samples < 0:
            raise ValueError("Sample counts cannot be negative")

        if not isinstance(self.confidence_bins, tuple):
            object.__setattr__(self, "confidence_bins", tuple(self.confidence_bins))
        if not isinstance(self.comparison_ids, tuple):
            object.__setattr__(self, "comparison_ids", tuple(self.comparison_ids))
        if not isinstance(self.prediction_ids, tuple):
            object.__setattr__(self, "prediction_ids", tuple(self.prediction_ids))
        if not isinstance(self.outcome_ids, tuple):
            object.__setattr__(self, "outcome_ids", tuple(self.outcome_ids))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
