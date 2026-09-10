"""
Servicio de Aplicación para Facturación y Pagos SaaS (Hito O.9 — Billing & Subscription Management).

Responsabilidades:
- Implementar BillingServicePort.
- Generar Invoices deterministas e idempotentes por ciclo de facturación.
- Procesar cobros de facturas mediante PaymentProviderPort desacoplado.
- Manejar eventos de webhook de pasarelas de forma determinista e idempotente.
- Coordinar transiciones de estado de cobro (PaymentAttempt, Invoice, Subscription).
- Mantener estricto aislamiento multi-tenant (O.1).
- Emitir trazas (K.2) y auditoría (K.1) seguras sin secretos ni datos sensibles.
- Operar con precisión financiera en Decimal (Money).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import threading
from typing import Optional, List, Dict, Any, Mapping, Tuple
import uuid

from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.domain.billing.models import (
    Subscription,
    SubscriptionStatus,
    Invoice,
    InvoiceStatus,
    InvoiceLine,
    InvoiceLineType,
    PaymentAttempt,
    PaymentStatus,
    PaymentProviderEvent,
    BillingPeriod,
    Money,
    SubscriptionNotFoundError,
    InvoiceNotFoundError,
    DuplicateBillingEventError,
    InvalidSubscriptionStateError,
    BillingTenantIsolationError,
    BillingIntegrityError,
    PaymentProviderError,
)
from src.domain.billing.ports import (
    SubscriptionRepositoryPort,
    InvoiceRepositoryPort,
    PaymentAttemptRepositoryPort,
    PaymentProviderEventRepositoryPort,
    PaymentProviderPort,
    BillingServicePort,
    SubscriptionServicePort,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort


class BillingService(BillingServicePort):
    """
    Servicio central de facturación comercial e integración de cobros SaaS.
    """

    def __init__(
        self,
        subscription_repository: SubscriptionRepositoryPort,
        invoice_repository: InvoiceRepositoryPort,
        payment_attempt_repository: PaymentAttemptRepositoryPort,
        event_repository: PaymentProviderEventRepositoryPort,
        payment_provider: PaymentProviderPort,
        subscription_service: Optional[SubscriptionServicePort] = None,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        agent_trace_repository: Optional[AgentTraceRepositoryPort] = None,
    ):
        self.subscription_repository = subscription_repository
        self.invoice_repository = invoice_repository
        self.payment_attempt_repository = payment_attempt_repository
        self.event_repository = event_repository
        self.payment_provider = payment_provider
        self.subscription_service = subscription_service
        self.clock = clock
        self.audit_repository = audit_repository
        self.agent_trace_repository = agent_trace_repository
        self._lock = threading.RLock()

    def set_subscription_service(self, subscription_service: SubscriptionServicePort) -> None:
        """Permite inyectar SubscriptionService de forma circular controlada."""
        with self._lock:
            self.subscription_service = subscription_service

    def _get_now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def _emit_audit(
        self,
        record_type: AuditRecordType,
        tenant_id: str,
        actor_id: Optional[str],
        action: str,
        status: str,
        details: Mapping[str, Any],
        correlation_id: Optional[str] = None,
    ) -> None:
        if self.audit_repository is None:
            return
        try:
            rec = AuditRecord(
                audit_id=f"aud_bill_{uuid.uuid4().hex[:12]}",
                occurred_at=self._get_now(),
                actor=AuditActor(
                    actor_type=AuditActorType.USER if actor_id else AuditActorType.SYSTEM,
                    actor_id=actor_id or tenant_id,
                ),
                record_type=record_type,
                subject_type="BILLING",
                subject_id=tenant_id,
                action_or_operation=action,
                status=status,
                correlation_id=correlation_id or f"corr_{uuid.uuid4().hex[:8]}",
                metadata=dict(details),
            )
            self.audit_repository.append(rec)
        except Exception:
            pass

    def generate_period_invoice(
        self,
        subscription_id: str,
        period: Optional[BillingPeriod] = None,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Invoice:
        """
        Genera la factura determinista de un período para una suscripción.
        IDEMPOTENCIA: Si ya existe una factura para esa suscripción y período, retorna la existente.
        """
        with self._lock:
            sub = self.subscription_repository.get_subscription(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"Subscription '{subscription_id}' not found.")

            if context is not None:
                CrossTenantGuard.ensure_tenant_context(context)
                CrossTenantGuard.assert_same_tenant(context, sub.tenant_id, operation_name="generate_period_invoice")

            target_period = period or sub.current_period

            # Verificar si ya existe factura emitida para este período
            existing_invoice = self.invoice_repository.get_invoice_by_period(
                tenant_id=sub.tenant_id,
                subscription_id=sub.subscription_id,
                period=target_period,
            )
            if existing_invoice is not None:
                return existing_invoice

            now = self._get_now()
            # Crear línea BASE_PLAN
            base_line = InvoiceLine(
                line_id=f"line_{uuid.uuid4().hex[:12]}",
                line_type=InvoiceLineType.BASE_PLAN,
                description=f"Subscription Plan: {sub.plan_id} (Cycle: {sub.billing_cycle.value})",
                quantity=Decimal("1.00"),
                unit_price=sub.base_price,
                amount=sub.base_price,
                currency=sub.currency,
            )

            invoice_id = f"inv_{uuid.uuid4().hex[:16]}"
            due_at = now + timedelta(days=14)

            # Si el monto es 0 (Plan Free), se marca automáticamente como PAID
            init_status = InvoiceStatus.PAID if sub.base_price == Decimal("0.00") else InvoiceStatus.OPEN
            paid_at = now if init_status == InvoiceStatus.PAID else None

            invoice = Invoice.create_deterministic(
                invoice_id=invoice_id,
                tenant_id=sub.tenant_id,
                subscription_id=sub.subscription_id,
                period=target_period,
                lines=[base_line],
                currency=sub.currency,
                status=init_status,
                issued_at=now,
                due_at=due_at,
            )
            if paid_at:
                invoice = Invoice(
                    invoice_id=invoice.invoice_id,
                    tenant_id=invoice.tenant_id,
                    subscription_id=invoice.subscription_id,
                    period=invoice.period,
                    currency=invoice.currency,
                    lines=invoice.lines,
                    subtotal=invoice.subtotal,
                    total=invoice.total,
                    status=InvoiceStatus.PAID,
                    discounts=invoice.discounts,
                    taxes=invoice.taxes,
                    issued_at=invoice.issued_at,
                    due_at=invoice.due_at,
                    paid_at=paid_at,
                    metadata=invoice.metadata,
                )

            self.invoice_repository.save_invoice(invoice)

            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=sub.tenant_id,
                actor_id=actor_id,
                action="INVOICE_ISSUED",
                status=invoice.status.value,
                details={
                    "invoice_id": invoice_id,
                    "subscription_id": sub.subscription_id,
                    "total": str(invoice.total),
                    "currency": invoice.currency,
                },
            )

            return invoice

    def process_payment(
        self,
        invoice_id: str,
        idempotency_key: str,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> PaymentAttempt:
        """
        Ejecuta el cobro de una factura a través de la pasarela configurada.
        IDEMPOTENCIA: Si la clave de idempotencia ya fue procesada para este tenant, retorna el intento previo.
        """
        with self._lock:
            invoice = self.invoice_repository.get_invoice(invoice_id)
            if invoice is None:
                raise InvoiceNotFoundError(f"Invoice '{invoice_id}' not found.")

            if context is not None:
                CrossTenantGuard.ensure_tenant_context(context)
                CrossTenantGuard.assert_same_tenant(context, invoice.tenant_id, operation_name="process_payment")

            # 1. Comprobar intento previo por clave de idempotencia
            existing_attempt = self.payment_attempt_repository.get_attempt_by_idempotency_key(
                tenant_id=invoice.tenant_id,
                idempotency_key=idempotency_key,
            )
            if existing_attempt is not None:
                return existing_attempt

            # 2. Si la factura ya está pagada, no cobrar doble
            if invoice.status == InvoiceStatus.PAID:
                attempt = PaymentAttempt(
                    attempt_id=f"payatt_{uuid.uuid4().hex[:16]}",
                    tenant_id=invoice.tenant_id,
                    invoice_id=invoice.invoice_id,
                    subscription_id=invoice.subscription_id,
                    amount=invoice.total,
                    currency=invoice.currency,
                    status=PaymentStatus.SUCCEEDED,
                    provider="internal_idempotency",
                    idempotency_key=idempotency_key,
                    error_message="Invoice already paid.",
                )
                self.payment_attempt_repository.save_attempt(attempt)
                return attempt

            # 3. Invocar al PaymentProviderPort desacoplado
            attempt = self.payment_provider.charge_invoice(
                tenant_id=invoice.tenant_id,
                invoice_id=invoice.invoice_id,
                amount=invoice.total,
                currency=invoice.currency,
                idempotency_key=idempotency_key,
            )

            # Completar suscripción si está presente en el attempt
            attempt = PaymentAttempt(
                attempt_id=attempt.attempt_id,
                tenant_id=attempt.tenant_id,
                invoice_id=attempt.invoice_id,
                amount=attempt.amount,
                currency=attempt.currency,
                status=attempt.status,
                provider=attempt.provider,
                idempotency_key=attempt.idempotency_key,
                subscription_id=invoice.subscription_id,
                provider_reference=attempt.provider_reference,
                error_message=attempt.error_message,
                attempted_at=attempt.attempted_at,
                completed_at=attempt.completed_at,
                metadata=attempt.metadata,
            )

            self.payment_attempt_repository.save_attempt(attempt)

            now = self._get_now()
            # 4. Transicionar estado de factura y suscripción según resultado
            if attempt.status == PaymentStatus.SUCCEEDED:
                paid_invoice = Invoice(
                    invoice_id=invoice.invoice_id,
                    tenant_id=invoice.tenant_id,
                    subscription_id=invoice.subscription_id,
                    period=invoice.period,
                    currency=invoice.currency,
                    lines=invoice.lines,
                    subtotal=invoice.subtotal,
                    total=invoice.total,
                    status=InvoiceStatus.PAID,
                    discounts=invoice.discounts,
                    taxes=invoice.taxes,
                    issued_at=invoice.issued_at,
                    due_at=invoice.due_at,
                    paid_at=now,
                    metadata=invoice.metadata,
                )
                self.invoice_repository.save_invoice(paid_invoice)

                # Activar suscripción si existe servicio
                if self.subscription_service is not None:
                    self.subscription_service.activate_subscription(
                        subscription_id=invoice.subscription_id,
                        actor_id=actor_id,
                        context=context,
                    )

            elif attempt.status == PaymentStatus.FAILED:
                # Si falló, la suscripción pasa a PAST_DUE
                sub = self.subscription_repository.get_subscription(invoice.subscription_id)
                if sub and sub.status != SubscriptionStatus.CANCELED:
                    past_due_sub = Subscription(
                        subscription_id=sub.subscription_id,
                        tenant_id=sub.tenant_id,
                        plan_id=sub.plan_id,
                        plan_version=sub.plan_version,
                        status=SubscriptionStatus.PAST_DUE,
                        billing_cycle=sub.billing_cycle,
                        current_period_start=sub.current_period_start,
                        current_period_end=sub.current_period_end,
                        base_price=sub.base_price,
                        currency=sub.currency,
                        cancel_at_period_end=sub.cancel_at_period_end,
                        canceled_at=sub.canceled_at,
                        provider_reference=sub.provider_reference,
                        created_at=sub.created_at,
                        updated_at=now,
                        metadata=sub.metadata,
                    )
                    self.subscription_repository.save_subscription(past_due_sub)

            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=invoice.tenant_id,
                actor_id=actor_id,
                action="PAYMENT_ATTEMPTED",
                status=attempt.status.value,
                details={
                    "attempt_id": attempt.attempt_id,
                    "invoice_id": invoice.invoice_id,
                    "amount": str(attempt.amount),
                    "currency": attempt.currency,
                    "status": attempt.status.value,
                },
            )

            return attempt

    def process_provider_event(
        self,
        event: PaymentProviderEvent,
    ) -> None:
        """
        Procesa un evento de webhook de la pasarela de forma determinista e idempotente.
        """
        with self._lock:
            # 1. Comprobar si el evento ya fue procesado
            if self.event_repository.is_event_processed(event.event_id):
                return

            # 2. Validar evento de pago exitoso o fallido
            if event.invoice_id is not None:
                invoice = self.invoice_repository.get_invoice(event.invoice_id)
                if invoice is not None and invoice.tenant_id == event.tenant_id:
                    now = self._get_now()
                    if event.event_type in ("payment.succeeded", "charge.succeeded", "payment_intent.succeeded"):
                        if invoice.status != InvoiceStatus.PAID:
                            paid_inv = Invoice(
                                invoice_id=invoice.invoice_id,
                                tenant_id=invoice.tenant_id,
                                subscription_id=invoice.subscription_id,
                                period=invoice.period,
                                currency=invoice.currency,
                                lines=invoice.lines,
                                subtotal=invoice.subtotal,
                                total=invoice.total,
                                status=InvoiceStatus.PAID,
                                discounts=invoice.discounts,
                                taxes=invoice.taxes,
                                issued_at=invoice.issued_at,
                                due_at=invoice.due_at,
                                paid_at=now,
                                metadata=invoice.metadata,
                            )
                            self.invoice_repository.save_invoice(paid_inv)

                            if self.subscription_service is not None:
                                self.subscription_service.activate_subscription(
                                    subscription_id=invoice.subscription_id,
                                    actor_id="WEBHOOK_EVENT",
                                )
                    elif event.event_type in ("payment.failed", "charge.failed", "payment_intent.payment_failed"):
                        sub = self.subscription_repository.get_subscription(invoice.subscription_id)
                        if sub and sub.status != SubscriptionStatus.CANCELED:
                            past_due_sub = Subscription(
                                subscription_id=sub.subscription_id,
                                tenant_id=sub.tenant_id,
                                plan_id=sub.plan_id,
                                plan_version=sub.plan_version,
                                status=SubscriptionStatus.PAST_DUE,
                                billing_cycle=sub.billing_cycle,
                                current_period_start=sub.current_period_start,
                                current_period_end=sub.current_period_end,
                                base_price=sub.base_price,
                                currency=sub.currency,
                                cancel_at_period_end=sub.cancel_at_period_end,
                                canceled_at=sub.canceled_at,
                                provider_reference=sub.provider_reference,
                                created_at=sub.created_at,
                                updated_at=now,
                                metadata=sub.metadata,
                            )
                            self.subscription_repository.save_subscription(past_due_sub)

            # 3. Guardar evento procesado
            self.event_repository.save_event(event)

            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=event.tenant_id,
                actor_id="PROVIDER_WEBHOOK",
                action="PROVIDER_EVENT_PROCESSED",
                status="SUCCESS",
                details={
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "provider": event.provider,
                },
            )
