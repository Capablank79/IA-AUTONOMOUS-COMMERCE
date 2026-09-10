"""
Puertos de dominio para Quota Management SaaS (Hito O.7 — Quota Management).

Define:
- QuotaPolicyRepositoryPort: Almacenamiento y recuperación tenant-scoped de políticas de cuota.
- QuotaReservationRepositoryPort: Gestión y ciclo de vida de reservas de cuota en vuelo con protección atómica contra TOCTOU.
- QuotaManagementServicePort: Contrato principal de evaluación, reserva y reconciliación de cuotas.
- QuotaAuditPort: Emisión desacoplada de eventos de auditoría y trazas de gobernanza de cuotas.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Sequence, Mapping, Any

from src.domain.tenant.models import TenantContext
from .models import (
    QuotaPolicy,
    QuotaRequest,
    QuotaDecision,
    QuotaReservation,
    QuotaReservationStatus,
)


class QuotaPolicyRepositoryPort(ABC):
    """Puerto de persistencia para políticas de cuota por Tenant."""

    @abstractmethod
    def get_policy(self, tenant_id: str) -> Optional[QuotaPolicy]:
        """Obtiene la política activa de cuotas para el tenant dado."""
        pass

    @abstractmethod
    def save_policy(self, policy: QuotaPolicy) -> None:
        """Guarda o actualiza la política de cuotas de un tenant."""
        pass

    @abstractmethod
    def delete_policy(self, tenant_id: str) -> bool:
        """Elimina la política de cuotas de un tenant."""
        pass


class QuotaReservationRepositoryPort(ABC):
    """Puerto de persistencia y concurrencia para reservas de cuota en vuelo."""

    @abstractmethod
    def save_reservation(self, reservation: QuotaReservation) -> None:
        """Guarda o actualiza una reserva de cuota."""
        pass

    @abstractmethod
    def get_reservation(self, reservation_id: str) -> Optional[QuotaReservation]:
        """Recupera una reserva por su ID."""
        pass

    @abstractmethod
    def get_by_correlation_id(self, tenant_id: str, correlation_id: str) -> Optional[QuotaReservation]:
        """Recupera una reserva existente por correlation_id para idempotencia."""
        pass

    @abstractmethod
    def list_active_reservations(
        self,
        tenant_id: str,
        current_time: datetime,
        identity_id: Optional[str] = None,
        model_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> List[QuotaReservation]:
        """Lista las reservas activas (RESERVED y no expiradas) para un tenant / scope."""
        pass


class QuotaAuditPort(ABC):
    """Puerto desacoplado para emisión de auditoría y traza de cuotas."""

    @abstractmethod
    def emit_quota_event(
        self,
        event_name: str,
        tenant_id: str,
        identity_id: Optional[str],
        details: Mapping[str, Any],
        correlation_id: Optional[str] = None,
    ) -> None:
        """Emite un evento de auditoría/traza seguro (sin prompts ni PII)."""
        pass


class QuotaManagementServicePort(ABC):
    """Contrato del servicio principal de gobernanza de cuotas de IA."""

    @abstractmethod
    def evaluate_and_reserve(
        self,
        request: QuotaRequest,
        context: Optional[TenantContext] = None,
    ) -> QuotaDecision:
        """
        Evalúa las cuotas aplicables y, si ALLOW, crea una reserva en vuelo atómica.
        """
        pass

    @abstractmethod
    def check_quota_only(
        self,
        request: QuotaRequest,
        context: Optional[TenantContext] = None,
    ) -> QuotaDecision:
        """
        Evalúa las cuotas aplicables de forma idempotente sin crear reservas en vuelo (read-only).
        """
        pass

    @abstractmethod
    def reconcile_reservation(
        self,
        reservation_id: str,
        tenant_id: str,
        actual_status: QuotaReservationStatus,
        actual_tokens: Optional[int] = None,
        actual_cost: Optional[Any] = None,
    ) -> Optional[QuotaReservation]:
        """
        Reconcilia o libera una reserva una vez concluida la inferencia o fallado el pipeline.
        """
        pass
