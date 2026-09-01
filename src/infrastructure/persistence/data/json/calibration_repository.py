import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.prediction.models import ComparisonStatus
from src.domain.calibration.models import (
    CalibrationStatus,
    ConfidenceBin,
    DecisionCalibrationRecord,
)
from src.domain.calibration.ports import CalibrationRepository


class JsonCalibrationRepositoryError(Exception):
    """Excepción base para errores de JsonCalibrationRepository."""
    pass


class InvalidCalibrationDataError(JsonCalibrationRepositoryError):
    """Se lanza cuando los datos de calibración leídos están corruptos o son inválidos."""
    pass


SENSITIVE_KEYS = {"password", "secret", "token", "api_key", "apikey", "pan", "cvv", "private_key", "credential"}


def _encode_json_value(val: Any) -> Any:
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                continue
            cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_confidence_bin(data: Dict[str, Any]) -> ConfidenceBin:
    return ConfidenceBin(
        confidence_level=Confidence(data["confidence_level"]),
        sample_count=int(data["sample_count"]),
        match_count=int(data["match_count"]),
        miss_count=int(data["miss_count"]),
        unknown_count=int(data["unknown_count"]),
        observed_success_rate=float(data["observed_success_rate"]),
        expected_confidence_score=float(data["expected_confidence_score"]),
        calibration_gap=float(data["calibration_gap"]),
    )


def _decode_calibration_record(data: Dict[str, Any]) -> DecisionCalibrationRecord:
    try:
        calculated_at = (
            datetime.fromisoformat(data["calculated_at"])
            if isinstance(data.get("calculated_at"), str)
            else datetime.now(timezone.utc)
        )
        status = CalibrationStatus(data.get("status", CalibrationStatus.UNKNOWN.value))
        confidence_bins = tuple(_decode_confidence_bin(b) for b in data.get("confidence_bins", []))

        return DecisionCalibrationRecord(
            calibration_id=data["calibration_id"],
            decision_id=data.get("decision_id"),
            mission_id=data.get("mission_id"),
            target_metric=data.get("target_metric", "general"),
            status=status,
            total_samples=int(data.get("total_samples", 0)),
            valid_samples=int(data.get("valid_samples", 0)),
            unknown_excluded_samples=int(data.get("unknown_excluded_samples", 0)),
            match_count=int(data.get("match_count", 0)),
            miss_count=int(data.get("miss_count", 0)),
            accuracy=float(data.get("accuracy", 0.0)),
            error_rate=float(data.get("error_rate", 0.0)),
            expected_confidence_score=float(data.get("expected_confidence_score", 0.0)),
            brier_score=float(data["brier_score"]) if data.get("brier_score") is not None else None,
            calibration_error=float(data.get("calibration_error", 0.0)),
            confidence_bins=confidence_bins,
            comparison_ids=tuple(data.get("comparison_ids", [])),
            prediction_ids=tuple(data.get("prediction_ids", [])),
            outcome_ids=tuple(data.get("outcome_ids", [])),
            calculated_at=calculated_at,
            correlation_id=data.get("correlation_id", "default-correlation"),
            idempotency_key=data.get("idempotency_key", "default-idempotency"),
            version=int(data.get("version", 1)),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidCalibrationDataError(f"Failed to decode DecisionCalibrationRecord: {e}") from e


class JsonCalibrationRepository(CalibrationRepository):
    """
    Implementación JSON durable del puerto CalibrationRepository.
    Mantiene registros de calibración en almacenamiento JSON persistente con escrituras atómicas.
    """

    def __init__(self, file_path: Union[str, Path]):
        self.file_path = Path(file_path)

    def _load_all(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {"calibrations": {}}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {"calibrations": {}}
                data = json.loads(content)
                if "calibrations" not in data:
                    return {"calibrations": {}}
                return data
        except json.JSONDecodeError as e:
            raise InvalidCalibrationDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonCalibrationRepositoryError(f"Error reading file {self.file_path}: {e}") from e

    def _save_all(self, data: Dict[str, Any]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.file_path)
        except Exception as e:
            if tmp_path.exists():
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise JsonCalibrationRepositoryError(f"Failed to save calibrations to {self.file_path}: {e}") from e

    def save_calibration(self, calibration: DecisionCalibrationRecord) -> None:
        root = self._load_all()
        encoded = _encode_json_value({
            "calibration_id": calibration.calibration_id,
            "decision_id": calibration.decision_id,
            "mission_id": calibration.mission_id,
            "target_metric": calibration.target_metric,
            "status": calibration.status,
            "total_samples": calibration.total_samples,
            "valid_samples": calibration.valid_samples,
            "unknown_excluded_samples": calibration.unknown_excluded_samples,
            "match_count": calibration.match_count,
            "miss_count": calibration.miss_count,
            "accuracy": calibration.accuracy,
            "error_rate": calibration.error_rate,
            "expected_confidence_score": calibration.expected_confidence_score,
            "brier_score": calibration.brier_score,
            "calibration_error": calibration.calibration_error,
            "confidence_bins": [
                {
                    "confidence_level": b.confidence_level,
                    "sample_count": b.sample_count,
                    "match_count": b.match_count,
                    "miss_count": b.miss_count,
                    "unknown_count": b.unknown_count,
                    "observed_success_rate": b.observed_success_rate,
                    "expected_confidence_score": b.expected_confidence_score,
                    "calibration_gap": b.calibration_gap,
                }
                for b in calibration.confidence_bins
            ],
            "comparison_ids": list(calibration.comparison_ids),
            "prediction_ids": list(calibration.prediction_ids),
            "outcome_ids": list(calibration.outcome_ids),
            "calculated_at": calibration.calculated_at,
            "correlation_id": calibration.correlation_id,
            "idempotency_key": calibration.idempotency_key,
            "version": calibration.version,
            "metadata": calibration.metadata,
        })
        root["calibrations"][calibration.calibration_id] = encoded
        self._save_all(root)

    def get_calibration_by_id(self, calibration_id: str) -> Optional[DecisionCalibrationRecord]:
        root = self._load_all()
        data = root["calibrations"].get(calibration_id)
        if not data:
            return None
        return _decode_calibration_record(data)

    def get_calibrations_by_decision_id(self, decision_id: str) -> List[DecisionCalibrationRecord]:
        root = self._load_all()
        results = []
        for data in root["calibrations"].values():
            if data.get("decision_id") == decision_id:
                results.append(_decode_calibration_record(data))
        return results

    def get_calibrations_by_mission_id(self, mission_id: str) -> List[DecisionCalibrationRecord]:
        root = self._load_all()
        results = []
        for data in root["calibrations"].values():
            if data.get("mission_id") == mission_id:
                results.append(_decode_calibration_record(data))
        return results

    def get_calibration_by_idempotency_key(self, idempotency_key: str) -> Optional[DecisionCalibrationRecord]:
        root = self._load_all()
        for data in root["calibrations"].values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_calibration_record(data)
        return None
