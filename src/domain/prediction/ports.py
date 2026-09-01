from abc import ABC, abstractmethod
from typing import Optional, List
from src.domain.prediction.models import PredictionRecord, PredictionComparison


class PredictionRepository(ABC):
    """
    Puerto primario del repositorio para guardar y consultar PredictionRecord y PredictionComparison.
    """

    @abstractmethod
    def save_prediction(self, prediction: PredictionRecord) -> None:
        pass

    @abstractmethod
    def get_prediction_by_id(self, prediction_id: str) -> Optional[PredictionRecord]:
        pass

    @abstractmethod
    def get_predictions_by_decision_id(self, decision_id: str) -> List[PredictionRecord]:
        pass

    @abstractmethod
    def get_predictions_by_mission_id(self, mission_id: str) -> List[PredictionRecord]:
        pass

    @abstractmethod
    def get_prediction_by_idempotency_key(self, idempotency_key: str) -> Optional[PredictionRecord]:
        pass

    @abstractmethod
    def save_comparison(self, comparison: PredictionComparison) -> None:
        pass

    @abstractmethod
    def get_comparison_by_id(self, comparison_id: str) -> Optional[PredictionComparison]:
        pass

    @abstractmethod
    def get_comparisons_by_prediction_id(self, prediction_id: str) -> List[PredictionComparison]:
        pass

    @abstractmethod
    def get_comparisons_by_outcome_id(self, outcome_id: str) -> List[PredictionComparison]:
        pass

    @abstractmethod
    def get_comparisons_by_decision_id(self, decision_id: str) -> List[PredictionComparison]:
        pass

    @abstractmethod
    def get_comparisons_by_mission_id(self, mission_id: str) -> List[PredictionComparison]:
        pass

    @abstractmethod
    def get_comparison_by_idempotency_key(self, idempotency_key: str) -> Optional[PredictionComparison]:
        pass
