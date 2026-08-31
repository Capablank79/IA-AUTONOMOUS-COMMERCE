from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Tuple, Mapping, Any
from types import MappingProxyType

from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class ActionStatus(str, Enum):
    """
    Estado del ciclo de vida de una Acción.
    """
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ActionRecord:
    """
    Registro de dominio inmutable para la persistencia y seguimiento de Acciones.
    Vinculado estrechamente con Mission y Decision.
    """
    action_id: str
    decision_id: str
    mission_id: str
    action_type: str
    status: ActionStatus = ActionStatus.PENDING
    target_resource: Optional[str] = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = "default-correlation"
    idempotency_key: str = "default-idempotency"
    version: int = 1
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    policy_reference: Optional[str] = None
    approval_reference: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.action_id or not isinstance(self.action_id, str):
            raise ValueError("ActionRecord.action_id must be a non-empty string")
        if not self.decision_id or not isinstance(self.decision_id, str):
            raise ValueError("ActionRecord.decision_id must be a non-empty string")
        if not self.mission_id or not isinstance(self.mission_id, str):
            raise ValueError("ActionRecord.mission_id must be a non-empty string")
        if not self.action_type or not isinstance(self.action_type, str):
            raise ValueError("ActionRecord.action_type must be a non-empty string")

        if not isinstance(self.parameters, MappingProxyType):
            object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
