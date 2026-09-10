"""
Puertos de dominio para Billing & Subscription Management SaaS (Hito O.9 — Billing & Subscription Management).

Define:
- SubscriptionRepositoryPort: Persistencia tenant-scoped de Suscripciones.
- InvoiceRepositoryPort: Persistencia tenant-scoped de Invoices.
- PaymentAttemptRepositoryPort: Persistencia tenant-scoped de PaymentAttempts.
- PaymentProviderEventRepositoryPort: Persistencia de eventos procesados para idempotencia.
- PaymentProviderPort: Abstracción de pasarela de pago (Stripe, MercadoPago, Mock).
- BillingServicePort: Contrato para ciclo de vida de facturación, emisión de invoices, cobros y eventos.
- SubscriptionServicePort: Contrato para gestión comercial de suscripciones y sincronización con O.8.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Sequence, Mapping, Any, Tuple

from src.domain.tenant.models import TenantContext
from .models import (
    Subscription,
    Invoice,
    InvoiceStatus,
    PaymentAttempt,
    PaymentStatus,
    PaymentProviderEvent,
    PaymentProviderReference,
    BillingPeriod,
    BillingCycle,
)


class SubscriptionRepositoryPort(ABC):
    """Puerto de persistencia tenant-scoped para suscripciones comerciales."""

    @abstractmethod
    def save_subscription(self, subscription: Subscription) -> None:
        """Guarda o actualiza una suscripción de forma atómica."""
        pass

    @abstractmethod
    def get_subscription(self, subscription_id: str) -> Optional[Subscription]:
        """Obtiene una suscripción por su ID único."""
        pass

    @abstractmethod
    def get_active_subscription(self, tenant_id: str, current_time: Optional[datetime] = None) -> Optional[Subscription]:
        """Obtiene la suscripción activa y vigente para un tenant."""
        pass

    @abstractmethod
    def list_subscriptions_for_tenant(self, tenant_id: str) -> List[Subscription]:
        """Lista todas las suscripciones históricas y actuales de un tenant."""
        pass


class InvoiceRepositoryPort(ABC):
    """Puerto de persistencia tenant-scoped para facturas (Invoices)."""

    @abstractmethod
    def save_invoice(self, invoice: Invoice) -> None:
        """Guarda o actualiza una factura de forma atómica."""
        pass

    @abstractmethod
    def get_invoice(self, invoice_id: str) -> Optional[Invoice]:
        """Obtiene una factura por su ID único."""
        pass

    @abstractmethod
    def get_invoice_by_period(self, tenant_id: str, subscription_id: str, period: BillingPeriod) -> Optional[Invoice]:
        """Obtiene la factura emitida para un tenant, suscripción y período determinado (idempotencia)."""
        pass

    @abstractmethod
    def list_invoices_for_tenant(self, tenant_id: str, status: Optional[InvoiceStatus] = None) -> List[Invoice]:
        """Lista todas las facturas de un tenant, opcionalmente filtradas por estado."""
        pass


class PaymentAttemptRepositoryPort(ABC):
    """Puerto de persistencia tenant-scoped para intentos de pago."""

    @abstractmethod
    def save_attempt(self, attempt: PaymentAttempt) -> None:
        """Guarda o actualiza un intento de pago de forma atómica."""
        pass

    @abstractmethod
    def get_attempt(self, attempt_id: str) -> Optional[PaymentAttempt]:
        """Obtiene un intento de pago por su ID."""
        pass

    @abstractmethod
    def get_attempt_by_idempotency_key(self, tenant_id: str, idempotency_key: str) -> Optional[PaymentAttempt]:
        """Obtiene un intento de pago por clave de idempotencia."""
        pass

    @abstractmethod
    def list_attempts_for_invoice(self, tenant_id: str, invoice_id: str) -> List[PaymentAttempt]:
        """Lista todos los intentos de pago realizados para una factura específica."""
        pass


class PaymentProviderEventRepositoryPort(ABC):
    """Puerto de persistencia para registro de eventos de pasarela y control de idempotencia."""

    @abstractmethod
    def save_event(self, event: PaymentProviderEvent) -> None:
        """Guarda un evento procesado."""
        pass

    @abstractmethod
    def get_event(self, event_id: str) -> Optional[PaymentProviderEvent]:
        """Obtiene un evento por su ID."""
        pass

    @abstractmethod
    def is_event_processed(self, event_id: str) -> bool:
        """Verifica si un evento ya fue procesado exitosamente."""
        pass


class PaymentProviderPort(ABC):
    """
    Abstracción desacoplada de pasarela de pago (Stripe, MercadoPago, Mock).
    NUNCA expone secretos ni maneja números de tarjeta directos.
    """

    @abstractmethod
    def create_checkout_session(
        self,
        tenant_id: str,
        subscription_id: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> PaymentProviderReference:
        """Crea una sesión de checkout / cobro en la pasarela."""
        pass

    @abstractmethod
    def charge_invoice(
        self,
        tenant_id: str,
        invoice_id: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> PaymentAttempt:
        """Ejecuta un intento de cobro directo mediante la pasarela."""
        pass

    @abstractmethod
    def fetch_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        """Consulta el estado de un pago en la pasarela."""
        pass


class SubscriptionServicePort(ABC):
    """Servicio principal de gobernanza de suscripciones y reconciliación con O.8."""

    @abstractmethod
    def create_subscription(
        self,
        tenant_id: str,
        plan_id: str,
        plan_version: Optional[str] = None,
        billing_cycle: BillingCycle = BillingCycle.MONTHLY,
        start_time: Optional[datetime] = None,
        auto_activate: bool = False,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Subscription:
        """Crea una nueva suscripción comercial para el tenant."""
        pass

    @abstractmethod
    def activate_subscription(
        self,
        subscription_id: str,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Subscription:
        """Activa una suscripción tras confirmación de pago o trial y coordina con O.8."""
        pass

    @abstractmethod
    def cancel_subscription(
        self,
        subscription_id: str,
        immediately: bool = False,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Subscription:
        """Cancela una suscripción inmediatamente o al final del período actual sin borrar histórico."""
        pass

    @abstractmethod
    def renew_subscription(
        self,
        subscription_id: str,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Tuple[Subscription, Invoice]:
        """Renueva la suscripción al finalizar el período actual y genera la siguiente factura."""
        pass

    @abstractmethod
    def get_active_subscription(
        self,
        tenant_id: str,
        current_time: Optional[datetime] = None,
        context: Optional[TenantContext] = None,
    ) -> Optional[Subscription]:
        """Resuelve la suscripción activa de un tenant."""
        pass


class BillingServicePort(ABC):
    """Servicio principal de facturación, generación de invoices y procesamiento de pagos."""

    @abstractmethod
    def generate_period_invoice(
        self,
        subscription_id: str,
        period: Optional[BillingPeriod] = None,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Invoice:
        """Genera la factura determinista para un período de suscripción (idempotente)."""
        pass

    @abstractmethod
    def process_payment(
        self,
        invoice_id: str,
        idempotency_key: str,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> PaymentAttempt:
        """Ejecuta el cobro de una factura a través de la pasarela configurada."""
        pass

    @abstractmethod
    def process_provider_event(
        self,
        event: PaymentProviderEvent,
    ) -> None:
        """Procesa un evento de webhook de pasarela de forma determinista e idempotente."""
        pass
