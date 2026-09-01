from typing import Optional, List

from src.domain.outcome.models import OutcomeRecord
from src.domain.prediction.models import PredictionComparison
from src.domain.calibration.models import DecisionCalibrationRecord
from src.domain.product_performance.models import ProductPerformanceRecord
from src.domain.supplier_performance.models import SupplierPerformanceRecord
from src.domain.strategy_performance.models import StrategyPerformanceRecord
from src.domain.learning_signals.models import (
    LearningSignalRecord,
    LearningSignalType,
    LearningSignalSubjectType,
)
from src.domain.learning_signals.ports import LearningSignalRepositoryPort
from src.domain.learning_signals.services import LearningSignalGenerator


class LearningSignalService:
    """
    Servicio de aplicación para coordinar la generación, idempotencia y persistencia de señales de aprendizaje (Task I.7).

    Reglas:
    - Reutiliza el repositorio inyectado (`LearningSignalRepositoryPort`).
    - Aplica idempotencia estricta por `idempotency_key`: si la señal ya existe en la memoria durable, no la duplica ni altera.
    - Facilita métodos para extraer e ingerir fuentes I.1 a I.6.
    - Proporciona consultas filtradas para consumo futuro del Learning Loop (sin ejecutar aprendizaje/entrenamiento).
    """

    def __init__(self, repository: LearningSignalRepositoryPort):
        self.repository = repository

    def process_outcome(self, outcome: OutcomeRecord) -> Optional[LearningSignalRecord]:
        """Procesa un OutcomeRecord (I.1) y persiste la señal si corresponde."""
        signal = LearningSignalGenerator.generate_from_outcome(outcome)
        if not signal:
            return None
        existing = self.repository.get_signal_by_idempotency_key(signal.idempotency_key)
        if existing:
            return existing
        self.repository.save_signal(signal)
        return signal

    def process_prediction_comparison(self, comparison: PredictionComparison) -> Optional[LearningSignalRecord]:
        """Procesa una PredictionComparison (I.2) y persiste la señal si corresponde."""
        signal = LearningSignalGenerator.generate_from_prediction_comparison(comparison)
        if not signal:
            return None
        existing = self.repository.get_signal_by_idempotency_key(signal.idempotency_key)
        if existing:
            return existing
        self.repository.save_signal(signal)
        return signal

    def process_calibration(self, calibration: DecisionCalibrationRecord) -> Optional[LearningSignalRecord]:
        """Procesa un DecisionCalibrationRecord (I.3) y persiste la señal si corresponde."""
        signal = LearningSignalGenerator.generate_from_calibration(calibration)
        if not signal:
            return None
        existing = self.repository.get_signal_by_idempotency_key(signal.idempotency_key)
        if existing:
            return existing
        self.repository.save_signal(signal)
        return signal

    def process_product_performance(self, perf: ProductPerformanceRecord) -> Optional[LearningSignalRecord]:
        """Procesa un ProductPerformanceRecord (I.4) y persiste la señal si corresponde."""
        signal = LearningSignalGenerator.generate_from_product_performance(perf)
        if not signal:
            return None
        existing = self.repository.get_signal_by_idempotency_key(signal.idempotency_key)
        if existing:
            return existing
        self.repository.save_signal(signal)
        return signal

    def process_supplier_performance(self, perf: SupplierPerformanceRecord) -> Optional[LearningSignalRecord]:
        """Procesa un SupplierPerformanceRecord (I.5) y persiste la señal si corresponde."""
        signal = LearningSignalGenerator.generate_from_supplier_performance(perf)
        if not signal:
            return None
        existing = self.repository.get_signal_by_idempotency_key(signal.idempotency_key)
        if existing:
            return existing
        self.repository.save_signal(signal)
        return signal

    def process_strategy_performance(self, perf: StrategyPerformanceRecord) -> Optional[LearningSignalRecord]:
        """Procesa un StrategyPerformanceRecord (I.6) y persiste la señal si corresponde."""
        signal = LearningSignalGenerator.generate_from_strategy_performance(perf)
        if not signal:
            return None
        existing = self.repository.get_signal_by_idempotency_key(signal.idempotency_key)
        if existing:
            return existing
        self.repository.save_signal(signal)
        return signal

    def get_signal_by_id(self, signal_id: str) -> Optional[LearningSignalRecord]:
        return self.repository.get_signal_by_id(signal_id)

    def get_signals_by_subject(self, subject_type: LearningSignalSubjectType, subject_id: str) -> List[LearningSignalRecord]:
        return self.repository.get_signals_by_subject(subject_type, subject_id)

    def get_signals_by_type(self, signal_type: LearningSignalType) -> List[LearningSignalRecord]:
        return self.repository.get_signals_by_type(signal_type)

    def list_all_signals(self) -> List[LearningSignalRecord]:
        return self.repository.list_all()
