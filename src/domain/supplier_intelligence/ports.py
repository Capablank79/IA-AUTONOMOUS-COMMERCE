from typing import Protocol, Optional, Sequence, List
from .models import (
    SupplierData,
    SupplierEvidence,
    Supplier,
    SupplierCandidate,
    SupplierProductReference,
)


class SupplierDataSource(Protocol):
    def get_supplier_data(self, supplier_id: str) -> Optional[SupplierData]:
        """Obtiene los datos base del proveedor desde la fuente de verdad."""
        ...

    def get_supplier_evidence(self, supplier_id: str, sku: str) -> Optional[SupplierEvidence]:
        """Obtiene la evidencia de un producto específico para un proveedor."""
        ...


class SupplierSource(Protocol):
    """
    Puerto desacoplado para consultar un origen de proveedores (local, API, directorio, etc.)
    sin depender de HTTP ni de implementaciones concretas.
    """
    @property
    def source_name(self) -> str:
        ...

    def search_suppliers(
        self,
        query: str,
        brand: Optional[str] = None,
        model: Optional[str] = None,
        sku: Optional[str] = None,
        limit: int = 10,
    ) -> Sequence[SupplierCandidate]:
        """Busca candidatos a proveedor según criterios del producto."""
        ...


class SupplierRepository(Protocol):
    """
    Puerto para persistencia y consulta de entidades Supplier y SupplierEvidence.
    """
    def save_supplier(self, supplier: Supplier) -> None:
        ...

    def get_supplier(self, supplier_id: str) -> Optional[Supplier]:
        ...

    def save_evidence(self, evidence: SupplierEvidence) -> None:
        ...

    def get_evidence(self, supplier_id: str, sku: str) -> Optional[SupplierEvidence]:
        ...

    def list_suppliers(self, limit: int = 100) -> Sequence[Supplier]:
        ...
