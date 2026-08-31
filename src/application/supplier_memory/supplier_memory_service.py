from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Mapping, Any

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import (
    SupplierStatus,
    EvidenceProvenanceType,
    SupplierReadiness,
)
from src.domain.supplier_memory.models import SupplierMemoryRecord
from src.domain.supplier_memory.ports import SupplierMemoryRepository


class SupplierMemoryService:
    """
    Servicio de Aplicación para gestionar la memoria contextual y cotizaciones de Proveedores.
    """

    def __init__(self, supplier_memory_repo: SupplierMemoryRepository):
        self.supplier_memory_repo = supplier_memory_repo

    def record_supplier_memory(
        self,
        supplier_memory_id: str,
        supplier_id: str,
        name: str,
        status: SupplierStatus = SupplierStatus.RESEARCH,
        sku: Optional[str] = None,
        cost_amount: Optional[Decimal] = None,
        cost_currency: str = "CLP",
        moq: Optional[int] = None,
        lead_time_days: Optional[int] = None,
        source: str = "SUPPLIER_DIRECTORY",
        evidence_reference: Optional[str] = None,
        verification_status: SupplierReadiness = SupplierReadiness.DISCOVERED,
        confidence: Confidence = Confidence.MEDIUM,
        provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> SupplierMemoryRecord:
        now = datetime.now(timezone.utc)
        record = SupplierMemoryRecord(
            supplier_memory_id=supplier_memory_id,
            supplier_id=supplier_id,
            name=name,
            status=status,
            sku=sku,
            cost_amount=cost_amount,
            cost_currency=cost_currency,
            moq=moq,
            lead_time_days=lead_time_days,
            source=source,
            evidence_reference=evidence_reference,
            verification_status=verification_status,
            confidence=confidence,
            provenance=provenance,
            observed_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        self.supplier_memory_repo.save(record)
        return record

    def get_supplier_memory_by_id(self, supplier_memory_id: str) -> Optional[SupplierMemoryRecord]:
        return self.supplier_memory_repo.get_by_id(supplier_memory_id)

    def get_supplier_memories_by_supplier_id(self, supplier_id: str) -> List[SupplierMemoryRecord]:
        return self.supplier_memory_repo.get_by_supplier_id(supplier_id)

    def get_supplier_memories_by_sku(self, sku: str) -> List[SupplierMemoryRecord]:
        return self.supplier_memory_repo.get_by_sku(sku)
