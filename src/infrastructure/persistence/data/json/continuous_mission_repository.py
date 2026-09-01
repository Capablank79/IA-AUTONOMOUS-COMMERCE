"""
Adaptador de persistencia JSON para Misiones Continuas (Continuous Missions - Hito J.7).

Implementa `ContinuousMissionRepositoryPort` siguiendo los estándares de Hito H / J:
- Escritura atómica vía `.tmp` + `replace()`.
- Thread-safe mediante RLock.
- Serialización determinista ISO-8601, Decimal y Enums.
- Sanitización y exclusión automática de secretos y tokens.
- Recuperación resiliente ante reinicios y archivos corruptos.
- Cero creación de base de datos paralela.
"""

import json
import os
import threading
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.mission.models import MissionType, MissionPriority
from src.domain.continuous_mission.models import (
    ContinuousMission,
    ContinuousMissionCycle,
    ContinuousMissionStatus,
    ContinuousCycleStatus,
    ContinuousMissionStopCondition,
)
from src.domain.continuous_mission.ports import ContinuousMissionRepositoryPort


class JsonContinuousMissionRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de Continuous Missions."""
    pass


class CorruptedContinuousMissionDataError(JsonContinuousMissionRepositoryError):
    """Se lanza cuando un archivo JSON de Continuous Mission o Cycle está corrupto."""
    pass


SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "token",
    "password",
    "secret",
    "api_key",
    "authorization",
    "client_secret",
    "card_number",
    "pan",
    "cvv",
    "private_key",
}


def _sanitize_data(val: Any) -> Any:
    """Sanitiza recursivamente datos para excluir credenciales y tokens sensibles."""
    if isinstance(val, dict):
        sanitized = {}
        for k, v in val.items():
            key_str = str(k).lower()
            if any(s in key_str for s in SENSITIVE_KEYS):
                sanitized[str(k)] = "[REDACTED]"
            else:
                sanitized[str(k)] = _sanitize_data(v)
        return sanitized
    if isinstance(val, (list, tuple, set)):
        return [_sanitize_data(v) for v in val]
    return val


def _encode_json_value(val: Any) -> Any:
    """Convierte tipos complejos a tipos JSON nativos."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        return {str(k): _encode_json_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_encode_json_value(v) for v in val]
    return val


