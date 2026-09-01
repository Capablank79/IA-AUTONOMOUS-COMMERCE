from abc import ABC, abstractmethod
from typing import List, Optional, Protocol
from .models import MarketObservation


class MarketObservationSourcePort(Protocol):
    """
    Puerto de Dominio para fuentes de observación de mercado.
    Permite desacoplar adaptadores (Mercado Libre, Amazon, Scrapers, Fixtures)
    del motor de monitoreo.
    """
    @property
    def source_name(self) -> str:
        """Nombre canónico de la fuente (ej. MERCADOLIBRE_LIVE, MOCK_SOURCE)."""
        ...

    def observe(
        self,
        query: Optional[str] = None,
        entity_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 50,
        correlation_id: Optional[str] = None,
    ) -> List[MarketObservation]:
        """
        Ejecuta la observación de mercado sobre una entidad, query o categoría.
        Devuelve una lista de MarketObservation estructuradas y normalizadas.
        En caso de fallo de red/API externa, no inventa datos y devuelve observaciones
        con status SOURCE_FAILURE o TIMEOUT.
        """
        ...


class MarketObservationRepository(Protocol):
    """
    Puerto de Dominio para persistencia durable de MarketObservation.
    Garantiza idempotencia, atomicidad y soporte de reinicio.
    """
    def save(self, observation: MarketObservation) -> None:
        """Persiste una observación de forma atómica e idempotente."""
        ...

    def save_all(self, observations: List[MarketObservation]) -> int:
        """Persiste un conjunto de observaciones. Devuelve el número de nuevas observaciones guardadas."""
        ...

    def get_by_id(self, observation_id: str) -> Optional[MarketObservation]:
        """Obtiene una observación por su ID primario."""
        ...

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[MarketObservation]:
        """Obtiene una observación por su clave determinista de idempotencia."""
        ...

    def list_by_entity(self, entity_id: str, limit: int = 100) -> List[MarketObservation]:
        """Lista observaciones históricas para una entidad ordenadas cronológicamente."""
        ...

    def list_all(self, limit: int = 1000) -> List[MarketObservation]:
        """Lista todas las observaciones registradas."""
        ...
