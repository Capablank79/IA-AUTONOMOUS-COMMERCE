from abc import ABC, abstractmethod
from typing import Optional, List
from src.domain.calibration.models import DecisionCalibrationRecord


class CalibrationRepository(ABC):
    """
    Puerto primario del repositorio para guardar y consultar DecisionCalibrationRecord.
    """

    @abstractmethod
    def save_calibration(self, calibration: DecisionCalibrationRecord) -> None:
        pass

    @abstractmethod
    def get_calibration_by_id(self, calibration_id: str) -> Optional[DecisionCalibrationRecord]:
        pass

    @abstractmethod
    def get_calibrations_by_decision_id(self, decision_id: str) -> List[DecisionCalibrationRecord]:
        pass

    @abstractmethod
    def get_calibrations_by_mission_id(self, mission_id: str) -> List[DecisionCalibrationRecord]:
        pass

    @abstractmethod
    def get_calibration_by_idempotency_key(self, idempotency_key: str) -> Optional[DecisionCalibrationRecord]:
        pass
