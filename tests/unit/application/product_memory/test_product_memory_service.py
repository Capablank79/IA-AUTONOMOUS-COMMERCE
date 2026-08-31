import pytest
import tempfile
from pathlib import Path
from decimal import Decimal

from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.product_memory.models import ProductMemoryRecord
from src.infrastructure.persistence.data.json.product_memory_repository import (
    JsonProductMemoryRepository,
    InvalidProductMemoryDataError,
)
from src.application.product_memory.product_memory_service import ProductMemoryService


def test_product_memory_record_immutability():
    rec = ProductMemoryRecord(
        product_memory_id="pm-1",
        sku="SKU-AUDIO-01",
        external_id="MLC-123456",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Audífonos Bluetooth Pro",
        category="Audio",
        price_amount=Decimal("29990"),
    )
    assert rec.sku == "SKU-AUDIO-01"
    assert rec.price_amount == Decimal("29990")

    with pytest.raises(Exception):
        rec.sku = "SKU-AUDIO-02"


def test_json_product_memory_repository_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "product_memory.json"
        repo = JsonProductMemoryRepository(file_path)

        rec = ProductMemoryRecord(
            product_memory_id="pm-100",
            sku="SKU-TEST-100",
            external_id="MLC-999888",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Teclado Mecánico RGB",
            category="Computación",
            price_amount=Decimal("45990"),
            sold_quantity=15,
            available_quantity=50,
            seller_id="SELLER-55",
            evidence_reference="ev-prod-100",
            metadata={"secret_token": "SENSITIVE_DATA", "normal_key": "val"},
        )

        repo.save(rec)
        assert repo.exists("pm-100") is True

        retrieved = repo.get_by_id("pm-100")
        assert retrieved is not None
        assert retrieved.sku == "SKU-TEST-100"
        assert retrieved.price_amount == Decimal("45990")
        assert retrieved.evidence_reference == "ev-prod-100"
        assert "secret_token" not in retrieved.metadata

        # SKU lookup
        by_sku = repo.get_by_sku("SKU-TEST-100")
        assert by_sku is not None
        assert by_sku.product_memory_id == "pm-100"

        # External ID lookup
        by_ext = repo.get_by_external_id("MLC-999888")
        assert len(by_ext) == 1
        assert by_ext[0].product_memory_id == "pm-100"


def test_product_memory_service_usage():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "product_memory.json"
        repo = JsonProductMemoryRepository(file_path)
        service = ProductMemoryService(repo)

        rec = service.record_product_memory(
            product_memory_id="pm-200",
            sku="SKU-GAMER-200",
            external_id="MLC-777",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Mouse Gamer",
            category="Computación",
            price_amount=Decimal("19990"),
            confidence=Confidence.HIGH,
            provenance=EvidenceProvenanceType.LIVE,
        )
        assert rec.product_memory_id == "pm-200"

        # Reload from new repo
        fresh_repo = JsonProductMemoryRepository(file_path)
        loaded = fresh_repo.get_by_sku("SKU-GAMER-200")
        assert loaded is not None
        assert loaded.title == "Mouse Gamer"
        assert loaded.provenance == EvidenceProvenanceType.LIVE


def test_corrupted_product_memory_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "corrupt_pm.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("{ invalid json }")

        repo = JsonProductMemoryRepository(file_path)
        with pytest.raises(InvalidProductMemoryDataError):
            repo.get_by_id("pm-1")
