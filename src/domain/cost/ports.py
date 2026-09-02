"""
Puertos de dominio para el Registro y Medición de Costes Operacionales (Cost Tracking - Hito K.3).

Define los contratos abstractos para:
- PricingCatalogPort: Consulta determinista de tarifas de modelos, herramientas y APIs.
- CostRepositoryPort: Almacenamiento persistente, atómico, append-only e idempotente de CostRecord.
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from datetime import datetime
from decimal import Decimal

from src.domain.cost.models import (
    CostRecord,
    CostSummary,
    PricingRate,
    CostType,
    UsageUnit,
)


class PricingCatalogPort(ABC):
    """
    Puerto primario/secundario para consultar tarifas de consumo de forma desacoplada y versionada.
    """

    @abstractmethod
    def get_rate(
        self,
        provider: str,
        service_or_model: str,
        at_time: Optional[datetime] = None,
        cost_type: Optional[CostType] = None,
    ) -> Optional[PricingRate]:
        """
        Obtiene la tarifa aplicable para un proveedor y modelo/servicio en un instante de tiempo determinado.
        Si no se encuentra tarifa configurada, retorna None (se preserva como UNKNOWN).
        """
        pass


class CostRepositoryPort(ABC):
    """
    Puerto secundario de persistencia para registros de costes operacionales (Append-only e idempotente).
    """

    @abstractmethod
    def append(self, record: CostRecord) -> CostRecord:
        """
        Persiste un CostRecord de forma atómica e inmutable.
        Si ya existe un registro con el mismo cost_id o idempotency_key, retorna el existente
        garantizando idempotencia de replay.
        """
        pass

    @abstractmethod
    def get_by_id(self, cost_id: str) -> Optional[CostRecord]:
        """Obtiene un CostRecord por su identificador único."""
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[CostRecord]:
        """Obtiene un CostRecord por su clave determinista de idempotencia."""
        pass

    @abstractmethod
    def list_records(
        self,
        execution_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        provider: Optional[str] = None,
        cost_type: Optional[CostType] = None,
        currency: Optional[str] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[CostRecord]:
        """
        Consulta registros de costes con filtros opcionales ordenados determinísticamente por occurred_at y cost_id.
        """
        pass

    @abstractmethod
    def get_summary(
        self,
        mission_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
    ) -> CostSummary:
        """
        Calcula y reconstruye el resumen inmutable CostSummary para una misión, ejecución o ciclo.
        """
        pass
