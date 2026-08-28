from typing import Protocol, Optional
from .models import SupplierData, SupplierEvidence

class SupplierDataSource(Protocol):
    def get_supplier_data(self, supplier_id: str) -> Optional[SupplierData]:
        """Obtiene los datos base del proveedor desde la fuente de verdad."""
        ...

    def get_supplier_evidence(self, supplier_id: str, sku: str) -> Optional[SupplierEvidence]:
        """Obtiene la evidencia de un producto específico para un proveedor."""
        ...
