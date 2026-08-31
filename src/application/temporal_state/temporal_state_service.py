from datetime import datetime, timezone
from typing import Optional, List, Mapping, Any

from src.domain.temporal_state.models import TemporalSnapshot
from src.domain.temporal_state.ports import TemporalStateRepository


class TemporalStateService:
    """
    Servicio de Aplicación para gestionar la dimensión temporal y reconstrucción histórica del estado del negocio.
    """

    def __init__(self, temporal_repo: TemporalStateRepository):
        self.temporal_repo = temporal_repo

    def record_snapshot(
        self,
        snapshot_id: str,
        entity_type: str,
        entity_id: str,
        state_payload: Mapping[str, Any],
        timestamp: Optional[datetime] = None,
        correlation_id: str = "default-correlation",
        provenance: str = "DERIVED",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> TemporalSnapshot:
        ts = timestamp or datetime.now(timezone.utc)
        snapshot = TemporalSnapshot(
            snapshot_id=snapshot_id,
            entity_type=entity_type,
            entity_id=entity_id,
            timestamp=ts,
            state_payload=state_payload,
            correlation_id=correlation_id,
            provenance=provenance,
            metadata=metadata or {},
        )
        self.temporal_repo.save_snapshot(snapshot)
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> Optional[TemporalSnapshot]:
        return self.temporal_repo.get_snapshot_by_id(snapshot_id)

    def get_history(self, entity_type: str, entity_id: str) -> List[TemporalSnapshot]:
        return self.temporal_repo.get_history_for_entity(entity_type, entity_id)

    def reconstruct_state_at(self, entity_type: str, entity_id: str, timestamp: datetime) -> Optional[TemporalSnapshot]:
        return self.temporal_repo.get_state_at(entity_type, entity_id, timestamp)
