"""
Adaptadores de Repositorio para Billing & Subscription SaaS (Hito O.9 — Billing & Subscription Management).

Define:
- InMemorySubscriptionRepository & JsonSubscriptionRepository
- InMemoryInvoiceRepository & JsonInvoiceRepository
- InMemoryPaymentAttemptRepository & JsonPaymentAttemptRepository
- InMemoryPaymentProviderEventRepository & JsonPaymentProviderEventRepository
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union

from src.domain.security.models import validate_safe_identifier
from src.domain.billing.models import (
    Subscription,
    SubscriptionStatus,
    BillingCycle,
    PaymentProviderReference,
    Invoice,
    InvoiceLine,
    InvoiceLineType,
    InvoiceStatus,
    BillingPeriod,
    PaymentAttempt,
    PaymentStatus,
    PaymentProviderEvent,
    BillingIntegrityError,
)
from src.domain.billing.ports import (
    SubscriptionRepositoryPort,
    InvoiceRepositoryPort,
    PaymentAttemptRepositoryPort,
    PaymentProviderEventRepositoryPort,
)


class InMemorySubscriptionRepository(SubscriptionRepositoryPort):
    """Repositorio en memoria thread-safe de suscripciones particionado por tenant."""

    def __init__(self):
        self._lock = threading.RLock()
        # tenant_id -> list of Subscription
        self._subscriptions_by_tenant: Dict[str, List[Subscription]] = {}

    def save_subscription(self, subscription: Subscription) -> None:
        validate_safe_identifier(subscription.tenant_id, "tenant_id")
        if not subscription.verify_integrity():
            raise BillingIntegrityError(f"Subscription checksum validation failed for {subscription.subscription_id}")
        with self._lock:
            t_list = self._subscriptions_by_tenant.setdefault(subscription.tenant_id, [])
            idx = next((i for i, s in enumerate(t_list) if s.subscription_id == subscription.subscription_id), None)
            if idx is not None:
                t_list[idx] = subscription
            else:
                t_list.append(subscription)

    def get_subscription(self, subscription_id: str) -> Optional[Subscription]:
        validate_safe_identifier(subscription_id, "subscription_id")
        with self._lock:
            for t_list in self._subscriptions_by_tenant.values():
                for s in t_list:
                    if s.subscription_id == subscription_id:
                        return s
            return None

    def get_active_subscription(self, tenant_id: str, current_time: Optional[datetime] = None) -> Optional[Subscription]:
        validate_safe_identifier(tenant_id, "tenant_id")
        check_time = current_time or datetime.now(timezone.utc)
        if check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=timezone.utc)
        with self._lock:
            t_list = self._subscriptions_by_tenant.get(tenant_id, [])
            active = [s for s in t_list if s.is_active and s.current_period_start <= check_time <= s.current_period_end]
            if not active:
                # Si no hay dentro del rango exacto, buscar cualquier ACTIVE vigente
                active = [s for s in t_list if s.is_active]
            if not active:
                return None
            return sorted(active, key=lambda s: s.created_at, reverse=True)[0]

    def list_subscriptions_for_tenant(self, tenant_id: str) -> List[Subscription]:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            t_list = self._subscriptions_by_tenant.get(tenant_id, [])
            return sorted(t_list, key=lambda s: s.created_at, reverse=True)


class InMemoryInvoiceRepository(InvoiceRepositoryPort):
    """Repositorio en memoria thread-safe de facturas particionado por tenant."""

    def __init__(self):
        self._lock = threading.RLock()
        # tenant_id -> list of Invoice
        self._invoices_by_tenant: Dict[str, List[Invoice]] = {}

    def save_invoice(self, invoice: Invoice) -> None:
        validate_safe_identifier(invoice.tenant_id, "tenant_id")
        if not invoice.verify_integrity():
            raise BillingIntegrityError(f"Invoice checksum validation failed for {invoice.invoice_id}")
        with self._lock:
            t_list = self._invoices_by_tenant.setdefault(invoice.tenant_id, [])
            idx = next((i for i, inv in enumerate(t_list) if inv.invoice_id == invoice.invoice_id), None)
            if idx is not None:
                t_list[idx] = invoice
            else:
                t_list.append(invoice)

    def get_invoice(self, invoice_id: str) -> Optional[Invoice]:
        validate_safe_identifier(invoice_id, "invoice_id")
        with self._lock:
            for t_list in self._invoices_by_tenant.values():
                for inv in t_list:
                    if inv.invoice_id == invoice_id:
                        return inv
            return None

    def get_invoice_by_period(self, tenant_id: str, subscription_id: str, period: BillingPeriod) -> Optional[Invoice]:
        validate_safe_identifier(tenant_id, "tenant_id")
        validate_safe_identifier(subscription_id, "subscription_id")
        with self._lock:
            t_list = self._invoices_by_tenant.get(tenant_id, [])
            for inv in t_list:
                if (
                    inv.subscription_id == subscription_id
                    and inv.period.start_time == period.start_time
                    and inv.period.end_time == period.end_time
                ):
                    return inv
            return None

    def list_invoices_for_tenant(self, tenant_id: str, status: Optional[InvoiceStatus] = None) -> List[Invoice]:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            t_list = self._invoices_by_tenant.get(tenant_id, [])
            if status is not None:
                t_list = [inv for inv in t_list if inv.status == status]
            return sorted(t_list, key=lambda inv: inv.issued_at, reverse=True)


class InMemoryPaymentAttemptRepository(PaymentAttemptRepositoryPort):
    """Repositorio en memoria thread-safe de intentos de cobro particionado por tenant."""

    def __init__(self):
        self._lock = threading.RLock()
        # tenant_id -> list of PaymentAttempt
        self._attempts_by_tenant: Dict[str, List[PaymentAttempt]] = {}

    def save_attempt(self, attempt: PaymentAttempt) -> None:
        validate_safe_identifier(attempt.tenant_id, "tenant_id")
        if not attempt.verify_integrity():
            raise BillingIntegrityError(f"PaymentAttempt checksum validation failed for {attempt.attempt_id}")
        with self._lock:
            t_list = self._attempts_by_tenant.setdefault(attempt.tenant_id, [])
            idx = next((i for i, a in enumerate(t_list) if a.attempt_id == attempt.attempt_id), None)
            if idx is not None:
                t_list[idx] = attempt
            else:
                t_list.append(attempt)

    def get_attempt(self, attempt_id: str) -> Optional[PaymentAttempt]:
        validate_safe_identifier(attempt_id, "attempt_id")
        with self._lock:
            for t_list in self._attempts_by_tenant.values():
                for a in t_list:
                    if a.attempt_id == attempt_id:
                        return a
            return None

    def get_attempt_by_idempotency_key(self, tenant_id: str, idempotency_key: str) -> Optional[PaymentAttempt]:
        validate_safe_identifier(tenant_id, "tenant_id")
        validate_safe_identifier(idempotency_key, "idempotency_key")
        with self._lock:
            t_list = self._attempts_by_tenant.get(tenant_id, [])
            for a in t_list:
                if a.idempotency_key == idempotency_key:
                    return a
            return None

    def list_attempts_for_invoice(self, tenant_id: str, invoice_id: str) -> List[PaymentAttempt]:
        validate_safe_identifier(tenant_id, "tenant_id")
        validate_safe_identifier(invoice_id, "invoice_id")
        with self._lock:
            t_list = self._attempts_by_tenant.get(tenant_id, [])
            filtered = [a for a in t_list if a.invoice_id == invoice_id]
            return sorted(filtered, key=lambda a: a.attempted_at, reverse=True)


class InMemoryPaymentProviderEventRepository(PaymentProviderEventRepositoryPort):
    """Repositorio en memoria de eventos procesados para idempotencia."""

    def __init__(self):
        self._lock = threading.RLock()
        self._events_by_id: Dict[str, PaymentProviderEvent] = {}

    def save_event(self, event: PaymentProviderEvent) -> None:
        if not event.verify_integrity():
            raise BillingIntegrityError(f"PaymentProviderEvent checksum validation failed for {event.event_id}")
        with self._lock:
            self._events_by_id[event.event_id] = event

    def get_event(self, event_id: str) -> Optional[PaymentProviderEvent]:
        with self._lock:
            return self._events_by_id.get(event_id)

    def is_event_processed(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._events_by_id


# ==========================================
# Repositorios JSON en Disco (Restart-Safe)
# ==========================================

class JsonSubscriptionRepository(SubscriptionRepositoryPort):
    """
    Repositorio JSON en disco para suscripciones tenant-scoped:
    `base_dir / "tenants" / {tenant_id} / "billing" / "subscriptions.json"`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_file(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        t_dir = self.base_dir / "tenants" / tenant_id / "billing"
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir / "subscriptions.json"

    def _sub_to_dict(self, s: Subscription) -> Dict[str, Any]:
        prov_dict = None
        if s.provider_reference:
            prov_dict = {
                "provider": s.provider_reference.provider,
                "reference_id": s.provider_reference.reference_id,
                "customer_reference": s.provider_reference.customer_reference,
                "checkout_url": s.provider_reference.checkout_url,
            }

        return {
            "subscription_id": s.subscription_id,
            "tenant_id": s.tenant_id,
            "plan_id": s.plan_id,
            "plan_version": s.plan_version,
            "status": s.status.value,
            "billing_cycle": s.billing_cycle.value,
            "current_period_start": s.current_period_start.isoformat(),
            "current_period_end": s.current_period_end.isoformat(),
            "base_price": str(s.base_price),
            "currency": s.currency,
            "cancel_at_period_end": s.cancel_at_period_end,
            "canceled_at": s.canceled_at.isoformat() if s.canceled_at else None,
            "provider_reference": prov_dict,
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat(),
            "metadata": dict(s.metadata),
            "checksum": s.checksum,
        }

    def _dict_to_sub(self, d: Dict[str, Any]) -> Subscription:
        prov_ref = None
        if d.get("provider_reference"):
            p_data = d["provider_reference"]
            prov_ref = PaymentProviderReference(
                provider=p_data["provider"],
                reference_id=p_data["reference_id"],
                customer_reference=p_data.get("customer_reference"),
                checkout_url=p_data.get("checkout_url"),
            )

        canc_at = datetime.fromisoformat(d["canceled_at"]) if d.get("canceled_at") else None

        return Subscription(
            subscription_id=d["subscription_id"],
            tenant_id=d["tenant_id"],
            plan_id=d["plan_id"],
            plan_version=d["plan_version"],
            status=SubscriptionStatus(d["status"]),
            billing_cycle=BillingCycle(d["billing_cycle"]),
            current_period_start=datetime.fromisoformat(d["current_period_start"]),
            current_period_end=datetime.fromisoformat(d["current_period_end"]),
            base_price=Decimal(d["base_price"]),
            currency=d.get("currency", "USD"),
            cancel_at_period_end=d.get("cancel_at_period_end", False),
            canceled_at=canc_at,
            provider_reference=prov_ref,
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
            metadata=d.get("metadata", {}),
            checksum=d.get("checksum", ""),
        )

    def _read_tenant_subs(self, tenant_id: str) -> List[Subscription]:
        f = self._get_file(tenant_id)
        if not f.exists():
            return []
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return [self._dict_to_sub(item) for item in data]
        except Exception:
            return []

    def _write_tenant_subs(self, tenant_id: str, subs: List[Subscription]) -> None:
        f = self._get_file(tenant_id)
        data = [self._sub_to_dict(s) for s in subs]
        tmp_file = f.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
        tmp_file.replace(f)

    def save_subscription(self, subscription: Subscription) -> None:
        validate_safe_identifier(subscription.tenant_id, "tenant_id")
        if not subscription.verify_integrity():
            raise BillingIntegrityError(f"Subscription checksum validation failed for {subscription.subscription_id}")
        with self._lock:
            subs = self._read_tenant_subs(subscription.tenant_id)
            idx = next((i for i, s in enumerate(subs) if s.subscription_id == subscription.subscription_id), None)
            if idx is not None:
                subs[idx] = subscription
            else:
                subs.append(subscription)
            self._write_tenant_subs(subscription.tenant_id, subs)

    def get_subscription(self, subscription_id: str) -> Optional[Subscription]:
        validate_safe_identifier(subscription_id, "subscription_id")
        with self._lock:
            tenants_dir = self.base_dir / "tenants"
            if not tenants_dir.exists():
                return None
            for t_path in tenants_dir.iterdir():
                if t_path.is_dir():
                    subs = self._read_tenant_subs(t_path.name)
                    for s in subs:
                        if s.subscription_id == subscription_id:
                            return s
            return None

    def get_active_subscription(self, tenant_id: str, current_time: Optional[datetime] = None) -> Optional[Subscription]:
        validate_safe_identifier(tenant_id, "tenant_id")
        check_time = current_time or datetime.now(timezone.utc)
        if check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=timezone.utc)
        with self._lock:
            subs = self._read_tenant_subs(tenant_id)
            active = [s for s in subs if s.is_active and s.current_period_start <= check_time <= s.current_period_end]
            if not active:
                active = [s for s in subs if s.is_active]
            if not active:
                return None
            return sorted(active, key=lambda s: s.created_at, reverse=True)[0]

    def list_subscriptions_for_tenant(self, tenant_id: str) -> List[Subscription]:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            subs = self._read_tenant_subs(tenant_id)
            return sorted(subs, key=lambda s: s.created_at, reverse=True)


