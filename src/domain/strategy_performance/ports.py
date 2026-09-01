from abc import ABC, abstractmethod
from typing import Optional, List
from src.domain.strategy_performance.models import StrategyPerformanceRecord


class StrategyPerformanceRepositoryPort(ABC):
    """
    Puerto primario de repositorio para guardar y consultar StrategyPerformanceRecord.
    """

    @abstractmethod
    def save_performance(self, performance: StrategyPerformanceRecord) -> None:
        """Persiste un registro de performance de estrategia."""
        pass

    @abstractmethod
    def get_performance_by_id(self, performance_id: str) -> Optional[StrategyPerformanceRecord]:
        """Obtiene un registro por su ID único."""
        pass

    @abstractmethod
    def get_performances_by_strategy_id(self, strategy_id: str) -> List[StrategyPerformanceRecord]:
        """Obtiene todos los registros de performance asociados a un ID de estrategia."""
        pass

    @abstractmethod
    def get_performance_by_idempotency_key(self, idempotency_key: str) -> Optional[StrategyPerformanceRecord]:
        """Obtiene un registro por su clave de idempotencia."""
        pass

    @abstractmethod
    def list_all(self) -> List[StrategyPerformanceRecord]:
        """Lista todos los registros de performance de estrategia conservados."""
        pass
