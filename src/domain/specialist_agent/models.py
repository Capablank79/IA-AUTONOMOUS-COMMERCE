from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from src.domain.security.models import deep_freeze, sanitize_security_data, validate_safe_identifier
from src.domain.tool.models import ToolContract


class AgentAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class AgentSelectionStatus(str, Enum):
    SELECTED = "SELECTED"
    NO_MATCH = "NO_MATCH"
    BLOCKED = "BLOCKED"


class AgentExecutionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    PARTIAL = "PARTIAL"


class AgentExecutionFailureType(str, Enum):
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    INVALID_INPUT = "INVALID_INPUT"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    POLICY_DENIED = "POLICY_DENIED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AgentCapabilityContract:
    input_contract: ToolContract
    output_contract: ToolContract

    def __post_init__(self):
        if not isinstance(self.input_contract, ToolContract):
            raise ValueError("input_contract must be a ToolContract")
        if not isinstance(self.output_contract, ToolContract):
            raise ValueError("output_contract must be a ToolContract")


@dataclass(frozen=True)
class AgentCapability:
    capability_id: str
    contract: AgentCapabilityContract
    action_types: Tuple[str, ...] = field(default_factory=tuple)
    tool_ids: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        validate_safe_identifier(self.capability_id, "capability_id")
        if not isinstance(self.contract, AgentCapabilityContract):
            raise ValueError("contract must be an AgentCapabilityContract")
        object.__setattr__(self, "action_types", tuple(self.action_types))
        object.__setattr__(self, "tool_ids", tuple(self.tool_ids))
        if not self.action_types:
            raise ValueError("AgentCapability requires at least one action_type")
        if len(set(self.action_types)) != len(self.action_types) or len(set(self.tool_ids)) != len(self.tool_ids):
            raise ValueError("Capability references cannot contain duplicates")


@dataclass(frozen=True)
class SpecialistAgentDefinition:
    agent_id: str
    tenant_id: str
    capabilities: Tuple[AgentCapability, ...]
    availability: AgentAvailability = AgentAvailability.UNKNOWN
    allowed_action_types: Tuple[str, ...] = field(default_factory=tuple)
    allowed_tool_ids: Tuple[str, ...] = field(default_factory=tuple)
    estimated_cost: Optional[Decimal] = None
    priority: int = 100
    policy_eligible: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    executor_key: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.agent_id, "agent_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if not self.executor_key:
            object.__setattr__(self, "executor_key", self.agent_id)
        validate_safe_identifier(self.executor_key, "executor_key")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "allowed_action_types", tuple(self.allowed_action_types))
        object.__setattr__(self, "allowed_tool_ids", tuple(self.allowed_tool_ids))
        if not self.capabilities:
            raise ValueError("SpecialistAgentDefinition requires capabilities")
        ids = [cap.capability_id for cap in self.capabilities]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate capability in specialist definition")
        if self.estimated_cost is not None and self.estimated_cost < Decimal("0"):
            raise ValueError("estimated_cost cannot be negative")
        if not isinstance(self.availability, AgentAvailability):
            object.__setattr__(self, "availability", AgentAvailability(self.availability))
        sanitized = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized))

    def get_capability(self, capability_id: str) -> Optional[AgentCapability]:
        return next((cap for cap in self.capabilities if cap.capability_id == capability_id), None)


@dataclass(frozen=True)
class AgentSelectionResult:
    status: AgentSelectionStatus
    capability_id: str
    agent_id: Optional[str] = None
    reason: str = ""
    estimated_cost: Optional[Decimal] = None
    is_cost_unknown: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.status, AgentSelectionStatus):
            object.__setattr__(self, "status", AgentSelectionStatus(self.status))
        if self.status == AgentSelectionStatus.SELECTED and not self.agent_id:
            raise ValueError("SELECTED requires agent_id")
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))


@dataclass(frozen=True)
class AgentExecutionContext:
    tenant_id: str
    mission_id: str
    capability_id: str
    action_type: str
    inputs: Mapping[str, Any]
    idempotency_key: str
    correlation_id: str
    tool_id: Optional[str] = None
    budget_remaining: Optional[Decimal] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.capability_id, "capability_id")
        if not self.action_type or not self.idempotency_key or not self.correlation_id:
            raise ValueError("action_type, idempotency_key and correlation_id are required")
        if self.budget_remaining is not None and self.budget_remaining < Decimal("0"):
            raise ValueError("budget_remaining cannot be negative")
        object.__setattr__(self, "inputs", deep_freeze(sanitize_security_data(dict(self.inputs))))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))


@dataclass(frozen=True)
class AgentExecutionResult:
    status: AgentExecutionStatus
    agent_id: Optional[str]
    capability_id: str
    outputs: Mapping[str, Any] = field(default_factory=dict)
    failure_type: Optional[AgentExecutionFailureType] = None
    failure_reason: Optional[str] = None
    cost_used: Optional[Decimal] = None
    tokens_used: Optional[int] = None
    idempotency_key: str = ""
    correlation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.status, AgentExecutionStatus):
            object.__setattr__(self, "status", AgentExecutionStatus(self.status))
        if self.failure_type is not None and not isinstance(self.failure_type, AgentExecutionFailureType):
            object.__setattr__(self, "failure_type", AgentExecutionFailureType(self.failure_type))
        if self.cost_used is not None and self.cost_used < Decimal("0"):
            raise ValueError("cost_used cannot be negative")
        if self.tokens_used is not None and self.tokens_used < 0:
            raise ValueError("tokens_used cannot be negative")
        object.__setattr__(self, "outputs", deep_freeze(sanitize_security_data(dict(self.outputs))))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(dict(self.metadata))))
