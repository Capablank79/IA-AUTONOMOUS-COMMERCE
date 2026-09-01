import json
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List

from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionPriority,
    MissionStatus,
    MissionResult,
    MissionTraceEntry,
)
from src.domain.mission.ports import MissionRepository


class JsonMissionRepositoryError(Exception):
    """Base exception for JsonMissionRepository errors."""
    pass


class InvalidMissionDataError(JsonMissionRepositoryError):
    """Raised when loaded mission data is corrupted or invalid."""
    pass


from dataclasses import is_dataclass, asdict

def _encode_json_value(val: Any) -> Any:
    """Helper to convert complex objects to JSON-serializable types."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):  # Enum
        return val.value
    if is_dataclass(val) and not isinstance(val, type):
        return _encode_json_value(asdict(val))
    if isinstance(val, dict):
        return {str(k): _encode_json_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonMissionRepository(MissionRepository):
    """
    JSON-file-based implementation of MissionRepository port.
    Provides durable persistence for Mission entities and MissionResult objects.
    """

    def __init__(self, storage_dir: Union[str, Path]):
        self.storage_dir = Path(storage_dir)
        self._missions_dir = self.storage_dir / "missions"
        self._results_dir = self.storage_dir / "results"

        self._missions_dir.mkdir(parents=True, exist_ok=True)
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def save(self, mission: Mission) -> None:
        file_path = self._missions_dir / f"{mission.mission_id}.json"

        data = {
            "mission_id": mission.mission_id,
            "type": mission.type.value,
            "priority": mission.priority.value,
            "status": mission.status.value,
            "parameters": _encode_json_value(mission.parameters),
            "created_at": mission.created_at.isoformat(),
            "updated_at": mission.updated_at.isoformat(),
        }

        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        temp_path.replace(file_path)

    def get_by_id(self, mission_id: str) -> Optional[Mission]:
        file_path = self._missions_dir / f"{mission_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            return Mission(
                mission_id=data["mission_id"],
                type=MissionType(data["type"]),
                priority=MissionPriority(data["priority"]),
                status=MissionStatus(data["status"]),
                parameters=data.get("parameters", {}),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise InvalidMissionDataError(f"Corrupted mission data for {mission_id}: {e}") from e

    def save_result(self, result: MissionResult) -> None:
        file_path = self._results_dir / f"{result.mission_id}.json"

        trace_data: List[Dict[str, Any]] = [
            {
                "step": entry.step,
                "status": entry.status.value,
                "timestamp": entry.timestamp.isoformat(),
                "metadata": _encode_json_value(entry.metadata),
            }
            for entry in result.trace
        ]

        data = {
            "mission_id": result.mission_id,
            "status": result.status.value,
            "output": _encode_json_value(result.output),
            "trace": trace_data,
            "evidences": _encode_json_value(result.evidences),
            "blocks": _encode_json_value(result.blocks),
            "errors": result.errors,
            "finished_at": result.finished_at.isoformat(),
        }

        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        temp_path.replace(file_path)

    def get_result(self, mission_id: str) -> Optional[MissionResult]:
        file_path = self._results_dir / f"{mission_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            trace = [
                MissionTraceEntry(
                    step=t["step"],
                    status=MissionStatus(t["status"]),
                    timestamp=datetime.fromisoformat(t["timestamp"]),
                    metadata=t.get("metadata", {}),
                )
                for t in data.get("trace", [])
            ]

            return MissionResult(
                mission_id=data["mission_id"],
                status=MissionStatus(data["status"]),
                output=data.get("output", {}),
                trace=trace,
                evidences=data.get("evidences", []),
                blocks=data.get("blocks", []),
                errors=data.get("errors", []),
                finished_at=datetime.fromisoformat(data["finished_at"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise InvalidMissionDataError(f"Corrupted mission result data for {mission_id}: {e}") from e
