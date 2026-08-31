from abc import ABC, abstractmethod
from typing import Optional, List
from .models import ProductMemoryRecord


class ProductMemoryRepository(ABC):
    """
    Puerto secundario (interface de persistencia) para guardar y consultar memoria de productos.
    """

    @abstractmethod
    def save(self, record: ProductMemoryRecord) -> None:
        """
        Guarda o actualiza un registro de memoria de producto.
        """
        pass

    @abstractmethod
    def get_by_id(self, product_memory_id: str) -> Optional[ProductMemoryRecord]:
        """
        Recupera un registro por su ID de memoria.
        """
        pass

    @abstractmethod
    def get_by_sku(self, sku: str) -> Optional[ProductMemoryRecord]:
        """
        Recupera un registro por su SKU comercial.
        """
        pass

    @abstractmethod
    def get_by_external_id(self, external_id: str) -> List[ProductMemoryRecord]:
        """
        Recupera registros asociados a un ID externo de marketplace (e.g. ML-12345).
        """
        pass

    @abstractmethod
    def exists(self, product_memory_id: str) -> bool:
        """
        Verifica si existe el registro.
        """
        pass
