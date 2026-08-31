import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.domain.result.ports import ResultRepository


class JsonResultRepositoryError(Exception):
    """Excepción base para errores de JsonResultRepository."""
    pass


class InvalidResultDataError(JsonResultRepositoryError):
    """Se lanza cuando los datos del resultado leído están corruptos o son inválidos."""
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


def _decode_result_record(data: Dict[str, Any]) -> ActionResultRecord:
    try:
        observed_at = datetime.fromisoformat(data["observed_at"]) if isinstance(data.get("observed_at"), str) else datetime.now(timezone.utc)
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))
        outcome = ResultOutcome(data.get("outcome", ResultOutcome.UNKNOWN.value))

        return ActionResultRecord(
            result_id=data["result_id"],
            action_id=data["action_id"],
            decision_id=data["decision_id"],
            mission_id=data["mission_id"],
            outcome=outcome,
            observed_at=observed_at,
            response_summary=data.get("response_summary", {}),
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
        raise InvalidResultDataError(f"Failed to decode ActionResultRecord: {e}") from e


class JsonResultRepository(ResultRepository):
    """
    Implementación JSON durable del puerto ResultRepository.
    Sanitiza PII y contraseñas, garantiza escrituras atómicas.
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
            raise InvalidResultDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonResultRepositoryError(f"Error reading file {self.file_path}: {e}") from e

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
            raise JsonResultRepositoryError(f"Failed to save results to {self.file_path}: {e}") from e

    def save(self, result: ActionResultRecord) -> None:
        records = self._load_all()
        encoded = _encode_json_value({
            "result_id": result.result_id,
            "action_id": result.action_id,
            "decision_id": result.decision_id,
            "mission_id": result.mission_id,
            "outcome": result.outcome,
            "observed_at": result.observed_at,
            "response_summary": result.response_summary,
            "evidence_reference": result.evidence_reference,
            "error_message": result.error_message,
            "confidence": result.confidence,
            "provenance": result.provenance,
            "correlation_id": result.correlation_id,
            "idempotency_key": result.idempotency_key,
            "version": result.version,
            "metadata": result.metadata,
        })
        records[result.result_id] = encoded
        self._save_all(records)

    def get_by_id(self, result_id: str) -> Optional[ActionResultRecord]:
        records = self._load_all()
        data = records.get(result_id)
        if not data:
            return None
        return _decode_result_record(data)

    def get_by_action_id(self, action_id: str) -> Optional[ActionResultRecord]:
        records = self._load_all()
        for data in records.values():
            if data.get("action_id") == action_id:
                return _decode_result_record(data)
        return None

    def get_by_decision_id(self, decision_id: str) -> List[ActionResultRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("decision_id") == decision_id:
                results.append(_decode_result_record(data))
        return results

    def get_by_mission_id(self, mission_id: str) -> List[ActionResultRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("mission_id") == mission_id:
                results.append(_decode_result_record(data))
        return results

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ActionResultRecord]:
        records = self._load_all()
        for data in records.values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_result_record(data)
        return None

    def exists(self, result_id: str) -> bool:
        records = self._load_all()
        return result_id in records
