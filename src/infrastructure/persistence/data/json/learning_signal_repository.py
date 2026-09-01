import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.learning_signals.models import (
    LearningSignalRecord,
    LearningSignalType,
    LearningSignalSubjectType,
    LearningSignalSourceType,
    SignalEvidenceClassification,
    SignalStatus,
)
from src.domain.learning_signals.ports import LearningSignalRepositoryPort


class JsonLearningSignalRepositoryError(Exception):
    """Excepción base para errores de JsonLearningSignalRepository."""
    pass


class InvalidLearningSignalDataError(JsonLearningSignalRepositoryError):
    """Se lanza cuando los datos de learning signal leídos están corruptos o son inválidos."""
    pass


SENSITIVE_KEYS = {
    "password", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "auth", "authorization",
    "private_key", "credentials", "payment", "card", "pan", "cvv"
}


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


def _decode_signal_record(data: Dict[str, Any]) -> LearningSignalRecord:
    try:
        observed_at = (
            datetime.fromisoformat(data["observed_at"])
            if isinstance(data.get("observed_at"), str)
            else datetime.now(timezone.utc)
        )
        created_at = (
            datetime.fromisoformat(data["created_at"])
            if isinstance(data.get("created_at"), str)
            else datetime.now(timezone.utc)
        )

        sig_type = LearningSignalType(data["signal_type"])
        subj_type = LearningSignalSubjectType(data["subject_type"])
        src_type = LearningSignalSourceType(data["source_type"])
        ev_class = SignalEvidenceClassification(data.get("evidence_classification", SignalEvidenceClassification.DERIVED.value))
        status = SignalStatus(data.get("status", SignalStatus.VALID.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))

        return LearningSignalRecord(
            signal_id=data["signal_id"],
            signal_type=sig_type,
            subject_type=subj_type,
            subject_id=data["subject_id"],
            source_type=src_type,
            source_id=data["source_id"],
            evidence_classification=ev_class,
            status=status,
            mission_id=data.get("mission_id"),
            decision_id=data.get("decision_id"),
            action_id=data.get("action_id"),
            result_id=data.get("result_id"),
            outcome_id=data.get("outcome_id"),
            prediction_id=data.get("prediction_id"),
            comparison_id=data.get("comparison_id"),
            calibration_id=data.get("calibration_id"),
            product_performance_id=data.get("product_performance_id"),
            supplier_performance_id=data.get("supplier_performance_id"),
            strategy_performance_id=data.get("strategy_performance_id"),
            signal_value=data.get("signal_value", {}),
            summary=data.get("summary", ""),
            observed_at=observed_at,
            created_at=created_at,
            evidence_reference=data.get("evidence_reference"),
            confidence=confidence,
            provenance=provenance,
            correlation_id=data.get("correlation_id", "default-correlation"),
            idempotency_key=data.get("idempotency_key", "default-idempotency"),
            version=int(data.get("version", 1)),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidLearningSignalDataError(f"Failed to decode LearningSignalRecord: {e}") from e


class JsonLearningSignalRepository(LearningSignalRepositoryPort):
    """
    Implementación JSON durable del puerto LearningSignalRepositoryPort.
    Mantiene señales de aprendizaje estructuradas en almacenamiento JSON persistente con escrituras atómicas.
    """

    def __init__(self, file_path: Union[str, Path]):
        self.file_path = Path(file_path)

    def _load_all(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            return {"signals": {}}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {"signals": {}}
                data = json.loads(content)
                if "signals" not in data:
                    return {"signals": {}}
                return data
        except json.JSONDecodeError as e:
            raise InvalidLearningSignalDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonLearningSignalRepositoryError(f"Error reading file {self.file_path}: {e}") from e

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
            raise JsonLearningSignalRepositoryError(f"Failed to save signals to {self.file_path}: {e}") from e

    def save_signal(self, signal: LearningSignalRecord) -> None:
        root = self._load_all()
        encoded = _encode_json_value({
            "signal_id": signal.signal_id,
            "signal_type": signal.signal_type,
            "subject_type": signal.subject_type,
            "subject_id": signal.subject_id,
            "source_type": signal.source_type,
            "source_id": signal.source_id,
            "evidence_classification": signal.evidence_classification,
            "status": signal.status,
            "mission_id": signal.mission_id,
            "decision_id": signal.decision_id,
            "action_id": signal.action_id,
            "result_id": signal.result_id,
            "outcome_id": signal.outcome_id,
            "prediction_id": signal.prediction_id,
            "comparison_id": signal.comparison_id,
            "calibration_id": signal.calibration_id,
            "product_performance_id": signal.product_performance_id,
            "supplier_performance_id": signal.supplier_performance_id,
            "strategy_performance_id": signal.strategy_performance_id,
            "signal_value": signal.signal_value,
            "summary": signal.summary,
            "observed_at": signal.observed_at,
            "created_at": signal.created_at,
            "evidence_reference": signal.evidence_reference,
            "confidence": signal.confidence,
            "provenance": signal.provenance,
            "correlation_id": signal.correlation_id,
            "idempotency_key": signal.idempotency_key,
            "version": signal.version,
            "metadata": signal.metadata,
        })
        root["signals"][signal.signal_id] = encoded
        self._save_all(root)

    def get_signal_by_id(self, signal_id: str) -> Optional[LearningSignalRecord]:
        root = self._load_all()
        data = root["signals"].get(signal_id)
        if not data:
            return None
        return _decode_signal_record(data)

    def get_signals_by_subject(self, subject_type: LearningSignalSubjectType, subject_id: str) -> List[LearningSignalRecord]:
        root = self._load_all()
        results = []
        for data in root["signals"].values():
            if data.get("subject_type") == subject_type.value and data.get("subject_id") == subject_id:
                results.append(_decode_signal_record(data))
        return results

    def get_signals_by_type(self, signal_type: LearningSignalType) -> List[LearningSignalRecord]:
        root = self._load_all()
        results = []
        for data in root["signals"].values():
            if data.get("signal_type") == signal_type.value:
                results.append(_decode_signal_record(data))
        return results

    def get_signal_by_idempotency_key(self, idempotency_key: str) -> Optional[LearningSignalRecord]:
        root = self._load_all()
        for data in root["signals"].values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_signal_record(data)
        return None

    def list_all(self) -> List[LearningSignalRecord]:
        root = self._load_all()
        return [_decode_signal_record(d) for d in root["signals"].values()]
