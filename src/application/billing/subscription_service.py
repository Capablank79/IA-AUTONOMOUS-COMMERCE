"""
Servicio de Aplicación para Gestión de Suscripciones SaaS (Hito O.9 — Billing & Subscription Management).

Responsabilidades:
- Implementar SubscriptionServicePort.
- Administrar el ciclo de vida de suscripciones comerciales tenant-scoped (create, activate, cancel, renew).
- Reconciliar y gobernar la asignación de planes en O.8 (PlanEntitlementService).
- Garantizar aislamiento multi-tenant estricto (O.1).
- Emitir trazas (K.2) y auditoría (K.1) seguras sin secretos ni datos sensibles.
- Soportar control temporal determinista mediante ClockPort (K.7).
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
from src.domain.plans.ports import PlanCatalogRepositoryPort, PlanEntitlementServicePort
from src.domain.plans.models import Plan, PlanAssignmentStatus, PlanNotFoundError
from src.domain.billing.models import (
    Subscription,
    SubscriptionStatus,
    BillingCycle,
    BillingPeriod,
    Invoice,
    InvoiceStatus,
    InvoiceLine,
    InvoiceLineType,
    Money,
    PaymentProviderReference,
    SubscriptionNotFoundError,
    InvalidSubscriptionStateError,
    BillingTenantIsolationError,
    BillingIntegrityError,
)
from src.domain.billing.ports import (
    SubscriptionRepositoryPort,
    InvoiceRepositoryPort,
    SubscriptionServicePort,
    BillingServicePort,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort


class SubscriptionService(SubscriptionServicePort):
    """
    Servicio central de suscripciones comerciales SaaS.
    """

    def __init__(
        self,
        subscription_repository: SubscriptionRepositoryPort,
        plan_catalog_repository: PlanCatalogRepositoryPort,
        plan_entitlement_service: Optional[PlanEntitlementServicePort] = None,
        billing_service: Optional[BillingServicePort] = None,
        clock: Optional[ClockPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        agent_trace_repository: Optional[AgentTraceRepositoryPort] = None,
    ):
        self.subscription_repository = subscription_repository
        self.plan_catalog_repository = plan_catalog_repository
        self.plan_entitlement_service = plan_entitlement_service
        self.billing_service = billing_service
        self.clock = clock
        self.audit_repository = audit_repository
        self.agent_trace_repository = agent_trace_repository
        self._lock = threading.RLock()

    def set_billing_service(self, billing_service: BillingServicePort) -> None:
        """Permite inyectar BillingService de forma circular controlada."""
        with self._lock:
            self.billing_service = billing_service

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
                audit_id=f"aud_sub_{uuid.uuid4().hex[:12]}",
                occurred_at=self._get_now(),
                actor=AuditActor(
                    actor_type=AuditActorType.USER if actor_id else AuditActorType.SYSTEM,
                    actor_id=actor_id or tenant_id,
                ),
                record_type=record_type,
                subject_type="SUBSCRIPTION",
                subject_id=tenant_id,
                action_or_operation=action,
                status=status,
                correlation_id=correlation_id or f"corr_{uuid.uuid4().hex[:8]}",
                metadata=dict(details),
            )
            self.audit_repository.append(rec)
        except Exception:
            pass

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
        """
        Crea una nueva suscripción comercial para el tenant.
        Obtiene el precio base desde el catálogo de O.8.
        """
        if context is not None:
            CrossTenantGuard.ensure_tenant_context(context)
            CrossTenantGuard.assert_same_tenant(context, tenant_id, operation_name="create_subscription")

        now = self._get_now()
        start = start_time or now
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        with self._lock:
            # 1. Validar plan en catálogo O.8
            plan = self.plan_catalog_repository.get_plan(plan_id, version=plan_version)
            if plan is None:
                raise PlanNotFoundError(f"Plan '{plan_id}' (version: {plan_version}) not found in catalog.")

            # 2. Calcular ventana del billing period
            period = BillingPeriod.from_cycle(start, billing_cycle)

            # 3. Determinar precio según metadatos del plan o catálogo de precios
            # Si el plan define "base_price" o "price" en metadata, usarlo; sino derivar por tier
            metadata_price = plan.metadata.get("base_price") or plan.metadata.get("price")
            if metadata_price is not None:
                base_price = Decimal(str(metadata_price))
            elif plan.tier.value == "FREE":
                base_price = Decimal("0.00")
            elif plan.tier.value == "PRO":
                base_price = Decimal("99.00")
            elif plan.tier.value == "ENTERPRISE":
                base_price = Decimal("499.00")
            else:
                base_price = Decimal("0.00")

            currency = str(plan.metadata.get("currency", "USD")).upper()
            if billing_cycle == BillingCycle.ANNUAL:
                base_price = (base_price * Decimal("12")).quantize(Decimal("0.01"))

            status = SubscriptionStatus.ACTIVE if auto_activate else SubscriptionStatus.TRIALING if base_price == Decimal("0.00") else SubscriptionStatus.PAST_DUE

            subscription_id = f"sub_{uuid.uuid4().hex[:16]}"
            subscription = Subscription(
                subscription_id=subscription_id,
                tenant_id=tenant_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                status=status,
                billing_cycle=billing_cycle,
                current_period_start=period.start_time,
                current_period_end=period.end_time,
                base_price=base_price,
                currency=currency,
                cancel_at_period_end=False,
                created_at=now,
                updated_at=now,
            )

            self.subscription_repository.save_subscription(subscription)

            # 4. Si se autoactiva o es gratuita, sincronizar PlanAssignment en O.8
            if subscription.is_active and self.plan_entitlement_service is not None:
                self.plan_entitlement_service.assign_plan(
                    tenant_id=tenant_id,
                    plan_id=plan.plan_id,
                    plan_version=plan.version,
                    effective_from=period.start_time,
                    effective_until=period.end_time,
                    source_reason=f"SUBSCRIPTION_CREATED_{subscription_id}",
                    actor_id=actor_id,
                    context=context,
                )

            # 5. Emitir auditoría
            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="SUBSCRIPTION_CREATED",
                status=status.value,
                details={
                    "subscription_id": subscription_id,
                    "plan_id": plan.plan_id,
                    "plan_version": plan.version,
                    "billing_cycle": billing_cycle.value,
                    "base_price": str(base_price),
                    "currency": currency,
                },
            )

            return subscription

    def activate_subscription(
        self,
        subscription_id: str,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Subscription:
        """
        Activa una suscripción comercial (p.ej. tras confirmación de pago) y sincroniza con O.8.
        """
        with self._lock:
            sub = self.subscription_repository.get_subscription(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"Subscription '{subscription_id}' not found.")

            if context is not None:
                CrossTenantGuard.ensure_tenant_context(context)
                CrossTenantGuard.assert_same_tenant(context, sub.tenant_id, operation_name="activate_subscription")

            if sub.status == SubscriptionStatus.CANCELED:
                raise InvalidSubscriptionStateError("Cannot activate a CANCELED subscription. Create a new subscription.")

            now = self._get_now()
            activated = Subscription(
                subscription_id=sub.subscription_id,
                tenant_id=sub.tenant_id,
                plan_id=sub.plan_id,
                plan_version=sub.plan_version,
                status=SubscriptionStatus.ACTIVE,
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

            self.subscription_repository.save_subscription(activated)

            # Sincronizar O.8 PlanAssignment
            if self.plan_entitlement_service is not None:
                self.plan_entitlement_service.assign_plan(
                    tenant_id=activated.tenant_id,
                    plan_id=activated.plan_id,
                    plan_version=activated.plan_version,
                    effective_from=activated.current_period_start,
                    effective_until=activated.current_period_end,
                    source_reason=f"SUBSCRIPTION_ACTIVATED_{subscription_id}",
                    actor_id=actor_id,
                    context=context,
                )

            # Auditoría
            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=activated.tenant_id,
                actor_id=actor_id,
                action="SUBSCRIPTION_ACTIVATED",
                status="ACTIVE",
                details={
                    "subscription_id": subscription_id,
                    "plan_id": activated.plan_id,
                },
            )

            return activated

    def cancel_subscription(
        self,
        subscription_id: str,
        immediately: bool = False,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Subscription:
        """
        Cancela una suscripción inmediatamente o marca cancel_at_period_end.
        Preserva todas las facturas y el histórico contable.
        """
        with self._lock:
            sub = self.subscription_repository.get_subscription(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"Subscription '{subscription_id}' not found.")

            if context is not None:
                CrossTenantGuard.ensure_tenant_context(context)
                CrossTenantGuard.assert_same_tenant(context, sub.tenant_id, operation_name="cancel_subscription")

            now = self._get_now()
            new_status = SubscriptionStatus.CANCELED if immediately else sub.status
            cancel_at_end = True if not immediately else sub.cancel_at_period_end

            canceled = Subscription(
                subscription_id=sub.subscription_id,
                tenant_id=sub.tenant_id,
                plan_id=sub.plan_id,
                plan_version=sub.plan_version,
                status=new_status,
                billing_cycle=sub.billing_cycle,
                current_period_start=sub.current_period_start,
                current_period_end=sub.current_period_end,
                base_price=sub.base_price,
                currency=sub.currency,
                cancel_at_period_end=cancel_at_end,
                canceled_at=now,
                provider_reference=sub.provider_reference,
                created_at=sub.created_at,
                updated_at=now,
                metadata=sub.metadata,
            )

            self.subscription_repository.save_subscription(canceled)

            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=canceled.tenant_id,
                actor_id=actor_id,
                action="SUBSCRIPTION_CANCELED",
                status=new_status.value,
                details={
                    "subscription_id": subscription_id,
                    "immediately": immediately,
                    "cancel_at_period_end": cancel_at_end,
                },
            )

            return canceled

    def renew_subscription(
        self,
        subscription_id: str,
        actor_id: Optional[str] = None,
        context: Optional[TenantContext] = None,
    ) -> Tuple[Subscription, Invoice]:
        """
        Renueva el período de facturación de una suscripción activa y emite la siguiente factura.
        Si estaba marcada para cancel_at_period_end, finaliza y pasa a CANCELED/EXPIRED.
        """
        with self._lock:
            sub = self.subscription_repository.get_subscription(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"Subscription '{subscription_id}' not found.")

            if context is not None:
                CrossTenantGuard.ensure_tenant_context(context)
                CrossTenantGuard.assert_same_tenant(context, sub.tenant_id, operation_name="renew_subscription")

            now = self._get_now()

            # Si estaba programada para cancelar al fin del período
            if sub.cancel_at_period_end:
                expired = Subscription(
                    subscription_id=sub.subscription_id,
                    tenant_id=sub.tenant_id,
                    plan_id=sub.plan_id,
                    plan_version=sub.plan_version,
                    status=SubscriptionStatus.CANCELED,
                    billing_cycle=sub.billing_cycle,
                    current_period_start=sub.current_period_start,
                    current_period_end=sub.current_period_end,
                    base_price=sub.base_price,
                    currency=sub.currency,
                    cancel_at_period_end=True,
                    canceled_at=sub.canceled_at or now,
                    provider_reference=sub.provider_reference,
                    created_at=sub.created_at,
                    updated_at=now,
                    metadata=sub.metadata,
                )
                self.subscription_repository.save_subscription(expired)
                raise InvalidSubscriptionStateError("Subscription was scheduled for cancellation and has expired.")

            # Nuevo período consecutivo
            next_period = BillingPeriod.from_cycle(sub.current_period_end, sub.billing_cycle)

            renewed = Subscription(
                subscription_id=sub.subscription_id,
                tenant_id=sub.tenant_id,
                plan_id=sub.plan_id,
                plan_version=sub.plan_version,
                status=sub.status,
                billing_cycle=sub.billing_cycle,
                current_period_start=next_period.start_time,
                current_period_end=next_period.end_time,
                base_price=sub.base_price,
                currency=sub.currency,
                cancel_at_period_end=False,
                canceled_at=None,
                provider_reference=sub.provider_reference,
                created_at=sub.created_at,
                updated_at=now,
                metadata=sub.metadata,
            )

            self.subscription_repository.save_subscription(renewed)

            # Generar factura determinista del nuevo período si BillingService está inyectado
            invoice = None
            if self.billing_service is not None:
                invoice = self.billing_service.generate_period_invoice(
                    subscription_id=renewed.subscription_id,
                    period=next_period,
                    actor_id=actor_id,
                    context=context,
                )

            # Sincronizar O.8 PlanAssignment si sigue activa
            if renewed.is_active and self.plan_entitlement_service is not None:
                self.plan_entitlement_service.assign_plan(
                    tenant_id=renewed.tenant_id,
                    plan_id=renewed.plan_id,
                    plan_version=renewed.plan_version,
                    effective_from=next_period.start_time,
                    effective_until=next_period.end_time,
                    source_reason=f"SUBSCRIPTION_RENEWED_{subscription_id}",
                    actor_id=actor_id,
                    context=context,
                )

            self._emit_audit(
                record_type=AuditRecordType.ACTION_EXECUTED,
                tenant_id=renewed.tenant_id,
                actor_id=actor_id,
                action="SUBSCRIPTION_RENEWED",
                status="ACTIVE",
                details={
                    "subscription_id": subscription_id,
                    "new_period_start": next_period.start_time.isoformat(),
                    "new_period_end": next_period.end_time.isoformat(),
                },
            )

            return renewed, invoice

    def get_active_subscription(
        self,
        tenant_id: str,
        current_time: Optional[datetime] = None,
        context: Optional[TenantContext] = None,
    ) -> Optional[Subscription]:
        """
        Resuelve la suscripción activa del tenant en el timestamp dado.
        """
        if context is not None:
            CrossTenantGuard.ensure_tenant_context(context)
            CrossTenantGuard.assert_same_tenant(context, tenant_id, operation_name="get_active_subscription")

        now = current_time or self._get_now()
        with self._lock:
            return self.subscription_repository.get_active_subscription(tenant_id, now)
