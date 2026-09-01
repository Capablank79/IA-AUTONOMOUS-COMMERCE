import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.outcome.ports import OutcomeRepository


class JsonOutcomeRepositoryError(Exception):
    """Excepción base para errores de JsonOutcomeRepository."""
    pass


class InvalidOutcomeDataError(JsonOutcomeRepositoryError):
    """Se lanza cuando los datos del outcome leído están corruptos o son inválidos."""
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


def _decode_outcome_record(data: Dict[str, Any]) -> OutcomeRecord:
    try:
        observed_at = datetime.fromisoformat(data["observed_at"]) if isinstance(data.get("observed_at"), str) else datetime.now(timezone.utc)
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))
        status = OutcomeStatus(data.get("status", OutcomeStatus.UNKNOWN.value))

        return OutcomeRecord(
            outcome_id=data["outcome_id"],
            mission_id=data["mission_id"],
            decision_id=data["decision_id"],
            action_id=data["action_id"],
            result_id=data.get("result_id"),
            outcome_type=data.get("outcome_type", "BUSINESS_OBSERVATION"),
            status=status,
            observed_at=observed_at,
            value_metrics=data.get("value_metrics", {}),
            evidence_reference=data.get("evidence_reference"),
            error_message=data.get("error_message"),
            confidence=confidence,
            provenance=provenance,
            correlation_id=data.get("correlation_id", "default-correlation"),
            idempotency_key=data.get("idempotency_key", "default-idempotency"),
            version=data.get("version", 1),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidOutcomeDataError(f"Failed to decode OutcomeRecord: {e}") from e


class JsonOutcomeRepository(OutcomeRepository):
    """
    Implementación JSON durable del puerto OutcomeRepository.
    Sanitiza datos sensibles (secretos, credenciales, tokens) y garantiza escrituras atómicas.
    """

    def __init__(self, file_path: Union[str, Path]):
        self.file_path = Path(file_path)

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except json.JSONDecodeError as e:
            raise InvalidOutcomeDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonOutcomeRepositoryError(f"Error reading file {self.file_path}: {e}") from e

    def _save_all(self, data: Dict[str, Dict[str, Any]]) -> None:
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
            raise JsonOutcomeRepositoryError(f"Failed to save outcomes to {self.file_path}: {e}") from e

    def save(self, outcome: OutcomeRecord) -> None:
        records = self._load_all()
        encoded = _encode_json_value({
            "outcome_id": outcome.outcome_id,
            "mission_id": outcome.mission_id,
            "decision_id": outcome.decision_id,
            "action_id": outcome.action_id,
            "result_id": outcome.result_id,
            "outcome_type": outcome.outcome_type,
            "status": outcome.status,
            "observed_at": outcome.observed_at,
            "value_metrics": outcome.value_metrics,
            "evidence_reference": outcome.evidence_reference,
            "error_message": outcome.error_message,
            "confidence": outcome.confidence,
            "provenance": outcome.provenance,
            "correlation_id": outcome.correlation_id,
            "idempotency_key": outcome.idempotency_key,
            "version": outcome.version,
            "metadata": outcome.metadata,
        })
        records[outcome.outcome_id] = encoded
        self._save_all(records)

    def get_by_id(self, outcome_id: str) -> Optional[OutcomeRecord]:
        records = self._load_all()
        data = records.get(outcome_id)
        if not data:
            return None
        return _decode_outcome_record(data)

    def get_by_action_id(self, action_id: str) -> List[OutcomeRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("action_id") == action_id:
                results.append(_decode_outcome_record(data))
        return results

    def get_by_decision_id(self, decision_id: str) -> List[OutcomeRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("decision_id") == decision_id:
                results.append(_decode_outcome_record(data))
        return results

    def get_by_mission_id(self, mission_id: str) -> List[OutcomeRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("mission_id") == mission_id:
                results.append(_decode_outcome_record(data))
        return results

    def get_by_result_id(self, result_id: str) -> List[OutcomeRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("result_id") == result_id:
                results.append(_decode_outcome_record(data))
        return results

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[OutcomeRecord]:
        records = self._load_all()
        for data in records.values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_outcome_record(data)
        return None

    def exists(self, outcome_id: str) -> bool:
        records = self._load_all()
        return outcome_id in records
