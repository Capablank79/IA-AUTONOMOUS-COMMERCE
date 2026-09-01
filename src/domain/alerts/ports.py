"""
Puertos de dominio para Alertas Autónomas (Autonomous Alerts - Hito J.6).

Define:
- AlertRepositoryPort: Persistencia durable de AlertRecord y AlertDeliveryResult.
- AlertDeliveryPort: Puerto de entrega desacoplado de canales externos concretos.
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from src.domain.alerts.models import AlertRecord, AlertDeliveryResult, AlertType, AlertSeverity, AlertStatus


class AlertRepositoryPort(ABC):
    """
    Puerto de persistencia para registros de alertas y sus resultados de entrega.
    """
    @abstractmethod
    def save(self, alert: AlertRecord) -> AlertRecord:
        """
        Persiste una alerta de forma atómica e inmutable.
        Si ya existe por idempotency_key o alert_id, retorna la alerta persistida.
        """
        pass

    @abstractmethod
    def get_by_id(self, alert_id: str) -> Optional[AlertRecord]:
        """Obtiene una alerta por su alert_id."""
        pass

    @abstractmethod
    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AlertRecord]:
        """Obtiene una alerta por su clave de idempotencia."""
        pass

    @abstractmethod
    def list_alerts(
        self,
        alert_type: Optional[AlertType] = None,
        severity: Optional[AlertSeverity] = None,
        subject_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AlertRecord]:
        """Lista alertas persistidas con filtros deterministas ordenadas cronológicamente."""
        pass

    @abstractmethod
    def record_delivery_result(self, result: AlertDeliveryResult) -> AlertDeliveryResult:
        """
        Persiste el resultado de un intento de entrega.
        """
        pass

    @abstractmethod
    def list_delivery_results_by_alert(self, alert_id: str) -> List[AlertDeliveryResult]:
        """Lista todos los resultados de entrega asociados a una alerta."""
        pass


class AlertDeliveryPort(ABC):
    """
    Puerto desacoplado para despachar alertas a canales de notificación.
    """
    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Nombre del canal de entrega (ej: IN_MEMORY, WEBHOOK, EMAIL, etc.)."""
        pass

    @abstractmethod
    def deliver(self, alert: AlertRecord) -> AlertDeliveryResult:
        """
        Envía la alerta al destino configurado.
        Debe devolver un AlertDeliveryResult estructurado y no lanzar excepciones no controladas.
        """
        pass
