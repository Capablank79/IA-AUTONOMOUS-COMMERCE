from abc import ABC, abstractmethod
from typing import Optional, List
from src.domain.product_performance.models import ProductPerformanceRecord


class ProductPerformanceRepository(ABC):
    """
    Puerto primario del repositorio para guardar y consultar ProductPerformanceRecord.
    """

    @abstractmethod
    def save_performance(self, performance: ProductPerformanceRecord) -> None:
        pass

    @abstractmethod
    def get_performance_by_id(self, performance_id: str) -> Optional[ProductPerformanceRecord]:
        pass

    @abstractmethod
    def get_performances_by_product_id(self, product_id: str) -> List[ProductPerformanceRecord]:
        pass

    @abstractmethod
    def get_performances_by_sku(self, sku: str) -> List[ProductPerformanceRecord]:
        pass

    @abstractmethod
    def get_performance_by_idempotency_key(self, idempotency_key: str) -> Optional[ProductPerformanceRecord]:
        pass
