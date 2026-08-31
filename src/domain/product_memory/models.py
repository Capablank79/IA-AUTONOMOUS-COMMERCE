from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Mapping, Any
from types import MappingProxyType

from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


@dataclass(frozen=True)
class ProductMemoryRecord:
    """
    Registro de dominio inmutable para la memoria contextual de Productos / Listings de Mercado.
    Reutiliza identificadores (sku, external_id) y preserva observaciones, precios y procedencia.
    """
    product_memory_id: str
    sku: str
    external_id: str
    marketplace: Marketplace
    title: str
    category: str
    price_amount: Decimal
    price_currency: str = "CLP"
    sold_quantity: Optional[int] = None
    available_quantity: int = 0
    seller_id: str = "UNKNOWN"
    evidence_reference: Optional[str] = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: Confidence = Confidence.MEDIUM
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.product_memory_id or not isinstance(self.product_memory_id, str):
            raise ValueError("ProductMemoryRecord.product_memory_id must be a non-empty string")
        if not self.sku or not isinstance(self.sku, str):
            raise ValueError("ProductMemoryRecord.sku must be a non-empty string")
        if not self.external_id or not isinstance(self.external_id, str):
            raise ValueError("ProductMemoryRecord.external_id must be a non-empty string")

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
