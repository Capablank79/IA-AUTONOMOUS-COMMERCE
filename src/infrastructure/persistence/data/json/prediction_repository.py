import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.prediction.models import PredictionRecord, PredictionComparison, ComparisonStatus
from src.domain.prediction.ports import PredictionRepository


class JsonPredictionRepositoryError(Exception):
    """Excepción base para errores de JsonPredictionRepository."""
    pass


class InvalidPredictionDataError(JsonPredictionRepositoryError):
    """Se lanza cuando los datos de la predicción/comparación leídos están corruptos o son inválidos."""
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


def _decode_prediction_record(data: Dict[str, Any]) -> PredictionRecord:
    try:
        created_at = datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else datetime.now(timezone.utc)
        expected_at = datetime.fromisoformat(data["expected_at"]) if isinstance(data.get("expected_at"), str) else None
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))

        return PredictionRecord(
            prediction_id=data["prediction_id"],
            mission_id=data["mission_id"],
            decision_id=data["decision_id"],
            action_id=data.get("action_id"),
            target_metric=data.get("target_metric", "general"),
            predicted_value=data.get("predicted_value"),
            predicted_class=data.get("predicted_class"),
            created_at=created_at,
            expected_at=expected_at,
            confidence=confidence,
            provenance=provenance,
            evidence_reference=data.get("evidence_reference"),
            correlation_id=data.get("correlation_id", "default-correlation"),
            idempotency_key=data.get("idempotency_key", "default-idempotency"),
            version=data.get("version", 1),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidPredictionDataError(f"Failed to decode PredictionRecord: {e}") from e


def _decode_prediction_comparison(data: Dict[str, Any]) -> PredictionComparison:
    try:
        evaluated_at = datetime.fromisoformat(data["evaluated_at"]) if isinstance(data.get("evaluated_at"), str) else datetime.now(timezone.utc)
        prediction_timestamp = datetime.fromisoformat(data["prediction_timestamp"]) if isinstance(data.get("prediction_timestamp"), str) else datetime.now(timezone.utc)
        outcome_timestamp = datetime.fromisoformat(data["outcome_timestamp"]) if isinstance(data.get("outcome_timestamp"), str) else datetime.now(timezone.utc)
        status = ComparisonStatus(data.get("status", ComparisonStatus.UNKNOWN.value))
        prediction_provenance = EvidenceProvenanceType(data.get("prediction_provenance", EvidenceProvenanceType.DERIVED.value))
        outcome_provenance = EvidenceProvenanceType(data.get("outcome_provenance", EvidenceProvenanceType.LIVE.value))
        prediction_confidence = Confidence(data.get("prediction_confidence", Confidence.MEDIUM.value))

        return PredictionComparison(
            comparison_id=data["comparison_id"],
            prediction_id=data["prediction_id"],
            outcome_id=data["outcome_id"],
            mission_id=data["mission_id"],
            decision_id=data["decision_id"],
            action_id=data.get("action_id"),
            target_metric=data.get("target_metric", "general"),
            expected_value=data.get("expected_value"),
            actual_value=data.get("actual_value"),
            delta=float(data["delta"]) if data.get("delta") is not None else None,
            status=status,
            evaluated_at=evaluated_at,
            prediction_timestamp=prediction_timestamp,
            outcome_timestamp=outcome_timestamp,
            prediction_provenance=prediction_provenance,
            outcome_provenance=outcome_provenance,
            prediction_confidence=prediction_confidence,
            correlation_id=data.get("correlation_id", "default-correlation"),
            idempotency_key=data.get("idempotency_key", "default-idempotency"),
            version=data.get("version", 1),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidPredictionDataError(f"Failed to decode PredictionComparison: {e}") from e


class JsonPredictionRepository(PredictionRepository):
    """
    Implementación JSON durable del puerto PredictionRepository.
    Mantiene predicciones y comparaciones en almacenamiento JSON persistente con escrituras atómicas.
    """

    def __init__(self, file_path: Union[str, Path]):
        self.file_path = Path(file_path)

    def _load_all(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {"predictions": {}, "comparisons": {}}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {"predictions": {}, "comparisons": {}}
                data = json.loads(content)
                if "predictions" not in data or "comparisons" not in data:
                    return {"predictions": {}, "comparisons": {}}
                return data
        except json.JSONDecodeError as e:
            raise InvalidPredictionDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonPredictionRepositoryError(f"Error reading file {self.file_path}: {e}") from e

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
            raise JsonPredictionRepositoryError(f"Failed to save predictions to {self.file_path}: {e}") from e

    def save_prediction(self, prediction: PredictionRecord) -> None:
        root = self._load_all()
        encoded = _encode_json_value({
            "prediction_id": prediction.prediction_id,
            "mission_id": prediction.mission_id,
            "decision_id": prediction.decision_id,
            "action_id": prediction.action_id,
            "target_metric": prediction.target_metric,
            "predicted_value": prediction.predicted_value,
            "predicted_class": prediction.predicted_class,
            "created_at": prediction.created_at,
            "expected_at": prediction.expected_at,
            "confidence": prediction.confidence,
            "provenance": prediction.provenance,
            "evidence_reference": prediction.evidence_reference,
            "correlation_id": prediction.correlation_id,
            "idempotency_key": prediction.idempotency_key,
            "version": prediction.version,
            "metadata": prediction.metadata,
        })
        root["predictions"][prediction.prediction_id] = encoded
        self._save_all(root)

    def get_prediction_by_id(self, prediction_id: str) -> Optional[PredictionRecord]:
        root = self._load_all()
        data = root["predictions"].get(prediction_id)
        if not data:
            return None
        return _decode_prediction_record(data)

    def get_predictions_by_decision_id(self, decision_id: str) -> List[PredictionRecord]:
        root = self._load_all()
        results = []
        for data in root["predictions"].values():
            if data.get("decision_id") == decision_id:
                results.append(_decode_prediction_record(data))
        return results

    def get_predictions_by_mission_id(self, mission_id: str) -> List[PredictionRecord]:
        root = self._load_all()
        results = []
        for data in root["predictions"].values():
            if data.get("mission_id") == mission_id:
                results.append(_decode_prediction_record(data))
        return results

    def get_prediction_by_idempotency_key(self, idempotency_key: str) -> Optional[PredictionRecord]:
        root = self._load_all()
        for data in root["predictions"].values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_prediction_record(data)
        return None

    def save_comparison(self, comparison: PredictionComparison) -> None:
        root = self._load_all()
        encoded = _encode_json_value({
            "comparison_id": comparison.comparison_id,
            "prediction_id": comparison.prediction_id,
            "outcome_id": comparison.outcome_id,
            "mission_id": comparison.mission_id,
            "decision_id": comparison.decision_id,
            "action_id": comparison.action_id,
            "target_metric": comparison.target_metric,
            "expected_value": comparison.expected_value,
            "actual_value": comparison.actual_value,
            "delta": comparison.delta,
            "status": comparison.status,
            "evaluated_at": comparison.evaluated_at,
            "prediction_timestamp": comparison.prediction_timestamp,
            "outcome_timestamp": comparison.outcome_timestamp,
            "prediction_provenance": comparison.prediction_provenance,
            "outcome_provenance": comparison.outcome_provenance,
            "prediction_confidence": comparison.prediction_confidence,
            "correlation_id": comparison.correlation_id,
            "idempotency_key": comparison.idempotency_key,
            "version": comparison.version,
            "metadata": comparison.metadata,
        })
        root["comparisons"][comparison.comparison_id] = encoded
        self._save_all(root)

    def get_comparison_by_id(self, comparison_id: str) -> Optional[PredictionComparison]:
        root = self._load_all()
        data = root["comparisons"].get(comparison_id)
        if not data:
            return None
        return _decode_prediction_comparison(data)

    def get_comparisons_by_prediction_id(self, prediction_id: str) -> List[PredictionComparison]:
        root = self._load_all()
        results = []
        for data in root["comparisons"].values():
            if data.get("prediction_id") == prediction_id:
                results.append(_decode_prediction_comparison(data))
        return results

    def get_comparisons_by_outcome_id(self, outcome_id: str) -> List[PredictionComparison]:
        root = self._load_all()
        results = []
        for data in root["comparisons"].values():
            if data.get("outcome_id") == outcome_id:
                results.append(_decode_prediction_comparison(data))
        return results

    def get_comparisons_by_decision_id(self, decision_id: str) -> List[PredictionComparison]:
        root = self._load_all()
        results = []
        for data in root["comparisons"].values():
            if data.get("decision_id") == decision_id:
                results.append(_decode_prediction_comparison(data))
        return results

    def get_comparisons_by_mission_id(self, mission_id: str) -> List[PredictionComparison]:
        root = self._load_all()
        results = []
        for data in root["comparisons"].values():
            if data.get("mission_id") == mission_id:
                results.append(_decode_prediction_comparison(data))
        return results

    def get_comparison_by_idempotency_key(self, idempotency_key: str) -> Optional[PredictionComparison]:
        root = self._load_all()
        for data in root["comparisons"].values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_prediction_comparison(data)
        return None
