import json
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List

from src.domain.mission.models import MissionType, MissionPriority
from src.domain.scheduling.models import (
    Schedule,
    ScheduleConfig,
    ScheduleOccurrence,
    ScheduleStatus,
    ScheduleType,
    ExecutionStatus,
    MissedExecutionPolicy,
)
from src.domain.scheduling.ports import ScheduleRepository


class JsonScheduleRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de scheduling."""
    pass


class CorruptedScheduleDataError(JsonScheduleRepositoryError):
    """Se lanza cuando los datos de un schedule u occurrence están corruptos."""
    pass


def _encode_json_value(val: Any) -> Any:
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, dict):
        return {str(k): _encode_json_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonScheduleRepository(ScheduleRepository):
    """
    Implementación en archivos JSON del puerto ScheduleRepository.
    Ofrece persistencia durable y atómica para Schedules y Occurrences.
    """

    def __init__(self, storage_dir: Union[str, Path]):
        self.storage_dir = Path(storage_dir)
        self._schedules_dir = self.storage_dir / "schedules"
        self._occurrences_dir = self.storage_dir / "occurrences"

        self._schedules_dir.mkdir(parents=True, exist_ok=True)
        self._occurrences_dir.mkdir(parents=True, exist_ok=True)

    def save(self, schedule: Schedule) -> None:
        file_path = self._schedules_dir / f"{schedule.schedule_id}.json"

        data = {
            "schedule_id": schedule.schedule_id,
            "mission_type": schedule.mission_type.value,
            "mission_parameters": _encode_json_value(schedule.mission_parameters),
            "schedule_type": schedule.schedule_type.value,
            "config": {
                "interval_seconds": schedule.config.interval_seconds,
                "cron_expression": schedule.config.cron_expression,
                "start_time": schedule.config.start_time.isoformat() if schedule.config.start_time else None,
                "end_time": schedule.config.end_time.isoformat() if schedule.config.end_time else None,
                "timezone_str": schedule.config.timezone_str,
                "missed_policy": schedule.config.missed_policy.value,
                "max_occurrences": schedule.config.max_occurrences,
            },
            "status": schedule.status.value,
            "priority": schedule.priority.value,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
            "total_runs": schedule.total_runs,
            "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
            "updated_at": schedule.updated_at.isoformat() if schedule.updated_at else None,
            "correlation_id": schedule.correlation_id,
            "provenance": schedule.provenance,
        }

        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        temp_path.replace(file_path)

    def get_by_id(self, schedule_id: str) -> Optional[Schedule]:
        file_path = self._schedules_dir / f"{schedule_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            cfg_dict = data.get("config", {})
            config = ScheduleConfig(
                interval_seconds=cfg_dict.get("interval_seconds"),
                cron_expression=cfg_dict.get("cron_expression"),
                start_time=datetime.fromisoformat(cfg_dict["start_time"]) if cfg_dict.get("start_time") else None,
                end_time=datetime.fromisoformat(cfg_dict["end_time"]) if cfg_dict.get("end_time") else None,
                timezone_str=cfg_dict.get("timezone_str", "UTC"),
                missed_policy=MissedExecutionPolicy(cfg_dict.get("missed_policy", "SKIP")),
                max_occurrences=cfg_dict.get("max_occurrences"),
            )

            return Schedule(
                schedule_id=data["schedule_id"],
                mission_type=MissionType(data["mission_type"]),
                mission_parameters=data.get("mission_parameters", {}),
                schedule_type=ScheduleType(data.get("schedule_type", "INTERVAL")),
                config=config,
                status=ScheduleStatus(data["status"]),
                priority=MissionPriority(data.get("priority", "MEDIUM")),
                next_run_at=datetime.fromisoformat(data["next_run_at"]) if data.get("next_run_at") else None,
                last_run_at=datetime.fromisoformat(data["last_run_at"]) if data.get("last_run_at") else None,
                total_runs=data.get("total_runs", 0),
                created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
                updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None,
                correlation_id=data.get("correlation_id"),
                provenance=data.get("provenance"),
            )
        except Exception as e:
            raise CorruptedScheduleDataError(f"Error loading schedule {schedule_id}: {e}") from e

    def list_all(self) -> List[Schedule]:
        schedules = []
        for file_path in self._schedules_dir.glob("*.json"):
            sched = self.get_by_id(file_path.stem)
            if sched:
                schedules.append(sched)
        return schedules

    def list_due(self, current_time: datetime) -> List[Schedule]:
        all_schedules = self.list_all()
        return [s for s in all_schedules if s.is_due(current_time)]

    def delete(self, schedule_id: str) -> bool:
        file_path = self._schedules_dir / f"{schedule_id}.json"
        if file_path.exists():
            file_path.unlink()
            return True
        return False

    def save_occurrence(self, occurrence: ScheduleOccurrence) -> None:
        file_path = self._occurrences_dir / f"{occurrence.occurrence_id}.json"

        data = {
            "occurrence_id": occurrence.occurrence_id,
            "schedule_id": occurrence.schedule_id,
            "scheduled_at": occurrence.scheduled_at.isoformat(),
            "idempotency_key": occurrence.idempotency_key,
            "triggered_at": occurrence.triggered_at.isoformat() if occurrence.triggered_at else None,
            "mission_id": occurrence.mission_id,
            "status": occurrence.status.value,
            "result_summary": _encode_json_value(occurrence.result_summary),
            "error": occurrence.error,
        }

        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        temp_path.replace(file_path)

    def get_occurrence(self, occurrence_id: str) -> Optional[ScheduleOccurrence]:
        file_path = self._occurrences_dir / f"{occurrence_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            return ScheduleOccurrence(
                occurrence_id=data["occurrence_id"],
                schedule_id=data["schedule_id"],
                scheduled_at=datetime.fromisoformat(data["scheduled_at"]),
                idempotency_key=data["idempotency_key"],
                triggered_at=datetime.fromisoformat(data["triggered_at"]) if data.get("triggered_at") else None,
                mission_id=data.get("mission_id"),
                status=ExecutionStatus(data["status"]),
                result_summary=data.get("result_summary"),
                error=data.get("error"),
            )
        except Exception as e:
            raise CorruptedScheduleDataError(f"Error loading occurrence {occurrence_id}: {e}") from e

    def get_occurrence_by_idempotency_key(self, idempotency_key: str) -> Optional[ScheduleOccurrence]:
        for occ in self.list_occurrences():
            if occ.idempotency_key == idempotency_key:
                return occ
        return None

    def list_occurrences(self, schedule_id: Optional[str] = None) -> List[ScheduleOccurrence]:
        occurrences = []
        for file_path in self._occurrences_dir.glob("*.json"):
            occ = self.get_occurrence(file_path.stem)
            if occ:
                if schedule_id is None or occ.schedule_id == schedule_id:
                    occurrences.append(occ)
        return occurrences
