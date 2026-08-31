import pytest
import tempfile
from pathlib import Path
from decimal import Decimal

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import (
    SupplierStatus,
    EvidenceProvenanceType,
    SupplierReadiness,
)
from src.domain.supplier_memory.models import SupplierMemoryRecord
from src.infrastructure.persistence.data.json.supplier_memory_repository import (
    JsonSupplierMemoryRepository,
    InvalidSupplierMemoryDataError,
)
from src.application.supplier_memory.supplier_memory_service import SupplierMemoryService


def test_supplier_memory_record_immutability():
    rec = SupplierMemoryRecord(
        supplier_memory_id="sm-1",
        supplier_id="SUP-AUDIO-01",
        name="Global Tech Imports",
        status=SupplierStatus.VERIFIED,
        cost_amount=Decimal("12500"),
    )
    assert rec.supplier_id == "SUP-AUDIO-01"
    assert rec.cost_amount == Decimal("12500")

    with pytest.raises(Exception):
        rec.name = "New Name"


def test_json_supplier_memory_repository_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "supplier_memory.json"
        repo = JsonSupplierMemoryRepository(file_path)

        rec = SupplierMemoryRecord(
            supplier_memory_id="sm-100",
            supplier_id="SUP-100",
            name="Shenzhen Electronics Co",
            status=SupplierStatus.ACTIVE,
            sku="SKU-KEYBOARD-1",
            cost_amount=Decimal("15000"),
            moq=100,
            lead_time_days=14,
            evidence_reference="ev-supp-100",
            metadata={"secret_credential": "MUST_BE_EXCLUDED", "valid_meta": "yes"},
        )

        repo.save(rec)
        assert repo.exists("sm-100") is True

        retrieved = repo.get_by_id("sm-100")
        assert retrieved is not None
        assert retrieved.supplier_id == "SUP-100"
        assert retrieved.cost_amount == Decimal("15000")
        assert retrieved.moq == 100
        assert retrieved.lead_time_days == 14
        assert "secret_credential" not in retrieved.metadata

        # Supplier ID lookup
        by_sup = repo.get_by_supplier_id("SUP-100")
        assert len(by_sup) == 1
        assert by_sup[0].supplier_memory_id == "sm-100"

        # SKU lookup
        by_sku = repo.get_by_sku("SKU-KEYBOARD-1")
        assert len(by_sku) == 1
        assert by_sku[0].supplier_memory_id == "sm-100"


def test_supplier_memory_service_usage():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "supplier_memory.json"
        repo = JsonSupplierMemoryRepository(file_path)
        service = SupplierMemoryService(repo)

        rec = service.record_supplier_memory(
            supplier_memory_id="sm-200",
            supplier_id="SUP-200",
            name="Santiago Wholesale",
            status=SupplierStatus.ACTIVE,
            sku="SKU-MOUSE-200",
            cost_amount=Decimal("8000"),
            verification_status=SupplierReadiness.EVALUATED,
            confidence=Confidence.HIGH,
            provenance=EvidenceProvenanceType.LIVE,
        )
        assert rec.supplier_memory_id == "sm-200"

        # Reload from new repo
        fresh_repo = JsonSupplierMemoryRepository(file_path)
        loaded = fresh_repo.get_by_id("sm-200")
        assert loaded is not None
        assert loaded.name == "Santiago Wholesale"
        assert loaded.provenance == EvidenceProvenanceType.LIVE


def test_corrupted_supplier_memory_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "corrupt_sm.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("{ corrupt json }")

        repo = JsonSupplierMemoryRepository(file_path)
        with pytest.raises(InvalidSupplierMemoryDataError):
            repo.get_by_id("sm-1")
