from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Sequence

from src.domain.publication.models import SalesChannel
from .models import (
    FulfillmentReconciliationReport,
    Shipment,
    ShipmentQueryResult,
    ShippingLabel,
    TrackingEvent,
)


class FulfillmentPort(ABC):
    """
    Puerto de salida de infraestructura para la interacción con APIs externas de envíos, tracking y etiquetas.
    Aísla completamente el dominio y la aplicación de los protocolos HTTP y SDKs de marketplaces o couriers.
    """

    @abstractmethod
    def fetch_shipments(
        self,
        channel: SalesChannel,
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ShipmentQueryResult:
        """
        Consulta envíos en el canal externo mediante búsqueda o polling estructurado.
        """
        pass

    @abstractmethod
    def get_shipment_by_external_id(
        self,
        external_shipment_id: str,
        channel: SalesChannel,
    ) -> ShipmentQueryResult:
        """
        Obtiene el detalle completo de un envío externo específico.
        """
        pass

    @abstractmethod
    def get_shipment_by_external_order_id(
        self,
        external_order_id: str,
        channel: SalesChannel,
    ) -> ShipmentQueryResult:
        """
        Obtiene el envío asociado a una orden externa específica.
        """
        pass

    @abstractmethod
    def get_tracking(
        self,
        external_shipment_id: str,
        channel: SalesChannel,
    ) -> Sequence[TrackingEvent]:
        """
        Obtiene el historial de eventos de seguimiento de un envío.
        """
        pass

    @abstractmethod
    def get_shipping_label(
        self,
        external_shipment_id: str,
        channel: SalesChannel,
    ) -> Optional[ShippingLabel]:
        """
        Obtiene o genera la etiqueta de envío si el canal cuenta con soporte real para ello.
        Si no está soportado o disponible, retorna None o ShippingLabel con status NOT_SUPPORTED.
        """
        pass


class FulfillmentRepositoryPort(ABC):
    """
    Puerto de persistencia para envíos normalizados, eventos de tracking e historial logístico.
    """

    @abstractmethod
    def save_shipment(self, shipment: Shipment) -> None:
        """
        Persiste o actualiza un envío normalizado.
        """
        pass

    @abstractmethod
    def get_shipment_by_id(self, shipment_id: str) -> Optional[Shipment]:
        """
        Recupera un envío por su identificador interno único.
        """
        pass

    @abstractmethod
    def get_shipment_by_external_id(
        self,
        external_shipment_id: str,
        channel_id: str,
    ) -> Optional[Shipment]:
        """
        Recupera un envío por su identificador externo de marketplace y canal.
        """
        pass

    @abstractmethod
    def get_shipment_by_order_id(
        self,
        order_id: str,
    ) -> Optional[Shipment]:
        """
        Recupera un envío por el identificador de orden interna.
        """
        pass

    @abstractmethod
    def get_shipment_by_external_order_id(
        self,
        external_order_id: str,
        channel_id: str,
    ) -> Optional[Shipment]:
        """
        Recupera un envío por el identificador de orden externa.
        """
        pass

    @abstractmethod
    def list_shipments(
        self,
        channel_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Shipment]:
        """
        Lista envíos internos según filtros.
        """
        pass

    @abstractmethod
    def save_tracking_event(self, event: TrackingEvent) -> bool:
        """
        Persiste un evento de tracking. Retorna True si es nuevo, False si ya existía (deduplicado).
        """
        pass

    @abstractmethod
    def get_tracking_events(self, shipment_id: str) -> Sequence[TrackingEvent]:
        """
        Obtiene el historial de eventos de seguimiento ordenados cronológicamente.
        """
        pass

    @abstractmethod
    def record_processed_fulfillment_event(
        self,
        event_id: str,
        idempotency_key: str,
        external_shipment_id: str,
    ) -> bool:
        """
        Registra el identificador de evento logístico / idempotency_key procesado.
        Retorna True si es nuevo, False si ya había sido procesado (detección de duplicado).
        """
        pass

    @abstractmethod
    def is_fulfillment_event_processed(self, event_id: str, idempotency_key: str) -> bool:
        """
        Verifica si un evento logístico o clave de idempotencia ya fue procesado previamente.
        """
        pass
