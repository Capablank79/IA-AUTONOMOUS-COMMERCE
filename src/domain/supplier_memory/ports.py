from abc import ABC, abstractmethod
from typing import Optional, List
from .models import SupplierMemoryRecord


class SupplierMemoryRepository(ABC):
    """
    Puerto secundario (interface de persistencia) para guardar y consultar memoria de proveedores.
    """

    @abstractmethod
    def save(self, record: SupplierMemoryRecord) -> None:
        """
        Guarda o actualiza un registro de memoria de proveedor.
        """
        pass

    @abstractmethod
    def get_by_id(self, supplier_memory_id: str) -> Optional[SupplierMemoryRecord]:
        """
        Recupera un registro por su ID de memoria.
        """
        pass

    @abstractmethod
    def get_by_supplier_id(self, supplier_id: str) -> List[SupplierMemoryRecord]:
        """
        Recupera registros asociados a un ID de proveedor.
        """
        pass

    @abstractmethod
    def get_by_sku(self, sku: str) -> List[SupplierMemoryRecord]:
        """
        Recupera cotizaciones/memorias de proveedor para un SKU específico.
        """
        pass

    @abstractmethod
    def exists(self, supplier_memory_id: str) -> bool:
        """
        Verifica si existe el registro.
        """
        pass
