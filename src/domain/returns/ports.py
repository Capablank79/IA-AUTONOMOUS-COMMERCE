from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from src.domain.publication.models import SalesChannel
from .models import (
    Claim,
    RefundDetail,
    Return,
    ReturnEvent,
    ReturnQueryResult,
    ReturnReconciliationReport,
    ReturnResolution,
    ReturnStatus,
)


class ReturnsPort(ABC):
    """
    Puerto de infraestructura para la interacción con APIs externas de devoluciones, reclamos y reembolsos.
    Aísla completamente el dominio de los protocolos HTTP y formatos específicos de marketplaces.
    """

    @abstractmethod
    def fetch_returns(
        self,
        channel: SalesChannel,
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ReturnQueryResult:
        """
        Consulta devoluciones en el canal externo mediante búsqueda o polling estructurado.
        """
        pass

    @abstractmethod
    def get_return_by_external_id(
        self,
        external_return_id: str,
        channel: SalesChannel,
    ) -> ReturnQueryResult:
        """
        Obtiene el detalle completo de una devolución externa específica.
        """
        pass

    @abstractmethod
    def get_return_by_external_order_id(
        self,
        external_order_id: str,
        channel: SalesChannel,
    ) -> ReturnQueryResult:
        """
        Obtiene la devolución asociada a una orden externa específica si existe.
        """
        pass

    @abstractmethod
    def get_claim_by_external_id(
        self,
        external_claim_id: str,
        channel: SalesChannel,
    ) -> Optional[Claim]:
        """
        Obtiene el detalle de un reclamo/disputa externa específico.
        """
        pass

    @abstractmethod
    def create_return_request(
        self,
        external_order_id: str,
        channel: SalesChannel,
        reason: str,
        correlation_id: str,
        idempotency_key: str,
    ) -> ReturnQueryResult:
        """
        Solicita la creación de una devolución en el canal externo si está soportado.
        Si no está soportado, retorna ReturnQueryResult con error NOT_SUPPORTED o UNKNOWN.
        """
        pass

    @abstractmethod
    def execute_refund(
        self,
        external_return_id: str,
        external_order_id: str,
        amount: Decimal,
        currency: str,
        channel: SalesChannel,
        correlation_id: str,
        idempotency_key: str,
    ) -> Optional[RefundDetail]:
        """
        Ejecuta un reembolso postventa en el canal externo si la API lo soporta.
        Si no está disponible o falla con timeout/5xx, retorna RefundDetail con status UNKNOWN o FAILED.
        """
        pass


class ReturnsRepositoryPort(ABC):
    """
    Puerto de persistencia para devoluciones, reclamos, eventos de auditoría e idempotencia.
    """

    @abstractmethod
    def save_return(self, ret: Return) -> None:
        """
        Persiste o actualiza una devolución normalizada.
        """
        pass

    @abstractmethod
    def get_return_by_id(self, return_id: str) -> Optional[Return]:
        """
        Recupera una devolución por su identificador interno único.
        """
        pass

    @abstractmethod
    def get_return_by_external_id(
        self,
        external_return_id: str,
        channel_id: str,
    ) -> Optional[Return]:
        """
        Recupera una devolución por su ID externo y canal.
        """
        pass

    @abstractmethod
    def get_return_by_order_id(self, order_id: str) -> Optional[Return]:
        """
        Recupera la devolución asociada a una orden interna.
        """
        pass

    @abstractmethod
    def get_return_by_external_order_id(
        self,
        external_order_id: str,
        channel_id: str,
    ) -> Optional[Return]:
        """
        Recupera la devolución asociada a una orden externa.
        """
        pass

    @abstractmethod
    def list_returns(
        self,
        channel_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Return]:
        """
        Lista devoluciones con filtros opcionales de canal y estado.
        """
        pass

    @abstractmethod
    def save_claim(self, claim: Claim) -> None:
        """
        Persiste o actualiza un reclamo/disputa.
        """
        pass

    @abstractmethod
    def get_claim_by_id(self, claim_id: str) -> Optional[Claim]:
        """
        Recupera un reclamo por su identificador interno.
        """
        pass

    @abstractmethod
    def get_claim_by_external_id(
        self,
        external_claim_id: str,
        channel_id: str,
    ) -> Optional[Claim]:
        """
        Recupera un reclamo por su ID externo y canal.
        """
        pass

    @abstractmethod
    def save_return_event(self, event: ReturnEvent) -> bool:
        """
        Registra un evento de devolución garantizando idempotencia por event_id.
        Retorna True si fue insertado, False si era duplicado.
        """
        pass

    @abstractmethod
    def get_events_for_return(self, return_id: str) -> Sequence[ReturnEvent]:
        """
        Obtiene el historial de eventos para una devolución dada.
        """
        pass

    @abstractmethod
    def is_event_processed(self, event_id: str) -> bool:
        """
        Verifica si un evento ya fue procesado previamente.
        """
        pass

    @abstractmethod
    def record_processed_event(self, event_id: str) -> None:
        """
        Registra un event_id como procesado para deduplicación estricta.
        """
        pass

    @abstractmethod
    def is_idempotency_key_executed(self, idempotency_key: str) -> bool:
        """
        Verifica si una clave de idempotencia ya fue ejecutada.
        """
        pass

    @abstractmethod
    def record_executed_idempotency_key(self, idempotency_key: str) -> None:
        """
        Registra una clave de idempotencia ejecutada.
        """
        pass
