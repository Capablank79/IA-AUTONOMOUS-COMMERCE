"""
Puertos de dominio y contratos para el Bus de Eventos y Event Store (Hito J.5).

Define los contratos abstractos desacoplados del sistema de almacenamiento o transporte.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Callable, Dict, Any

from src.domain.events.models import EventRecord, DeliveryRecord, EventType


class EventHandlerPort(ABC):
    """
    Contrato base para un consumidor/manejador de eventos desacoplado.
    """
    @property
    @abstractmethod
    def handler_id(self) -> str:
        """Identificador determinista y único del manejador."""
        pass

    @abstractmethod
    def can_handle(self, event_type: EventType) -> bool:
        """Indica si este handler procesa el tipo de evento especificado."""
        pass

    @abstractmethod
    def handle(self, event: EventRecord) -> None:
        """
        Ejecuta la lógica del consumidor sobre el evento.
        Debe ser idempotente respecto a reintentos o repetición del mismo evento.
        """
        pass


class EventStorePort(ABC):
    """
    Puerto de persistencia para Eventos y DeliveryRecords (Event Store).
    """
    @abstractmethod
    def append(self, event: EventRecord) -> EventRecord:
        """
        Persiste un evento de forma atómica e inmutable.
        Si ya existe por idempotency_key o event_id, retorna la versión existente sin duplicar.
        """
        pass

    @abstractmethod
    def get_by_id(self, event_id: str) -> Optional[EventRecord]:
        """Obtiene un evento por su event_id."""
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[EventRecord]:
        """Obtiene un evento por su clave de idempotencia."""
        pass

    @abstractmethod
    def list_events(
        self,
        event_type: Optional[EventType] = None,
        subject_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[EventRecord]:
        """Lista eventos persistidos según filtros ordenados cronológicamente."""
        pass

    @abstractmethod
    def record_delivery(self, delivery: DeliveryRecord) -> DeliveryRecord:
        """
        Persiste o actualiza el estado de entrega a un handler específico.
        """
        pass

    @abstractmethod
    def get_delivery(self, event_id: str, handler_id: str) -> Optional[DeliveryRecord]:
        """Obtiene el registro de entrega de un evento para un handler."""
        pass

    @abstractmethod
    def list_deliveries_by_event(self, event_id: str) -> List[DeliveryRecord]:
        """Lista todos los registros de entrega asociados a un evento."""
        pass


class EventPublisherPort(ABC):
    """
    Puerto para publicación y despacho de eventos hacia los suscriptores.
    """
    @abstractmethod
    def publish(self, event: EventRecord) -> List[DeliveryRecord]:
        """
        Publica y despacha el evento a todos los handlers registrados.
        Retorna los registros de entrega resultantes.
        """
        pass
