from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Mapping, Any

from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.product_memory.models import ProductMemoryRecord
from src.domain.product_memory.ports import ProductMemoryRepository


class ProductMemoryService:
    """
    Servicio de Aplicación para gestionar la memoria contextual y trazabilidad de Productos / Listings.
    """

    def __init__(self, product_memory_repo: ProductMemoryRepository):
        self.product_memory_repo = product_memory_repo

    def record_product_memory(
        self,
        product_memory_id: str,
        sku: str,
        external_id: str,
        marketplace: Marketplace,
        title: str,
        category: str,
        price_amount: Decimal,
        price_currency: str = "CLP",
        sold_quantity: Optional[int] = None,
        available_quantity: int = 0,
        seller_id: str = "UNKNOWN",
        evidence_reference: Optional[str] = None,
        confidence: Confidence = Confidence.MEDIUM,
        provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ProductMemoryRecord:
        now = datetime.now(timezone.utc)
        record = ProductMemoryRecord(
            product_memory_id=product_memory_id,
            sku=sku,
            external_id=external_id,
            marketplace=marketplace,
            title=title,
            category=category,
            price_amount=price_amount,
            price_currency=price_currency,
            sold_quantity=sold_quantity,
            available_quantity=available_quantity,
            seller_id=seller_id,
            evidence_reference=evidence_reference,
            observed_at=now,
            updated_at=now,
            confidence=confidence,
            provenance=provenance,
            metadata=metadata or {},
        )
        self.product_memory_repo.save(record)
        return record

    def get_product_memory_by_id(self, product_memory_id: str) -> Optional[ProductMemoryRecord]:
        return self.product_memory_repo.get_by_id(product_memory_id)

    def get_product_memory_by_sku(self, sku: str) -> Optional[ProductMemoryRecord]:
        return self.product_memory_repo.get_by_sku(sku)

    def get_product_memories_by_external_id(self, external_id: str) -> List[ProductMemoryRecord]:
        return self.product_memory_repo.get_by_external_id(external_id)
