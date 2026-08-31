from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Mapping, Any
from types import MappingProxyType


@dataclass(frozen=True)
class TemporalSnapshot:
    """
    Representa una foto de estado inmutable tomada en un instante de tiempo T.
    Permite diferenciar CURRENT STATE de TEMPORAL/HISTORICAL STATE.
    """
    snapshot_id: str
    entity_type: str
    entity_id: str
    timestamp: datetime
    state_payload: Mapping[str, Any]
    correlation_id: str = "default-correlation"
    provenance: str = "DERIVED"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.snapshot_id or not isinstance(self.snapshot_id, str):
            raise ValueError("TemporalSnapshot.snapshot_id must be a non-empty string")
        if not self.entity_type or not isinstance(self.entity_type, str):
            raise ValueError("TemporalSnapshot.entity_type must be a non-empty string")
        if not self.entity_id or not isinstance(self.entity_id, str):
            raise ValueError("TemporalSnapshot.entity_id must be a non-empty string")

        if not isinstance(self.state_payload, MappingProxyType):
            object.__setattr__(self, "state_payload", MappingProxyType(dict(self.state_payload)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
