"""
Exportación de modelos, puertos y algoritmos de Agent Coordination (Hito R.4).
"""

from src.domain.agent_coordination.models import (
    CoordinationStatus,
    CoordinationTaskStatus,
    CoordinationFailureType,
    MergeStrategy,
    CoordinationEventType,
    CoordinationPolicy,
    TaskClaim,
    AgentAssignment,
    AgentHandoff,
    MergeResult,
    SharedCoordinationContext,
    CoordinationTask,
    CoordinationSession,
)
from src.domain.agent_coordination.merger import (
    DeterministicResultMerger,
)
from src.domain.agent_coordination.ports import (
    CoordinationSessionRepositoryPort,
    AgentCoordinatorPort,
)

from src.domain.agent_coordination.delegation_models import (
    DelegationReason,
    DelegationPolicy,
    DelegationDecisionStatus,
    DelegationRequest,
    DelegationDecision,
    DelegationRecord,
)

__all__ = [
    "CoordinationStatus",
    "CoordinationTaskStatus",
    "CoordinationFailureType",
    "MergeStrategy",
    "CoordinationEventType",
    "CoordinationPolicy",
    "TaskClaim",
    "AgentAssignment",
    "AgentHandoff",
    "MergeResult",
    "SharedCoordinationContext",
    "CoordinationTask",
    "CoordinationSession",
    "DeterministicResultMerger",
    "CoordinationSessionRepositoryPort",
    "AgentCoordinatorPort",
    "DelegationReason",
    "DelegationPolicy",
    "DelegationDecisionStatus",
    "DelegationRequest",
    "DelegationDecision",
    "DelegationRecord",
]
