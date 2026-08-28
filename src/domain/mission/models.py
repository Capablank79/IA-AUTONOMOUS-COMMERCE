from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Any, Optional
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
