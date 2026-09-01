from abc import ABC, abstractmethod
from typing import List, Optional, Protocol, Tuple
from .models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    OpportunityDetectionCriteria,
)
from src.domain.market_monitoring.models import MarketObservation


class OpportunityDetectionEnginePort(Protocol):
    """
    Puerto de Dominio para el motor de detección y estructuración de oportunidades.
    Transforma MarketObservation(s) en OpportunityRecord deterministas.
    """
    def detect_opportunities(
        self,
        observations: List[MarketObservation],
        criteria: Optional[OpportunityDetectionCriteria] = None,
        correlation_id: Optional[str] = None,
    ) -> List[OpportunityRecord]:
        """
        Analiza observaciones de mercado y genera registros de oportunidad estructurados.
        UNKNOWN != 0 y no inventa datos. Si los datos son insuficientes o inválidos,
        no fabrica oportunidades falsas.
        """
        ...


class OpportunityRepositoryPort(Protocol):
    """
    Puerto de Dominio para la persistencia durable de registros de oportunidad.
    Garantiza atomicidad, idempotencia, sanitización de secretos y resiliencia ante reinicio.
    """
    def save(self, opportunity: OpportunityRecord) -> None:
        """Persiste una oportunidad de forma atómica e idempotente."""
        ...

    def save_all(self, opportunities: List[OpportunityRecord]) -> int:
        """Persiste una lista de oportunidades y retorna la cantidad de nuevas oportunidades guardadas."""
        ...

    def get_by_id(self, opportunity_id: str) -> Optional[OpportunityRecord]:
        """Obtiene una oportunidad por su identificador primario."""
        ...

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[OpportunityRecord]:
        """Obtiene una oportunidad por su clave determinista de idempotencia."""
        ...

    def list_by_product(self, canonical_product_id: str, limit: int = 100) -> List[OpportunityRecord]:
        """Lista oportunidades para un producto específico ordenadas cronológicamente."""
        ...

    def list_by_type(self, opportunity_type: OpportunityType, limit: int = 100) -> List[OpportunityRecord]:
        """Lista oportunidades filtradas por tipo."""
        ...

    def list_by_status(self, status: OpportunityStatus, limit: int = 100) -> List[OpportunityRecord]:
        """Lista oportunidades filtradas por estado."""
        ...

    def list_all(self, limit: int = 1000) -> List[OpportunityRecord]:
        """Lista todas las oportunidades registradas."""
        ...
