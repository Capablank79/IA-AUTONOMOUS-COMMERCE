import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.action.models import ActionRecord, ActionStatus
from src.domain.action.ports import ActionRepository


class JsonActionRepositoryError(Exception):
    """Excepción base para errores de JsonActionRepository."""
    pass


class InvalidActionDataError(JsonActionRepositoryError):
    """Se lanza cuando los datos de la acción leída están corruptos o son inválidos."""
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


def _decode_action_record(data: Dict[str, Any]) -> ActionRecord:
    try:
        created_at = datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else datetime.now(timezone.utc)
        updated_at = datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else datetime.now(timezone.utc)
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))
        status = ActionStatus(data.get("status", ActionStatus.PENDING.value))

        return ActionRecord(
            action_id=data["action_id"],
            decision_id=data["decision_id"],
            mission_id=data["mission_id"],
            action_type=data["action_type"],
            status=status,
            target_resource=data.get("target_resource"),
            parameters=data.get("parameters", {}),
            created_at=created_at,
            updated_at=updated_at,
            correlation_id=data.get("correlation_id", "default-correlation"),
            idempotency_key=data.get("idempotency_key", "default-idempotency"),
            version=data.get("version", 1),
            provenance=provenance,
            policy_reference=data.get("policy_reference"),
            approval_reference=data.get("approval_reference"),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidActionDataError(f"Failed to decode ActionRecord: {e}") from e


class JsonActionRepository(ActionRepository):
    """
    Implementación en almacenamiento JSON del puerto ActionRepository.
    Persistencia durable, atómica y segura con sanitización de PII/credenciales.
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
            raise InvalidActionDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonActionRepositoryError(f"Error reading file {self.file_path}: {e}") from e

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
            raise JsonActionRepositoryError(f"Failed to save actions to {self.file_path}: {e}") from e

    def save(self, action: ActionRecord) -> None:
        records = self._load_all()
        encoded = _encode_json_value({
            "action_id": action.action_id,
            "decision_id": action.decision_id,
            "mission_id": action.mission_id,
            "action_type": action.action_type,
            "status": action.status,
            "target_resource": action.target_resource,
            "parameters": action.parameters,
            "created_at": action.created_at,
            "updated_at": action.updated_at,
            "correlation_id": action.correlation_id,
            "idempotency_key": action.idempotency_key,
            "version": action.version,
            "provenance": action.provenance,
            "policy_reference": action.policy_reference,
            "approval_reference": action.approval_reference,
            "metadata": action.metadata,
        })
        records[action.action_id] = encoded
        self._save_all(records)

    def get_by_id(self, action_id: str) -> Optional[ActionRecord]:
        records = self._load_all()
        data = records.get(action_id)
        if not data:
            return None
        return _decode_action_record(data)

    def get_by_decision_id(self, decision_id: str) -> List[ActionRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("decision_id") == decision_id:
                results.append(_decode_action_record(data))
        return results

    def get_by_mission_id(self, mission_id: str) -> List[ActionRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("mission_id") == mission_id:
                results.append(_decode_action_record(data))
        return results

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ActionRecord]:
        records = self._load_all()
        for data in records.values():
            if data.get("idempotency_key") == idempotency_key:
                return _decode_action_record(data)
        return None

    def exists(self, action_id: str) -> bool:
        records = self._load_all()
        return action_id in records
