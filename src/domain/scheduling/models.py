from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

from src.domain.mission.models import MissionType, MissionPriority


class Clock(ABC):
    """Puerto abstracto de proveedor de tiempo para determinismo y testing."""

    @abstractmethod
    def now(self) -> datetime:
        """Devuelve la fecha/hora actual (UTC por convención)."""
        pass


class SystemClock(Clock):
    """Implementación del reloj basada en el tiempo del sistema."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class DeterministicClock(Clock):
    """Implementación de reloj determinista con soporte para avance manual y timezones."""

    def __init__(self, initial_time: Optional[datetime] = None):
        if initial_time is None:
            self._current_time = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        else:
            if initial_time.tzinfo is None:
                self._current_time = initial_time.replace(tzinfo=timezone.utc)
            else:
                self._current_time = initial_time

    def now(self) -> datetime:
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        if new_time.tzinfo is None:
            self._current_time = new_time.replace(tzinfo=timezone.utc)
        else:
            self._current_time = new_time

    def advance(self, seconds: float) -> None:
        from datetime import timedelta
        self._current_time = self._current_time + timedelta(seconds=seconds)


class ScheduleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"
    COMPLETED = "COMPLETED"


class ScheduleType(str, Enum):
    INTERVAL = "INTERVAL"
    ONCE = "ONCE"
    CRON = "CRON"


class MissedExecutionPolicy(str, Enum):
    SKIP = "SKIP"
    CATCH_UP_ONE = "CATCH_UP_ONE"
    CATCH_UP_ALL = "CATCH_UP_ALL"


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ScheduleConfig:
    interval_seconds: Optional[int] = None
    cron_expression: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    timezone_str: str = "UTC"
    missed_policy: MissedExecutionPolicy = MissedExecutionPolicy.SKIP
    max_occurrences: Optional[int] = None


@dataclass(frozen=True)
class ScheduleOccurrence:
    occurrence_id: str
    schedule_id: str
    scheduled_at: datetime
    idempotency_key: str
    triggered_at: Optional[datetime] = None
    mission_id: Optional[str] = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    result_summary: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    mission_type: MissionType
    mission_parameters: Dict[str, Any] = field(default_factory=dict)
    schedule_type: ScheduleType = ScheduleType.INTERVAL
    config: ScheduleConfig = field(default_factory=ScheduleConfig)
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    priority: MissionPriority = MissionPriority.MEDIUM
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    total_runs: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    correlation_id: Optional[str] = None
    provenance: Optional[str] = None

    def is_due(self, current_time: datetime) -> bool:
        if self.status != ScheduleStatus.ACTIVE:
            return False
        if self.next_run_at is None:
            return False
        return self.next_run_at <= current_time

    def compute_next_run(self, current_time: datetime) -> Optional[datetime]:
        """Calcula deterministamente el próximo run."""
        from datetime import timedelta
        if self.status != ScheduleStatus.ACTIVE:
            return None
        if self.config.max_occurrences is not None and self.total_runs >= self.config.max_occurrences:
            return None
        if self.schedule_type == ScheduleType.ONCE:
            if self.total_runs > 0:
                return None
            return self.next_run_at or self.config.start_time or current_time
        elif self.schedule_type == ScheduleType.INTERVAL:
            interval = self.config.interval_seconds or 60
            base = self.next_run_at or current_time
            # Si base está en el pasado lejano y la política es SKIP, avanzamos hasta el futuro inmediato
            if self.config.missed_policy == MissedExecutionPolicy.SKIP and base < current_time:
                # Calcular cuántos intervalos han pasado
                delta_sec = (current_time - base).total_seconds()
                steps = int(delta_sec // interval) + 1
                return base + timedelta(seconds=steps * interval)
            return base + timedelta(seconds=interval)
        return None
