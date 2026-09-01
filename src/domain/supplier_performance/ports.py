from abc import ABC, abstractmethod
from typing import Optional, List
from src.domain.supplier_performance.models import SupplierPerformanceRecord


class SupplierPerformanceRepositoryPort(ABC):
    """
    Puerto primario/secundario para la persistencia Hexagonal de SupplierPerformanceRecord.
    El dominio no conoce JSON, SQL, Filesystem ni APIs externas.
    """

    @abstractmethod
    def save(self, record: SupplierPerformanceRecord) -> SupplierPerformanceRecord:
        """
        Guarda o actualiza de manera idempotente un registro de Supplier Performance.
        """
        pass

    @abstractmethod
    def get_by_id(self, performance_id: str) -> Optional[SupplierPerformanceRecord]:
        """
        Recupera un registro por su identificador único de performance.
        """
        pass

    @abstractmethod
    def get_by_supplier_id(self, supplier_id: str) -> List[SupplierPerformanceRecord]:
        """
        Recupera el historial de registros de desempeño asociados a un proveedor.
        """
        pass

    @abstractmethod
    def list_all(self) -> List[SupplierPerformanceRecord]:
        """
        Lista todos los registros de desempeño persistidos.
        """
        pass
