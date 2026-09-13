"""
Puertos de repositorio, notificación y servicio para Alerting de Producción (Hito P.8).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Sequence, Tuple, Union, List, Dict, Any

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.production_alerting.models import (
    AlertRule,
    AlertRuleType,
    AlertSeverity,
    AlertState,
    ProductionAlertInstance,
    ProductionAlertScope,
    NotificationMessage,
    NotificationResult,
    AlertEvaluationResult,
)


class ProductionAlertRepositoryPort(ABC):
    """Puerto de persistencia para alertas de producción e incidentes."""

    @abstractmethod
    def save_alert(self, alert: ProductionAlertInstance) -> ProductionAlertInstance:
        """Persiste o actualiza una instancia de alerta de producción de forma atómica e íntegra."""
        pass

    @abstractmethod
    def get_alert_by_id(
        self,
        environment: ApplicationEnvironment,
        alert_id: str,
        tenant_id: Optional[str] = None,
    ) -> Optional[ProductionAlertInstance]:
        """Obtiene una alerta por ID dentro de su entorno y tenant."""
        pass

    @abstractmethod
    def get_active_alert_by_deduplication_key(
        self,
        environment: ApplicationEnvironment,
        deduplication_key: str,
    ) -> Optional[ProductionAlertInstance]:
        """Obtiene una alerta activa o reconocida con la clave de deduplicación dada."""
        pass

    @abstractmethod
    def list_alerts(
        self,
        environment: ApplicationEnvironment,
        state: Optional[AlertState] = None,
        severity: Optional[AlertSeverity] = None,
        rule_type: Optional[AlertRuleType] = None,
        scope: Optional[ProductionAlertScope] = None,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[ProductionAlertInstance]:
        """Lista alertas filtradas con control estricto de entorno y tenant."""
        pass

    @abstractmethod
    def clear(self, environment: Optional[ApplicationEnvironment] = None) -> None:
        """Limpia las alertas registradas (útil para reinicialización o pruebas)."""
        pass


class NotificationPort(ABC):
    """Puerto desacoplado para canal de notificación de alertas de producción."""

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Nombre del canal (ej: MOCK, AUDIT_LOG, WEBHOOK_STUB)."""
        pass

    @abstractmethod
    def send(self, message: NotificationMessage) -> NotificationResult:
        """
        Envía un mensaje de notificación.
        Failure-safe: nunca lanza excepciones no controladas hacia el motor de alertas.
        """
        pass
