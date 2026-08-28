from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Any, Optional, Tuple, Mapping
from types import MappingProxyType
import uuid

class MissionType(str, Enum):
    MARKET_DISCOVERY = "MARKET_DISCOVERY"
    SUPPLIER_SEARCH = "SUPPLIER_SEARCH"
    FULL_OPPORTUNITY_ANALYSIS = "FULL_OPPORTUNITY_ANALYSIS"

class MissionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"

class MissionPriority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class LoopAction(str, Enum):
    CONTINUE = "CONTINUE"
    PIVOT = "PIVOT"
    REJECT = "REJECT"
    PROMOTE = "PROMOTE"
    COMPLETE = "COMPLETE"

@dataclass(frozen=True)
class LoopDecision:
    action: LoopAction
    reason: str
    target: Optional[str] = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None

    def __post_init__(self):
        if not isinstance(self.parameters, MappingProxyType):
            object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

@dataclass(frozen=True)
class LoopState:
    mission_id: str
    iteration: int
    goal: str
    current_target: Optional[str] = None
    observations: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    evidences: Tuple[Any, ...] = field(default_factory=tuple)
    decision_history: Tuple[LoopDecision, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.observations, tuple):
            frozen_obs = tuple(
                MappingProxyType(obs) if isinstance(obs, dict) and not isinstance(obs, MappingProxyType) else obs
                for obs in self.observations
            )
            object.__setattr__(self, "observations", frozen_obs)
        else:
            frozen_obs = tuple(
                MappingProxyType(obs) if isinstance(obs, dict) and not isinstance(obs, MappingProxyType) else obs
                for obs in self.observations
            )
            object.__setattr__(self, "observations", frozen_obs)

        if not isinstance(self.evidences, tuple):
            object.__setattr__(self, "evidences", tuple(self.evidences))

        if not isinstance(self.decision_history, tuple):
            object.__setattr__(self, "decision_history", tuple(self.decision_history))

@dataclass(frozen=True)
class LoopTraceEntry:
    iteration: int
    decision: LoopDecision
    reason: str
    action: LoopAction
    target: Optional[str]
    parameters: Mapping[str, Any]
    observation: Mapping[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        if not isinstance(self.parameters, MappingProxyType):
            object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        if not isinstance(self.observation, MappingProxyType):
            object.__setattr__(self, "observation", MappingProxyType(dict(self.observation)))

@dataclass(frozen=True)
class Mission:
    mission_id: str
    type: MissionType
    priority: MissionPriority = MissionPriority.MEDIUM
    status: MissionStatus = MissionStatus.PENDING
    parameters: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def create(cls, mission_type: MissionType, parameters: Dict[str, Any], priority: MissionPriority = MissionPriority.MEDIUM) -> 'Mission':
        now = datetime.utcnow()
        return cls(
            mission_id=str(uuid.uuid4()),
            type=mission_type,
            priority=priority,
            status=MissionStatus.PENDING,
            parameters=parameters,
            created_at=now,
            updated_at=now
        )

@dataclass(frozen=True)
class MissionTraceEntry:
    step: str
    status: MissionStatus
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class MissionResult:
    mission_id: str
    status: MissionStatus
    output: Dict[str, Any] = field(default_factory=dict)
    trace: List[MissionTraceEntry] = field(default_factory=list)
    evidences: List[Any] = field(default_factory=list)
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    finished_at: datetime = field(default_factory=datetime.utcnow)