def _parse_datetime(dt_val: Optional[str]) -> Optional[datetime]:
    if not dt_val:
        return None
    try:
        dt = datetime.fromisoformat(dt_val)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class JsonContinuousMissionRepository(ContinuousMissionRepositoryPort):
    """
    Implementación JSON de ContinuousMissionRepositoryPort.
    Almacena misiones continuas en `storage_dir / "continuous_missions"` y
    ciclos en `storage_dir / "continuous_cycles"`.
    """

    def __init__(self, storage_dir: Union[str, Path]):
        self.storage_dir = Path(storage_dir)
        self._missions_dir = self.storage_dir / "continuous_missions"
        self._cycles_dir = self.storage_dir / "continuous_cycles"
        self._lock = threading.RLock()

        self._missions_dir.mkdir(parents=True, exist_ok=True)
        self._cycles_dir.mkdir(parents=True, exist_ok=True)

    def save(self, continuous_mission: ContinuousMission) -> None:
        with self._lock:
            file_path = self._missions_dir / f"{continuous_mission.continuous_mission_id}.json"

            raw_data = {
                "continuous_mission_id": continuous_mission.continuous_mission_id,
                "schedule_id": continuous_mission.schedule_id,
                "mission_type": continuous_mission.mission_type.value,
                "goal": continuous_mission.goal,
                "status": continuous_mission.status.value,
                "priority": continuous_mission.priority.value,
                "mission_parameters": _encode_json_value(continuous_mission.mission_parameters),
                "stop_condition": {
                    "max_cycles": continuous_mission.stop_condition.max_cycles,
                    "max_consecutive_failures": continuous_mission.stop_condition.max_consecutive_failures,
                    "stop_on_unknown": continuous_mission.stop_condition.stop_on_unknown,
                    "custom_criteria": _encode_json_value(continuous_mission.stop_condition.custom_criteria),
                },
                "created_at": continuous_mission.created_at.isoformat() if continuous_mission.created_at else None,
                "started_at": continuous_mission.started_at.isoformat() if continuous_mission.started_at else None,
                "last_cycle_at": continuous_mission.last_cycle_at.isoformat() if continuous_mission.last_cycle_at else None,
                "next_cycle_at": continuous_mission.next_cycle_at.isoformat() if continuous_mission.next_cycle_at else None,
                "cycle_count": continuous_mission.cycle_count,
                "consecutive_failures": continuous_mission.consecutive_failures,
                "total_failures": continuous_mission.total_failures,
                "last_result_status": continuous_mission.last_result_status,
                "last_cycle_id": continuous_mission.last_cycle_id,
                "last_mission_id": continuous_mission.last_mission_id,
                "correlation_id": continuous_mission.correlation_id,
                "provenance": continuous_mission.provenance,
                "metadata": _encode_json_value(continuous_mission.metadata),
            }

            sanitized_data = _sanitize_data(raw_data)

            temp_path = file_path.with_suffix(".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(sanitized_data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                temp_path.replace(file_path)
            except Exception as e:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass
                raise JsonContinuousMissionRepositoryError(f"Failed to save ContinuousMission: {str(e)}") from e

    def get_by_id(self, continuous_mission_id: str) -> Optional[ContinuousMission]:
        with self._lock:
            file_path = self._missions_dir / f"{continuous_mission_id}.json"
            if not file_path.exists():
                return None
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._deserialize_mission(data)
            except Exception as e:
                raise CorruptedContinuousMissionDataError(f"Corrupted ContinuousMission file {file_path}: {str(e)}") from e

    def get_by_schedule_id(self, schedule_id: str) -> Optional[ContinuousMission]:
        with self._lock:
            for mission in self.list_all():
                if mission.schedule_id == schedule_id:
                    return mission
            return None

    def list_all(self) -> List[ContinuousMission]:
        with self._lock:
            missions = []
            for file_path in self._missions_dir.glob("*.json"):
                if file_path.name.endswith(".tmp"):
                    continue
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    missions.append(self._deserialize_mission(data))
                except Exception:
                    continue
            return sorted(missions, key=lambda m: m.created_at or datetime.min.replace(tzinfo=timezone.utc))

    def list_active(self) -> List[ContinuousMission]:
        with self._lock:
            return [m for m in self.list_all() if m.status == ContinuousMissionStatus.ACTIVE]

    def save_cycle(self, cycle: ContinuousMissionCycle) -> None:
        with self._lock:
            file_path = self._cycles_dir / f"{cycle.cycle_id}.json"

            raw_data = {
                "cycle_id": cycle.cycle_id,
                "continuous_mission_id": cycle.continuous_mission_id,
                "cycle_number": cycle.cycle_number,
                "scheduled_at": cycle.scheduled_at.isoformat() if cycle.scheduled_at else None,
                "started_at": cycle.started_at.isoformat() if cycle.started_at else None,
                "completed_at": cycle.completed_at.isoformat() if cycle.completed_at else None,
                "status": cycle.status.value,
                "mission_id": cycle.mission_id,
                "occurrence_id": cycle.occurrence_id,
                "idempotency_key": cycle.idempotency_key,
                "correlation_id": cycle.correlation_id,
                "causation_id": cycle.causation_id,
                "provenance": cycle.provenance,
                "result_summary": _encode_json_value(cycle.result_summary),
                "error_message": cycle.error_message,
            }

            sanitized_data = _sanitize_data(raw_data)

            temp_path = file_path.with_suffix(".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(sanitized_data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                temp_path.replace(file_path)
            except Exception as e:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass
                raise JsonContinuousMissionRepositoryError(f"Failed to save ContinuousMissionCycle: {str(e)}") from e

    def get_cycle(self, cycle_id: str) -> Optional[ContinuousMissionCycle]:
        with self._lock:
            file_path = self._cycles_dir / f"{cycle_id}.json"
            if not file_path.exists():
                return None
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._deserialize_cycle(data)
            except Exception as e:
                raise CorruptedContinuousMissionDataError(f"Corrupted ContinuousMissionCycle file {file_path}: {str(e)}") from e

    def get_cycle_by_idempotency_key(self, idempotency_key: str) -> Optional[ContinuousMissionCycle]:
        with self._lock:
            for cycle in self.list_cycles():
                if cycle.idempotency_key == idempotency_key:
                    return cycle
            return None

    def list_cycles(self, continuous_mission_id: Optional[str] = None) -> List[ContinuousMissionCycle]:
        with self._lock:
            cycles = []
            for file_path in self._cycles_dir.glob("*.json"):
                if file_path.name.endswith(".tmp"):
                    continue
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    cycle = self._deserialize_cycle(data)
                    if continuous_mission_id is None or cycle.continuous_mission_id == continuous_mission_id:
                        cycles.append(cycle)
                except Exception:
                    continue
            return sorted(cycles, key=lambda c: (c.cycle_number, c.started_at or datetime.min.replace(tzinfo=timezone.utc)))

    def _deserialize_mission(self, data: Dict[str, Any]) -> ContinuousMission:
        stop_cond_data = data.get("stop_condition", {})
        stop_cond = ContinuousMissionStopCondition(
            max_cycles=stop_cond_data.get("max_cycles"),
            max_consecutive_failures=stop_cond_data.get("max_consecutive_failures", 3),
            stop_on_unknown=stop_cond_data.get("stop_on_unknown", False),
            custom_criteria=stop_cond_data.get("custom_criteria", {}),
        )

        return ContinuousMission(
            continuous_mission_id=data["continuous_mission_id"],
            schedule_id=data["schedule_id"],
            mission_type=MissionType(data["mission_type"]),
            goal=data.get("goal", ""),
            status=ContinuousMissionStatus(data["status"]),
            priority=MissionPriority(data.get("priority", "MEDIUM")),
            mission_parameters=data.get("mission_parameters", {}),
            stop_condition=stop_cond,
            created_at=_parse_datetime(data.get("created_at")) or datetime.now(timezone.utc),
            started_at=_parse_datetime(data.get("started_at")),
            last_cycle_at=_parse_datetime(data.get("last_cycle_at")),
            next_cycle_at=_parse_datetime(data.get("next_cycle_at")),
            cycle_count=data.get("cycle_count", 0),
            consecutive_failures=data.get("consecutive_failures", 0),
            total_failures=data.get("total_failures", 0),
            last_result_status=data.get("last_result_status"),
            last_cycle_id=data.get("last_cycle_id"),
            last_mission_id=data.get("last_mission_id"),
            correlation_id=data.get("correlation_id"),
            provenance=data.get("provenance"),
            metadata=data.get("metadata", {}),
        )

    def _deserialize_cycle(self, data: Dict[str, Any]) -> ContinuousMissionCycle:
        return ContinuousMissionCycle(
            cycle_id=data["cycle_id"],
            continuous_mission_id=data["continuous_mission_id"],
            cycle_number=data["cycle_number"],
            scheduled_at=_parse_datetime(data["scheduled_at"]) or datetime.now(timezone.utc),
            started_at=_parse_datetime(data["started_at"]) or datetime.now(timezone.utc),
            completed_at=_parse_datetime(data.get("completed_at")),
            status=ContinuousCycleStatus(data["status"]),
            mission_id=data.get("mission_id"),
            occurrence_id=data.get("occurrence_id"),
            idempotency_key=data.get("idempotency_key", ""),
            correlation_id=data.get("correlation_id"),
            causation_id=data.get("causation_id"),
            provenance=data.get("provenance"),
            result_summary=data.get("result_summary", {}),
            error_message=data.get("error_message"),
        )