class JsonInvoiceRepository(InvoiceRepositoryPort):
    """
    Repositorio JSON en disco para facturas tenant-scoped:
    `base_dir / "tenants" / {tenant_id} / "billing" / "invoices.json"`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_file(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        t_dir = self.base_dir / "tenants" / tenant_id / "billing"
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir / "invoices.json"

    def _inv_to_dict(self, inv: Invoice) -> Dict[str, Any]:
        lines_data = []
        for line in inv.lines:
            lines_data.append({
                "line_id": line.line_id,
                "line_type": line.line_type.value,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "amount": str(line.amount),
                "currency": line.currency,
                "metadata": dict(line.metadata),
            })

        return {
            "invoice_id": inv.invoice_id,
            "tenant_id": inv.tenant_id,
            "subscription_id": inv.subscription_id,
            "period": {
                "start_time": inv.period.start_time.isoformat(),
                "end_time": inv.period.end_time.isoformat(),
            },
            "currency": inv.currency,
            "lines": lines_data,
            "subtotal": str(inv.subtotal),
            "discounts": str(inv.discounts),
            "taxes": str(inv.taxes),
            "total": str(inv.total),
            "status": inv.status.value,
            "issued_at": inv.issued_at.isoformat(),
            "due_at": inv.due_at.isoformat() if inv.due_at else None,
            "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
            "metadata": dict(inv.metadata),
            "checksum": inv.checksum,
        }

    def _dict_to_inv(self, d: Dict[str, Any]) -> Invoice:
        lines = []
        for l_data in d.get("lines", []):
            lines.append(InvoiceLine(
                line_id=l_data["line_id"],
                line_type=InvoiceLineType(l_data["line_type"]),
                description=l_data["description"],
                quantity=Decimal(l_data["quantity"]),
                unit_price=Decimal(l_data["unit_price"]),
                amount=Decimal(l_data["amount"]),
                currency=l_data.get("currency", "USD"),
                metadata=l_data.get("metadata", {}),
            ))

        period = BillingPeriod(
            start_time=datetime.fromisoformat(d["period"]["start_time"]),
            end_time=datetime.fromisoformat(d["period"]["end_time"]),
        )

        due_at = datetime.fromisoformat(d["due_at"]) if d.get("due_at") else None
        paid_at = datetime.fromisoformat(d["paid_at"]) if d.get("paid_at") else None

        return Invoice(
            invoice_id=d["invoice_id"],
            tenant_id=d["tenant_id"],
            subscription_id=d["subscription_id"],
            period=period,
            currency=d["currency"],
            lines=tuple(lines),
            subtotal=Decimal(d["subtotal"]),
            discounts=Decimal(d.get("discounts", "0.00")),
            taxes=Decimal(d.get("taxes", "0.00")),
            total=Decimal(d["total"]),
            status=InvoiceStatus(d["status"]),
            issued_at=datetime.fromisoformat(d["issued_at"]),
            due_at=due_at,
            paid_at=paid_at,
            metadata=d.get("metadata", {}),
            checksum=d.get("checksum", ""),
        )

    def _read_tenant_invoices(self, tenant_id: str) -> List[Invoice]:
        f = self._get_file(tenant_id)
        if not f.exists():
            return []
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return [self._dict_to_inv(item) for item in data]
        except Exception:
            return []

    def _write_tenant_invoices(self, tenant_id: str, invoices: List[Invoice]) -> None:
        f = self._get_file(tenant_id)
        data = [self._inv_to_dict(inv) for inv in invoices]
        tmp_file = f.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
        tmp_file.replace(f)

    def save_invoice(self, invoice: Invoice) -> None:
        validate_safe_identifier(invoice.tenant_id, "tenant_id")
        if not invoice.verify_integrity():
            raise BillingIntegrityError(f"Invoice checksum validation failed for {invoice.invoice_id}")
        with self._lock:
            invoices = self._read_tenant_invoices(invoice.tenant_id)
            idx = next((i for i, inv in enumerate(invoices) if inv.invoice_id == invoice.invoice_id), None)
            if idx is not None:
                invoices[idx] = invoice
            else:
                invoices.append(invoice)
            self._write_tenant_invoices(invoice.tenant_id, invoices)

    def get_invoice(self, invoice_id: str) -> Optional[Invoice]:
        validate_safe_identifier(invoice_id, "invoice_id")
        with self._lock:
            tenants_dir = self.base_dir / "tenants"
            if not tenants_dir.exists():
                return None
            for t_path in tenants_dir.iterdir():
                if t_path.is_dir():
                    invoices = self._read_tenant_invoices(t_path.name)
                    for inv in invoices:
                        if inv.invoice_id == invoice_id:
                            return inv
            return None

    def get_invoice_by_period(self, tenant_id: str, subscription_id: str, period: BillingPeriod) -> Optional[Invoice]:
        validate_safe_identifier(tenant_id, "tenant_id")
        validate_safe_identifier(subscription_id, "subscription_id")
        with self._lock:
            invoices = self._read_tenant_invoices(tenant_id)
            for inv in invoices:
                if (
                    inv.subscription_id == subscription_id
                    and inv.period.start_time == period.start_time
                    and inv.period.end_time == period.end_time
                ):
                    return inv
            return None

    def list_invoices_for_tenant(self, tenant_id: str, status: Optional[InvoiceStatus] = None) -> List[Invoice]:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            invoices = self._read_tenant_invoices(tenant_id)
            if status is not None:
                invoices = [inv for inv in invoices if inv.status == status]
            return sorted(invoices, key=lambda inv: inv.issued_at, reverse=True)


class JsonPaymentAttemptRepository(PaymentAttemptRepositoryPort):
    """
    Repositorio JSON en disco para intentos de pago tenant-scoped:
    `base_dir / "tenants" / {tenant_id} / "billing" / "payments.json"`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_file(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        t_dir = self.base_dir / "tenants" / tenant_id / "billing"
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir / "payments.json"

    def _att_to_dict(self, a: PaymentAttempt) -> Dict[str, Any]:
        return {
            "attempt_id": a.attempt_id,
            "tenant_id": a.tenant_id,
            "invoice_id": a.invoice_id,
            "amount": str(a.amount),
            "currency": a.currency,
            "status": a.status.value,
            "provider": a.provider,
            "idempotency_key": a.idempotency_key,
            "subscription_id": a.subscription_id,
            "provider_reference": a.provider_reference,
            "error_message": a.error_message,
            "attempted_at": a.attempted_at.isoformat(),
            "completed_at": a.completed_at.isoformat() if a.completed_at else None,
            "metadata": dict(a.metadata),
            "checksum": a.checksum,
        }

    def _dict_to_att(self, d: Dict[str, Any]) -> PaymentAttempt:
        comp_at = datetime.fromisoformat(d["completed_at"]) if d.get("completed_at") else None
        return PaymentAttempt(
            attempt_id=d["attempt_id"],
            tenant_id=d["tenant_id"],
            invoice_id=d["invoice_id"],
            amount=Decimal(d["amount"]),
            currency=d["currency"],
            status=PaymentStatus(d["status"]),
            provider=d["provider"],
            idempotency_key=d["idempotency_key"],
            subscription_id=d.get("subscription_id"),
            provider_reference=d.get("provider_reference"),
            error_message=d.get("error_message"),
            attempted_at=datetime.fromisoformat(d["attempted_at"]),
            completed_at=comp_at,
            metadata=d.get("metadata", {}),
            checksum=d.get("checksum", ""),
        )

    def _read_tenant_attempts(self, tenant_id: str) -> List[PaymentAttempt]:
        f = self._get_file(tenant_id)
        if not f.exists():
            return []
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return [self._dict_to_att(item) for item in data]
        except Exception:
            return []

    def _write_tenant_attempts(self, tenant_id: str, attempts: List[PaymentAttempt]) -> None:
        f = self._get_file(tenant_id)
        data = [self._att_to_dict(a) for a in attempts]
        tmp_file = f.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
        tmp_file.replace(f)

    def save_attempt(self, attempt: PaymentAttempt) -> None:
        validate_safe_identifier(attempt.tenant_id, "tenant_id")
        if not attempt.verify_integrity():
            raise BillingIntegrityError(f"PaymentAttempt checksum validation failed for {attempt.attempt_id}")
        with self._lock:
            attempts = self._read_tenant_attempts(attempt.tenant_id)
            idx = next((i for i, a in enumerate(attempts) if a.attempt_id == attempt.attempt_id), None)
            if idx is not None:
                attempts[idx] = attempt
            else:
                attempts.append(attempt)
            self._write_tenant_attempts(attempt.tenant_id, attempts)

    def get_attempt(self, attempt_id: str) -> Optional[PaymentAttempt]:
        validate_safe_identifier(attempt_id, "attempt_id")
        with self._lock:
            tenants_dir = self.base_dir / "tenants"
            if not tenants_dir.exists():
                return None
            for t_path in tenants_dir.iterdir():
                if t_path.is_dir():
                    attempts = self._read_tenant_attempts(t_path.name)
                    for a in attempts:
                        if a.attempt_id == attempt_id:
                            return a
            return None

    def get_attempt_by_idempotency_key(self, tenant_id: str, idempotency_key: str) -> Optional[PaymentAttempt]:
        validate_safe_identifier(tenant_id, "tenant_id")
        validate_safe_identifier(idempotency_key, "idempotency_key")
        with self._lock:
            attempts = self._read_tenant_attempts(tenant_id)
            for a in attempts:
                if a.idempotency_key == idempotency_key:
                    return a
            return None

    def list_attempts_for_invoice(self, tenant_id: str, invoice_id: str) -> List[PaymentAttempt]:
        validate_safe_identifier(tenant_id, "tenant_id")
        validate_safe_identifier(invoice_id, "invoice_id")
        with self._lock:
            attempts = self._read_tenant_attempts(tenant_id)
            filtered = [a for a in attempts if a.invoice_id == invoice_id]
            return sorted(filtered, key=lambda a: a.attempted_at, reverse=True)


class JsonPaymentProviderEventRepository(PaymentProviderEventRepositoryPort):
    """
    Repositorio JSON en disco para eventos de pasarela procesados (idempotencia global):
    `base_dir / "billing" / "events.json"`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_file(self) -> Path:
        b_dir = self.base_dir / "billing"
        b_dir.mkdir(parents=True, exist_ok=True)
        return b_dir / "events.json"

    def _ev_to_dict(self, ev: PaymentProviderEvent) -> Dict[str, Any]:
        return {
            "event_id": ev.event_id,
            "provider": ev.provider,
            "event_type": ev.event_type,
            "tenant_id": ev.tenant_id,
            "idempotency_key": ev.idempotency_key,
            "subscription_id": ev.subscription_id,
            "invoice_id": ev.invoice_id,
            "provider_payment_id": ev.provider_payment_id,
            "amount": str(ev.amount) if ev.amount is not None else None,
            "currency": ev.currency,
            "occurred_at": ev.occurred_at.isoformat(),
            "payload_hash": ev.payload_hash,
            "metadata": dict(ev.metadata),
            "checksum": ev.checksum,
        }

    def _dict_to_ev(self, d: Dict[str, Any]) -> PaymentProviderEvent:
        amt = Decimal(d["amount"]) if d.get("amount") is not None else None
        return PaymentProviderEvent(
            event_id=d["event_id"],
            provider=d["provider"],
            event_type=d["event_type"],
            tenant_id=d["tenant_id"],
            idempotency_key=d["idempotency_key"],
            subscription_id=d.get("subscription_id"),
            invoice_id=d.get("invoice_id"),
            provider_payment_id=d.get("provider_payment_id"),
            amount=amt,
            currency=d.get("currency"),
            occurred_at=datetime.fromisoformat(d["occurred_at"]),
            payload_hash=d.get("payload_hash", ""),
            metadata=d.get("metadata", {}),
            checksum=d.get("checksum", ""),
        )

    def _read_all(self) -> List[PaymentProviderEvent]:
        f = self._get_file()
        if not f.exists():
            return []
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return [self._dict_to_ev(item) for item in data]
        except Exception:
            return []

    def _write_all(self, events: List[PaymentProviderEvent]) -> None:
        f = self._get_file()
        data = [self._ev_to_dict(ev) for ev in events]
        tmp_file = f.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
        tmp_file.replace(f)

    def save_event(self, event: PaymentProviderEvent) -> None:
        if not event.verify_integrity():
            raise BillingIntegrityError(f"PaymentProviderEvent checksum validation failed for {event.event_id}")
        with self._lock:
            events = self._read_all()
            idx = next((i for i, ev in enumerate(events) if ev.event_id == event.event_id), None)
            if idx is not None:
                events[idx] = event
            else:
                events.append(event)
            self._write_all(events)

    def get_event(self, event_id: str) -> Optional[PaymentProviderEvent]:
        with self._lock:
            events = self._read_all()
            for ev in events:
                if ev.event_id == event_id:
                    return ev
            return None

    def is_event_processed(self, event_id: str) -> bool:
        with self._lock:
            events = self._read_all()
            return any(ev.event_id == event_id for ev in events)
