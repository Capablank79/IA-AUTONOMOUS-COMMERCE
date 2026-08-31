import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.temporal_state.models import TemporalSnapshot
from src.domain.temporal_state.ports import TemporalStateRepository


class JsonTemporalStateRepositoryError(Exception):
    """Excepción base para errores de JsonTemporalStateRepository."""
    pass


class InvalidTemporalSnapshotDataError(JsonTemporalStateRepositoryError):
    """Se lanza cuando los datos de snapshot leídos están corruptos o son inválidos."""
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


def _decode_temporal_snapshot(data: Dict[str, Any]) -> TemporalSnapshot:
    try:
        timestamp = datetime.fromisoformat(data["timestamp"]) if isinstance(data.get("timestamp"), str) else datetime.now(timezone.utc)
        return TemporalSnapshot(
            snapshot_id=data["snapshot_id"],
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            timestamp=timestamp,
            state_payload=data.get("state_payload", {}),
            correlation_id=data.get("correlation_id", "default-correlation"),
            provenance=data.get("provenance", "DERIVED"),
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidTemporalSnapshotDataError(f"Failed to decode TemporalSnapshot: {e}") from e


class JsonTemporalStateRepository(TemporalStateRepository):
    """
    Implementación JSON durable del puerto TemporalStateRepository.
    Ordena eventos cronológicamente y permite la reconstrucción de estado a tiempo T.
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
            raise InvalidTemporalSnapshotDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonTemporalStateRepositoryError(f"Error reading file {self.file_path}: {e}") from e

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
            raise JsonTemporalStateRepositoryError(f"Failed to save temporal state to {self.file_path}: {e}") from e

    def save_snapshot(self, snapshot: TemporalSnapshot) -> None:
        records = self._load_all()
        raw_dict = {
            "snapshot_id": snapshot.snapshot_id,
            "entity_type": snapshot.entity_type,
            "entity_id": snapshot.entity_id,
            "timestamp": snapshot.timestamp,
            "state_payload": snapshot.state_payload,
            "correlation_id": snapshot.correlation_id,
            "provenance": snapshot.provenance,
            "metadata": snapshot.metadata,
        }
        encoded = _encode_json_value(raw_dict)
        records[snapshot.snapshot_id] = encoded
        self._save_all(records)

    def get_snapshot_by_id(self, snapshot_id: str) -> Optional[TemporalSnapshot]:
        records = self._load_all()
        data = records.get(snapshot_id)
        if not data:
            return None
        return _decode_temporal_snapshot(data)

    def get_history_for_entity(self, entity_type: str, entity_id: str) -> List[TemporalSnapshot]:
        records = self._load_all()
        history = []
        for data in records.values():
            if data.get("entity_type") == entity_type and data.get("entity_id") == entity_id:
                history.append(_decode_temporal_snapshot(data))

        # Sort chronologically by timestamp
        history.sort(key=lambda s: s.timestamp)
        return history

    def get_state_at(self, entity_type: str, entity_id: str, timestamp: datetime) -> Optional[TemporalSnapshot]:
        history = self.get_history_for_entity(entity_type, entity_id)
        if not history:
            return None

        # Normalize timestamp timezone if necessary
        target_ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)

        valid_snapshots = [s for s in history if s.timestamp <= target_ts]
        if not valid_snapshots:
            return None

        # Return the latest snapshot at or before target timestamp
        return valid_snapshots[-1]
