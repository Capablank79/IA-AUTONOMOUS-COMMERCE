from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Sequence

from src.domain.publication.models import SalesChannel
from .models import Order, OrderEvent, OrderQueryResult, OrderReconciliationReport


class OrderPort(ABC):
    """
    Puerto de salida de infraestructura para la interacción con APIs externas de órdenes.
    Aísla completamente el dominio y la aplicación de los protocolos HTTP y SDKs de marketplaces.
    """

    @abstractmethod
    def fetch_orders(
        self,
        channel: SalesChannel,
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> OrderQueryResult:
        """
        Consulta órdenes en el canal externo mediante búsqueda o polling estructurado.
        """
        pass

    @abstractmethod
    def get_order_by_external_id(
        self,
        external_order_id: str,
        channel: SalesChannel,
    ) -> OrderQueryResult:
        """
        Obtiene el detalle completo de una orden externa específica.
        """
        pass


class OrderRepositoryPort(ABC):
    """
    Puerto de persistencia para órdenes normalizadas, historial y deduplicación.
    """

    @abstractmethod
    def save_order(self, order: Order) -> None:
        """
        Persiste o actualiza una orden normalizada.
        """
        pass

    @abstractmethod
    def get_order_by_id(self, order_id: str) -> Optional[Order]:
        """
        Recupera una orden por su identificador interno único.
        """
        pass

    @abstractmethod
    def get_order_by_external_id(
        self,
        external_order_id: str,
        channel_id: str,
    ) -> Optional[Order]:
        """
        Recupera una orden por su identificador externo de marketplace y canal.
        """
        pass

    @abstractmethod
    def list_orders(
        self,
        channel_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Order]:
        """
        Lista órdenes internas según filtros.
        """
        pass

    @abstractmethod
    def record_processed_event(
        self,
        event_id: str,
        idempotency_key: str,
        external_order_id: str,
    ) -> bool:
        """
        Registra el identificador de evento / idempotency_key procesado.
        Retorna True si es nuevo, False si ya había sido procesado (detección de duplicado).
        """
        pass

    @abstractmethod
    def is_event_processed(self, event_id: str, idempotency_key: str) -> bool:
        """
        Verifica si un evento o clave de idempotencia ya fue procesado previamente.
        """
        pass
