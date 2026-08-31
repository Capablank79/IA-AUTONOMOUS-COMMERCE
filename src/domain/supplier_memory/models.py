from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Mapping, Any
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import (
    SupplierStatus,
    EvidenceProvenanceType,
    SupplierReadiness,
)


@dataclass(frozen=True)
class SupplierMemoryRecord:
    """
    Registro de dominio inmutable para la memoria contextual y procedencia de Proveedores / Cotizaciones.
    Reutiliza identificadores de proveedor y cotización sin almacenar credenciales.
    """
    supplier_memory_id: str
    supplier_id: str
    name: str
    status: SupplierStatus
    sku: Optional[str] = None
    cost_amount: Optional[Decimal] = None
    cost_currency: str = "CLP"
    moq: Optional[int] = None
    lead_time_days: Optional[int] = None
    source: str = "SUPPLIER_DIRECTORY"
    evidence_reference: Optional[str] = None
    verification_status: SupplierReadiness = SupplierReadiness.DISCOVERED
    confidence: Confidence = Confidence.MEDIUM
    provenance: EvidenceProvenanceType = EvidenceProvenanceType.DERIVED
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.supplier_memory_id or not isinstance(self.supplier_memory_id, str):
            raise ValueError("SupplierMemoryRecord.supplier_memory_id must be a non-empty string")
        if not self.supplier_id or not isinstance(self.supplier_id, str):
            raise ValueError("SupplierMemoryRecord.supplier_id must be a non-empty string")

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
