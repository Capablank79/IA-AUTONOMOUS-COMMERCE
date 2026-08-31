import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

from src.domain.temporal_state.models import TemporalSnapshot
from src.infrastructure.persistence.data.json.temporal_state_repository import (
    JsonTemporalStateRepository,
    InvalidTemporalSnapshotDataError,
)
from src.application.temporal_state.temporal_state_service import TemporalStateService


def test_temporal_snapshot_immutability():
    ts = datetime.now(timezone.utc)
    snap = TemporalSnapshot(
        snapshot_id="snap-1",
        entity_type="PRODUCT_LISTING",
        entity_id="MLC-100",
        timestamp=ts,
        state_payload={"price": 1000, "status": "ACTIVE"},
    )
    assert snap.snapshot_id == "snap-1"
    assert snap.state_payload["price"] == 1000

    with pytest.raises(Exception):
        snap.entity_type = "ORDER"


def test_temporal_state_chronological_ordering_and_reconstruction():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "temporal_state.json"
        repo = JsonTemporalStateRepository(file_path)
        service = TemporalStateService(repo)

        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 3, 12, 0, 0, tzinfo=timezone.utc)

        # Record t1 first, then t0 (out of order insertion)
        service.record_snapshot(
            snapshot_id="snap-t1",
            entity_type="SUPPLIER_QUOTE",
            entity_id="SUP-99",
            state_payload={"cost": 5000, "lead_time": 10},
            timestamp=t1,
        )

        service.record_snapshot(
            snapshot_id="snap-t0",
            entity_type="SUPPLIER_QUOTE",
            entity_id="SUP-99",
            state_payload={"cost": 6000, "lead_time": 15},
            timestamp=t0,
        )

        service.record_snapshot(
            snapshot_id="snap-t2",
            entity_type="SUPPLIER_QUOTE",
            entity_id="SUP-99",
            state_payload={"cost": 4500, "lead_time": 7},
            timestamp=t2,
        )

        # Check chronological ordering
        history = service.get_history("SUPPLIER_QUOTE", "SUP-99")
        assert len(history) == 3
        assert history[0].snapshot_id == "snap-t0"
        assert history[1].snapshot_id == "snap-t1"
        assert history[2].snapshot_id == "snap-t2"

        # Reconstruct state at T1.5 (between t1 and t2) -> Should return snap-t1
        t1_5 = datetime(2026, 1, 2, 18, 0, 0, tzinfo=timezone.utc)
        snap_t1_5 = service.reconstruct_state_at("SUPPLIER_QUOTE", "SUP-99", t1_5)
        assert snap_t1_5 is not None
        assert snap_t1_5.snapshot_id == "snap-t1"
        assert snap_t1_5.state_payload["cost"] == 5000

        # Reconstruct state before T0 -> Should return None
        t_before = datetime(2025, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
        assert service.reconstruct_state_at("SUPPLIER_QUOTE", "SUP-99", t_before) is None


def test_corrupted_temporal_state_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "corrupt_temp.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("{ invalid json }")

        repo = JsonTemporalStateRepository(file_path)
        with pytest.raises(InvalidTemporalSnapshotDataError):
            repo.get_snapshot_by_id("snap-1")
