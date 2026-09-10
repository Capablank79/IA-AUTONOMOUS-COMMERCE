"""
Puertos de dominio para SaaS Observability (Hito O.12 — SaaS / Platformization).

Define:
- OperationalAlertRepositoryPort: Persistencia de alertas operacionales tenant-scoped.
- TenantObservabilityServicePort: Servicio orquestador de métricas, salud y alertas de tenant.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Sequence, List, Dict, Any

from .models import (
    OperationalAlert,
    OperationalAlertType,
    AlertStatus,
    TenantOperationalSnapshot,
    ObservabilityMetric,
    MetricType,
)


class OperationalAlertRepositoryPort(ABC):
    """Puerto de repositorio para persistencia de alertas operacionales de tenants."""

    @abstractmethod
    def save_alert(self, alert: OperationalAlert) -> OperationalAlert:
        """Guarda o actualiza una alerta operacional."""
        pass

    @abstractmethod
    def get_alert_by_id(self, tenant_id: str, alert_id: str) -> Optional[OperationalAlert]:
        """Obtiene una alerta por ID dentro del tenant."""
        pass

    @abstractmethod
    def list_alerts(
        self,
        tenant_id: str,
        status: Optional[AlertStatus] = None,
        organization_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[OperationalAlert]:
        """Lista alertas asociadas a un tenant/organización."""
        pass

    @abstractmethod
    def get_active_alert_by_deduplication_key(
        self, tenant_id: str, deduplication_key: str
    ) -> Optional[OperationalAlert]:
        """Busca una alerta activa con la misma clave de deduplicación."""
        pass


class TenantObservabilityServicePort(ABC):
    """Puerto del servicio de observabilidad de tenants."""

    @abstractmethod
    def get_tenant_snapshot(
        self,
        tenant_id: str,
        organization_id: Optional[str] = None,
        window_seconds: int = 3600,
    ) -> TenantOperationalSnapshot:
        """Calcula y retorna un snapshot operacional determinista para el tenant."""
        pass

    @abstractmethod
    def evaluate_tenant_health_and_alerts(
        self,
        tenant_id: str,
        organization_id: Optional[str] = None,
        window_seconds: int = 3600,
    ) -> Sequence[OperationalAlert]:
        """Evalúa métricas y señales operacionales, emitiendo o resolviendo alertas según corresponda."""
        pass

    @abstractmethod
    def acknowledge_alert(
        self, tenant_id: str, alert_id: str, actor_id: str
    ) -> OperationalAlert:
        """Marca una alerta como reconocida."""
        pass

    @abstractmethod
    def resolve_alert(
        self, tenant_id: str, alert_id: str, actor_id: str, reason: str = ""
    ) -> OperationalAlert:
        """Resuelve manualmente o explícitamente una alerta."""
        pass

    @abstractmethod
    def list_alerts(
        self,
        tenant_id: str,
        status: Optional[AlertStatus] = None,
        organization_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[OperationalAlert]:
        """Lista alertas asociadas a un tenant/organización."""
        pass
