"""
Modelos de dominio para Misiones Continuas (Continuous Missions - Hito J.7).

Define las entidades inmutables y estructuras de dominio:
- ContinuousMissionStatus: Taxonomía canónica de estados de una misión continua.
- ContinuousCycleStatus: Estado de ejecución de un ciclo individual.
- StopConditionType: Tipos deterministas de condiciones de parada.
- ContinuousMissionStopCondition: Configuración declarativa de parada.
- ContinuousMissionCycle: Registro inmutable de un ciclo ejecutado o en curso.
- ContinuousMission: Entidad raíz inmutable de la misión continua.

Principios:
- Inmutabilidad estricta (`frozen=True`, MappingProxyType).
- Determinismo y trazabilidad causal completa (`correlation_id`, `idempotency_key`, `provenance`).
- Preservación de incertidumbre `UNKNOWN`.
- Cero almacenamiento de credenciales o secretos (Sanitización).
- Desacoplamiento total de frameworks, APIs externas o bucles infinitos.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Dict, List, Tuple
import hashlib
import uuid

from src.domain.mission.models import MissionType, MissionPriority, MissionStatus


class ContinuousMissionStatus(str, Enum):
    """
    Lifecycle canónico de una Misión Continua (J.7).
    Transiciones deterministas y explícitas:
    CREATED -> ACTIVE -> PAUSED -> ACTIVE -> STOPPED / COMPLETED / FAILED / UNKNOWN
    """
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ContinuousCycleStatus(str, Enum):
    """
    Estado de ejecución de un ciclo individual de la misión continua.
    """
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


class StopConditionType(str, Enum):
    """
    Taxonomía de condiciones de parada configurables.
    """
    MANUAL = "MANUAL"
    MAX_CYCLES = "MAX_CYCLES"
    MAX_CONSECUTIVE_FAILURES = "MAX_CONSECUTIVE_FAILURES"
    TERMINAL_GOAL_REACHED = "TERMINAL_GOAL_REACHED"
    SCHEDULE_DISABLED = "SCHEDULE_DISABLED"


@dataclass(frozen=True)
class ContinuousMissionStopCondition:
    """
    Condición de parada declarativa para la misión continua.
    """
    max_cycles: Optional[int] = None
    max_consecutive_failures: int = 3
    stop_on_unknown: bool = False
    custom_criteria: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.custom_criteria, MappingProxyType):
            object.__setattr__(
                self, "custom_criteria", MappingProxyType(dict(self.custom_criteria))
            )


@dataclass(frozen=True)
class ContinuousMissionCycle:
    """
    Registro inmutable de un ciclo individual ejecutado dentro de una misión continua.

    Conserva la identidad y trazabilidad:
    Continuous Mission -> Cycle -> Mission -> Decision -> Action -> Result -> Outcome -> Learning
    """
    cycle_id: str
    continuous_mission_id: str
    cycle_number: int
    scheduled_at: datetime
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: ContinuousCycleStatus = ContinuousCycleStatus.PENDING
    mission_id: Optional[str] = None
    occurrence_id: Optional[str] = None
    idempotency_key: str = ""
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    provenance: Optional[str] = None
    result_summary: Mapping[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.result_summary, MappingProxyType):
            object.__setattr__(
                self, "result_summary", MappingProxyType(dict(self.result_summary))
            )
        if not self.idempotency_key:
            generated_key = f"cmc_{self.continuous_mission_id}_cycle_{self.cycle_number}_{self.scheduled_at.isoformat()}"
            object.__setattr__(self, "idempotency_key", generated_key)


@dataclass(frozen=True)
class ContinuousMission:
    """
    Entidad de dominio inmutable para Misiones Continuas (Hito J.7).

    Coordina la operación periódica y gobernada a través de múltiples ciclos,
    reutilizando el Schedule (J.1), AutonomousLoop/Mission, PolicyEngine,
    ActionExecutor y Business Memory sin crear arquitecturas paralelas.
    """
    continuous_mission_id: str
    schedule_id: str
    mission_type: MissionType
    goal: str
    status: ContinuousMissionStatus = ContinuousMissionStatus.CREATED
    priority: MissionPriority = MissionPriority.MEDIUM
    mission_parameters: Mapping[str, Any] = field(default_factory=dict)
    stop_condition: ContinuousMissionStopCondition = field(default_factory=ContinuousMissionStopCondition)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    last_cycle_at: Optional[datetime] = None
    next_cycle_at: Optional[datetime] = None
    cycle_count: int = 0
    consecutive_failures: int = 0
    total_failures: int = 0
    last_result_status: Optional[str] = None
    last_cycle_id: Optional[str] = None
    last_mission_id: Optional[str] = None
    correlation_id: Optional[str] = None
    provenance: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.mission_parameters, MappingProxyType):
            object.__setattr__(
                self, "mission_parameters", MappingProxyType(dict(self.mission_parameters))
            )
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(
                self, "metadata", MappingProxyType(dict(self.metadata))
            )

    @classmethod
    def create(
        cls,
        schedule_id: str,
        mission_type: MissionType,
        goal: str,
        continuous_mission_id: Optional[str] = None,
        priority: MissionPriority = MissionPriority.MEDIUM,
        mission_parameters: Optional[Dict[str, Any]] = None,
        stop_condition: Optional[ContinuousMissionStopCondition] = None,
        correlation_id: Optional[str] = None,
        provenance: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[datetime] = None,
    ) -> 'ContinuousMission':
        now = created_at or datetime.now(timezone.utc)
        cm_id = continuous_mission_id or f"cm_{uuid.uuid4().hex[:12]}"
        corr_id = correlation_id or f"corr_cm_{uuid.uuid4().hex[:12]}"
        prov = provenance or "ContinuousMissionService"

        return cls(
            continuous_mission_id=cm_id,
            schedule_id=schedule_id,
            mission_type=mission_type,
            goal=goal,
            status=ContinuousMissionStatus.CREATED,
            priority=priority,
            mission_parameters=mission_parameters or {},
            stop_condition=stop_condition or ContinuousMissionStopCondition(),
            created_at=now,
            started_at=None,
            last_cycle_at=None,
            next_cycle_at=now,
            cycle_count=0,
            consecutive_failures=0,
            total_failures=0,
            last_result_status=None,
            last_cycle_id=None,
            last_mission_id=None,
            correlation_id=corr_id,
            provenance=prov,
            metadata=metadata or {},
        )
