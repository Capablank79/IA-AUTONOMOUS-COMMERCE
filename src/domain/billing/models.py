"""
Modelos de dominio para Billing & Subscription Management SaaS (Hito O.9 — Billing & Subscription Management).

Define:
- SubscriptionStatus: Estados de ciclo de vida de la suscripción (ACTIVE, TRIALING, PAST_DUE, CANCELED, EXPIRED, SUSPENDED, UNKNOWN).
- BillingCycle: Periodicidad comercial (MONTHLY, ANNUAL).
- BillingPeriod: Ventana temporal UTC inmutable [start_time, end_time).
- Money: Objeto de valor monetario inmutable con Decimal y código de moneda ISO 4217.
- PaymentProviderReference: Referencia inmutable a identidades de pasarela de pago segura (sin datos de tarjeta/secretos).
- Subscription: Entidad agregada inmutable y versionada de suscripción comercial tenant-scoped.
- InvoiceStatus: Estados de la factura (DRAFT, OPEN, PAID, VOID, UNCOLLECTIBLE, UNKNOWN).
- InvoiceLineType: Tipos canónicos de líneas de factura (BASE_PLAN, USAGE, DISCOUNT, TAX, ADJUSTMENT).
- InvoiceLine: Línea individual inmutable de factura con montos en Decimal.
- Invoice: Factura inmutable determinista tenant-scoped con totales calculados y checksum SHA-256.
- PaymentStatus: Estados del intento de cobro (PENDING, SUCCEEDED, FAILED, CANCELED, UNKNOWN).
- PaymentAttempt: Registro inmutable de intento de pago asociado a una Invoice con checksum SHA-256.
- PaymentProviderEvent: Evento inmutable de pasarela (webhook/notificación) con idempotencia y checksum.
- Excepciones de dominio para Billing & Subscription Management.

Principios O.9:
1. Responde a: "¿Qué plan tiene contratado un tenant, cuál es su ciclo de facturación, qué invoices se generan y cuál es el estado de cobro?".
2. Desacoplamiento O.8 vs O.9: Plan define catálogo y entitlements técnicos (O.8); Subscription define contrato comercial y estado financiero (O.9).
3. Moneda y Montos: Precisión con Decimal obligatoria, nunca float. Sin conversión implícita entre monedas distintas.
4. Tenant Isolation estricto: Todo recurso de facturación (Subscription, Invoice, PaymentAttempt) está particionado por tenant_id.
5. Seguridad Financiera (N.5, N.9): Cero almacenamiento de números de tarjeta (PAN), CVV, o secretos de pasarela.
6. Idempotencia y Determinismo: Creación de suscripciones, emisión de invoices, cobros y procesamiento de eventos son estrictamente idempotentes.
7. Reconciliación con O.8: Subscription ACTIVE coordina/conduce la asignación de Plan en O.8 (PlanAssignment).
8. Preservación Histórica: Cancelaciones y fallos nunca eliminan invoices o histórico de consumo de O.6.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List
import uuid

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class BillingError(Exception):
    """Excepción base para errores del dominio de Billing & Subscription."""
    pass


class SubscriptionNotFoundError(BillingError):
    """Se lanza cuando no se encuentra una suscripción solicitada."""
    pass


class InvoiceNotFoundError(BillingError):
    """Se lanza cuando no se encuentra una factura solicitada."""
    pass


class PaymentAttemptNotFoundError(BillingError):
    """Se lanza cuando no se encuentra un intento de pago."""
    pass


class BillingIntegrityError(BillingError):
    """Se lanza cuando se detecta manipulación o discrepancia de checksum en un modelo de facturación."""
    pass


class CurrencyMismatchError(BillingError):
    """Se lanza cuando se intentan operar montos con monedas distintas sin conversión explícita."""
    pass


class InvalidSubscriptionStateError(BillingError):
    """Se lanza cuando una transición de estado de suscripción es inválida según el ciclo de vida."""
    pass


class DuplicateBillingEventError(BillingError):
    """Se lanza ante eventos de pago o cobros duplicados no autorizados."""
    pass


class PaymentProviderError(BillingError):
    """Se lanza cuando ocurre un error en la comunicación o respuesta de la pasarela de pago."""
    pass


class BillingTenantIsolationError(BillingError):
    """Se lanza cuando se detecta un intento de acceso cruzado entre tenants en facturación."""
    pass


class SubscriptionStatus(str, Enum):
    """Estados canónicos del ciclo de vida de una Suscripción."""
    ACTIVE = "ACTIVE"
    TRIALING = "TRIALING"
    PAST_DUE = "PAST_DUE"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


class BillingCycle(str, Enum):
    """Periodicidad comercial del ciclo de facturación."""
    MONTHLY = "MONTHLY"
    ANNUAL = "ANNUAL"


class InvoiceStatus(str, Enum):
    """Estados canónicos de una Factura."""
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    PAID = "PAID"
    VOID = "VOID"
    UNCOLLECTIBLE = "UNCOLLECTIBLE"
    UNKNOWN = "UNKNOWN"


class InvoiceLineType(str, Enum):
    """Tipos canónicos de líneas de factura."""
    BASE_PLAN = "BASE_PLAN"
    USAGE = "USAGE"
    DISCOUNT = "DISCOUNT"
    TAX = "TAX"
    ADJUSTMENT = "ADJUSTMENT"


class PaymentStatus(str, Enum):
    """Estados canónicos del intento de pago."""
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Money:
    """
    Objeto de valor inmutable para representación monetaria segura y determinista.
    Garantiza uso estricto de Decimal y validación de moneda ISO 4217.
    """
    amount: Decimal
    currency: str = "USD"

    def __post_init__(self):
        if not isinstance(self.amount, Decimal):
            if isinstance(self.amount, (int, str)):
                object.__setattr__(self, "amount", Decimal(str(self.amount)))
            elif isinstance(self.amount, float):
                # Rechazar float para prevenir errores de precisión de punto flotante
                raise TypeError("Float is not permitted for Money amount. Use Decimal, int or string.")
            else:
                raise TypeError(f"Invalid type for Money amount: {type(self.amount)}")

        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError("Money currency must be a non-empty string (e.g. 'USD', 'CLP').")

        object.__setattr__(self, "currency", self.currency.strip().upper())
        # Cuantificar a 2 decimales estándar por defecto para consistencia
        rounded = self.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        object.__setattr__(self, "amount", rounded)

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            raise TypeError("Cannot add Money to non-Money instance.")
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"Currency mismatch: {self.currency} vs {other.currency}")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            raise TypeError("Cannot subtract non-Money instance from Money.")
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"Currency mismatch: {self.currency} vs {other.currency}")
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: Union[Decimal, int]) -> "Money":
        if isinstance(factor, float):
            raise TypeError("Float multiplication is not permitted for Money.")
        if not isinstance(factor, (Decimal, int)):
            raise TypeError(f"Cannot multiply Money by {type(factor)}")
        return Money(self.amount * Decimal(str(factor)), self.currency)

    def __lt__(self, other: "Money") -> bool:
        if not isinstance(other, Money) or self.currency != other.currency:
            raise CurrencyMismatchError("Cannot compare Money with different currencies or types.")
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        if not isinstance(other, Money) or self.currency != other.currency:
            raise CurrencyMismatchError("Cannot compare Money with different currencies or types.")
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        if not isinstance(other, Money) or self.currency != other.currency:
            raise CurrencyMismatchError("Cannot compare Money with different currencies or types.")
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        if not isinstance(other, Money) or self.currency != other.currency:
            raise CurrencyMismatchError("Cannot compare Money with different currencies or types.")
        return self.amount >= other.amount

    @classmethod
    def zero(cls, currency: str = "USD") -> "Money":
        return cls(Decimal("0.00"), currency)


@dataclass(frozen=True)
class BillingPeriod:
    """
    Ventana temporal UTC inmutable que delimita un ciclo de facturación [start_time, end_time).
    """
    start_time: datetime
    end_time: datetime

    def __post_init__(self):
        if self.start_time.tzinfo is None:
            object.__setattr__(self, "start_time", self.start_time.replace(tzinfo=timezone.utc))
        if self.end_time.tzinfo is None:
            object.__setattr__(self, "end_time", self.end_time.replace(tzinfo=timezone.utc))

        if self.start_time >= self.end_time:
            raise ValueError(f"start_time ({self.start_time}) must be strictly before end_time ({self.end_time})")

    def contains(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return self.start_time <= timestamp < self.end_time

    @classmethod
    def from_cycle(cls, start_time: datetime, cycle: BillingCycle) -> "BillingPeriod":
        """Calcula el BillingPeriod determinista a partir de un timestamp inicial y el BillingCycle."""
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)

        if cycle == BillingCycle.MONTHLY:
            # Siguiente mes aproximado determinista (30 días estándar para consistencia UTC)
            # o cálculo de mes calendario
            year = start_time.year
            month = start_time.month + 1
            if month > 12:
                month = 1
                year += 1
            # Manejar límites de día de mes de forma segura
            day = min(start_time.day, 28)
            end_time = datetime(year, month, day, start_time.hour, start_time.minute, start_time.second, tzinfo=timezone.utc)
        elif cycle == BillingCycle.ANNUAL:
            end_time = datetime(start_time.year + 1, start_time.month, min(start_time.day, 28), start_time.hour, start_time.minute, start_time.second, tzinfo=timezone.utc)
        else:
            raise ValueError(f"Unsupported BillingCycle: {cycle}")

        return cls(start_time=start_time, end_time=end_time)


@dataclass(frozen=True)
class PaymentProviderReference:
    """
    Referencia segura a identidades creadas en pasarelas de pago (p.ej. Stripe Customer ID, Checkout ID).
    NUNCA contiene credenciales ni datos de tarjeta.
    """
    provider: str
    reference_id: str
    customer_reference: Optional[str] = None
    checkout_url: Optional[str] = None

    def __post_init__(self):
        if not self.provider or not self.provider.strip():
            raise ValueError("Provider must be a non-empty string.")
        if not self.reference_id or not self.reference_id.strip():
            raise ValueError("Reference ID must be a non-empty string.")
        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(self, "reference_id", self.reference_id.strip())


@dataclass(frozen=True)
class Subscription:
    """
    Entidad inmutable agregada de Suscripción comercial tenant-scoped.
    """
    subscription_id: str
    tenant_id: str
    plan_id: str
    plan_version: str
    status: SubscriptionStatus
    billing_cycle: BillingCycle
    current_period_start: datetime
    current_period_end: datetime
    base_price: Decimal
    currency: str = "USD"
    cancel_at_period_end: bool = False
    canceled_at: Optional[datetime] = None
    provider_reference: Optional[PaymentProviderReference] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.subscription_id, "subscription_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.plan_id, "plan_id")

        if not isinstance(self.status, SubscriptionStatus):
            object.__setattr__(self, "status", SubscriptionStatus(self.status))

        if not isinstance(self.billing_cycle, BillingCycle):
            object.__setattr__(self, "billing_cycle", BillingCycle(self.billing_cycle))

        if not isinstance(self.base_price, Decimal):
            if isinstance(self.base_price, (int, str)):
                object.__setattr__(self, "base_price", Decimal(str(self.base_price)))
            else:
                raise TypeError("base_price must be a Decimal, int or str.")

        if self.base_price < Decimal("0.00"):
            raise ValueError("base_price cannot be negative.")

        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "base_price", self.base_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        if self.current_period_start.tzinfo is None:
            object.__setattr__(self, "current_period_start", self.current_period_start.replace(tzinfo=timezone.utc))
        if self.current_period_end.tzinfo is None:
            object.__setattr__(self, "current_period_end", self.current_period_end.replace(tzinfo=timezone.utc))
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if self.updated_at.tzinfo is None:
            object.__setattr__(self, "updated_at", self.updated_at.replace(tzinfo=timezone.utc))
        if self.canceled_at is not None and self.canceled_at.tzinfo is None:
            object.__setattr__(self, "canceled_at", self.canceled_at.replace(tzinfo=timezone.utc))

        if self.current_period_start >= self.current_period_end:
            raise ValueError("current_period_start must be strictly before current_period_end")

        if not isinstance(self.metadata, MappingProxyType):
            sanitized = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", MappingProxyType(sanitized))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise BillingIntegrityError(
                f"Subscription checksum mismatch for subscription_id={self.subscription_id}: "
                f"provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        prov_ref = f"{self.provider_reference.provider}:{self.provider_reference.reference_id}" if self.provider_reference else "NONE"
        canc_str = self.canceled_at.isoformat() if self.canceled_at else "NONE"
        raw = (
            f"{self.subscription_id}|{self.tenant_id}|{self.plan_id}|{self.plan_version}|"
            f"{self.status.value}|{self.billing_cycle.value}|{self.current_period_start.isoformat()}|"
            f"{self.current_period_end.isoformat()}|{self.base_price}|{self.currency}|"
            f"{self.cancel_at_period_end}|{canc_str}|{prov_ref}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()

    @property
    def is_active(self) -> bool:
        return self.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)

    @property
    def current_period(self) -> BillingPeriod:
        return BillingPeriod(start_time=self.current_period_start, end_time=self.current_period_end)


@dataclass(frozen=True)
class InvoiceLine:
    """
    Línea individual inmutable de factura.
    """
    line_id: str
    line_type: InvoiceLineType
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    currency: str = "USD"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.line_id, "line_id")
        if not isinstance(self.line_type, InvoiceLineType):
            object.__setattr__(self, "line_type", InvoiceLineType(self.line_type))

        if not isinstance(self.quantity, Decimal):
            object.__setattr__(self, "quantity", Decimal(str(self.quantity)))

        if not isinstance(self.unit_price, Decimal):
            object.__setattr__(self, "unit_price", Decimal(str(self.unit_price)))

        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)))

        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "unit_price", self.unit_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        object.__setattr__(self, "amount", self.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        if not isinstance(self.metadata, MappingProxyType):
            sanitized = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", MappingProxyType(sanitized))


@dataclass(frozen=True)
class Invoice:
    """
    Factura inmutable determinista tenant-scoped.
    """
    invoice_id: str
    tenant_id: str
    subscription_id: str
    period: BillingPeriod
    currency: str
    lines: Tuple[InvoiceLine, ...]
    subtotal: Decimal
    total: Decimal
    status: InvoiceStatus = InvoiceStatus.DRAFT
    discounts: Decimal = Decimal("0.00")
    taxes: Decimal = Decimal("0.00")
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    due_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.invoice_id, "invoice_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.subscription_id, "subscription_id")

        if not isinstance(self.status, InvoiceStatus):
            object.__setattr__(self, "status", InvoiceStatus(self.status))

        if isinstance(self.lines, (list, Sequence)):
            object.__setattr__(self, "lines", tuple(self.lines))

        object.__setattr__(self, "currency", self.currency.strip().upper())

        for field_name in ("subtotal", "discounts", "taxes", "total"):
            val = getattr(self, field_name)
            if not isinstance(val, Decimal):
                val = Decimal(str(val))
            object.__setattr__(self, field_name, val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        if self.issued_at.tzinfo is None:
            object.__setattr__(self, "issued_at", self.issued_at.replace(tzinfo=timezone.utc))
        if self.due_at is not None and self.due_at.tzinfo is None:
            object.__setattr__(self, "due_at", self.due_at.replace(tzinfo=timezone.utc))
        if self.paid_at is not None and self.paid_at.tzinfo is None:
            object.__setattr__(self, "paid_at", self.paid_at.replace(tzinfo=timezone.utc))

        if not isinstance(self.metadata, MappingProxyType):
            sanitized = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", MappingProxyType(sanitized))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise BillingIntegrityError(
                f"Invoice checksum mismatch for invoice_id={self.invoice_id}: "
                f"provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        lines_repr = [
            f"{line.line_id}:{line.line_type.value}:{line.quantity}:{line.unit_price}:{line.amount}"
            for line in sorted(self.lines, key=lambda x: x.line_id)
        ]
        lines_str = ",".join(lines_repr)
        paid_str = self.paid_at.isoformat() if self.paid_at else "NONE"
        due_str = self.due_at.isoformat() if self.due_at else "NONE"
        raw = (
            f"{self.invoice_id}|{self.tenant_id}|{self.subscription_id}|"
            f"{self.period.start_time.isoformat()}|{self.period.end_time.isoformat()}|"
            f"{self.currency}|{self.subtotal}|{self.discounts}|{self.taxes}|{self.total}|"
            f"{self.status.value}|{self.issued_at.isoformat()}|{due_str}|{paid_str}|{lines_str}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()

    @classmethod
    def create_deterministic(
        cls,
        invoice_id: str,
        tenant_id: str,
        subscription_id: str,
        period: BillingPeriod,
        lines: Sequence[InvoiceLine],
        currency: str = "USD",
        discounts: Decimal = Decimal("0.00"),
        taxes: Decimal = Decimal("0.00"),
        status: InvoiceStatus = InvoiceStatus.OPEN,
        issued_at: Optional[datetime] = None,
        due_at: Optional[datetime] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "Invoice":
        """Calcula subtotal y total automáticamente a partir de las líneas."""
        subtotal = Decimal("0.00")
        for line in lines:
            if line.currency != currency.strip().upper():
                raise CurrencyMismatchError(f"Line currency {line.currency} does not match invoice currency {currency}")
            subtotal += line.amount

        total = subtotal - discounts + taxes
        if total < Decimal("0.00"):
            total = Decimal("0.00")

        return cls(
            invoice_id=invoice_id,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            period=period,
            currency=currency,
            lines=tuple(lines),
            subtotal=subtotal,
            discounts=discounts,
            taxes=taxes,
            total=total,
            status=status,
            issued_at=issued_at or datetime.now(timezone.utc),
            due_at=due_at,
            metadata=metadata or {},
        )


@dataclass(frozen=True)
class PaymentAttempt:
    """
    Registro inmutable de un intento de cobro para una Invoice.
    """
    attempt_id: str
    tenant_id: str
    invoice_id: str
    amount: Decimal
    currency: str
    status: PaymentStatus
    provider: str
    idempotency_key: str
    subscription_id: Optional[str] = None
    provider_reference: Optional[str] = None
    error_message: Optional[str] = None
    attempted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.attempt_id, "attempt_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.invoice_id, "invoice_id")
        validate_safe_identifier(self.idempotency_key, "idempotency_key")

        if not isinstance(self.status, PaymentStatus):
            object.__setattr__(self, "status", PaymentStatus(self.status))

        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)))

        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "amount", self.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        object.__setattr__(self, "provider", self.provider.strip().lower())

        if self.attempted_at.tzinfo is None:
            object.__setattr__(self, "attempted_at", self.attempted_at.replace(tzinfo=timezone.utc))
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            object.__setattr__(self, "completed_at", self.completed_at.replace(tzinfo=timezone.utc))

        if not isinstance(self.metadata, MappingProxyType):
            sanitized = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", MappingProxyType(sanitized))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise BillingIntegrityError(
                f"PaymentAttempt checksum mismatch for attempt_id={self.attempt_id}: "
                f"provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        comp_str = self.completed_at.isoformat() if self.completed_at else "NONE"
        pref_str = self.provider_reference or "NONE"
        raw = (
            f"{self.attempt_id}|{self.tenant_id}|{self.invoice_id}|{self.amount}|"
            f"{self.currency}|{self.status.value}|{self.provider}|{self.idempotency_key}|"
            f"{pref_str}|{self.attempted_at.isoformat()}|{comp_str}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()


@dataclass(frozen=True)
class PaymentProviderEvent:
    """
    Evento inmutable proveniente de una pasarela de pago (webhook boundary).
    Permite procesamiento determinista e idempotente.
    """
    event_id: str
    provider: str
    event_type: str
    tenant_id: str
    idempotency_key: str
    subscription_id: Optional[str] = None
    invoice_id: Optional[str] = None
    provider_payment_id: Optional[str] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload_hash: str = field(default="")
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.event_id, "event_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.idempotency_key, "idempotency_key")

        object.__setattr__(self, "provider", self.provider.strip().lower())
        if self.currency:
            object.__setattr__(self, "currency", self.currency.strip().upper())
        if self.amount is not None and not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        if self.occurred_at.tzinfo is None:
            object.__setattr__(self, "occurred_at", self.occurred_at.replace(tzinfo=timezone.utc))

        if not isinstance(self.metadata, MappingProxyType):
            sanitized = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", MappingProxyType(sanitized))

        if not self.payload_hash:
            raw_meta = json.dumps(dict(self.metadata), sort_keys=True, ensure_ascii=False)
            object.__setattr__(self, "payload_hash", hashlib.sha256(raw_meta.encode("utf-8")).hexdigest())

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise BillingIntegrityError(
                f"PaymentProviderEvent checksum mismatch for event_id={self.event_id}: "
                f"provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        amt_str = str(self.amount) if self.amount is not None else "NONE"
        curr_str = self.currency or "NONE"
        inv_str = self.invoice_id or "NONE"
        sub_str = self.subscription_id or "NONE"
        pid_str = self.provider_payment_id or "NONE"
        raw = (
            f"{self.event_id}|{self.provider}|{self.event_type}|{self.tenant_id}|"
            f"{self.idempotency_key}|{sub_str}|{inv_str}|{pid_str}|{amt_str}|"
            f"{curr_str}|{self.occurred_at.isoformat()}|{self.payload_hash}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()
