"""
Pruebas Unitarias para O.9 — Billing & Subscription Management.

Cubre exhaustivamente:
1. Inmutabilidad y Checksum de Subscription
2. Validación obligatoria de Tenant
3. Soporte estricto de BillingCycle (MONTHLY, ANNUAL)
4. Precisión de Money con Decimal (prohibición de floats)
5. Creación de suscripciones con precio base de O.8
6. Vinculación estricta de Plan y Versión
7. Generación determinista de Invoices
8. Cálculo exacto de totales y subtotales en Invoices
9. Idempotencia en la generación de Invoices por período
10. Transición de estado tras pago exitoso
11. Transición de estado tras fallo de cobro (PAST_DUE)
12. Idempotencia en procesamiento de eventos de pasarela (webhook)
13. Preservación del histórico tras cancelación (cancel_at_period_end)
14. Aislamiento estricto multi-tenant (Tenant A != Tenant B)
15. Seguridad N.5/N.9: Cero almacenamiento de números de tarjeta o secretos
16. Desacoplamiento de O.8 (Plan != Subscription) y no implementación de O.10
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
import uuid

from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.plans.models import (
    Plan,
    PlanTier,
    PlanStatus,
    PlanFeature,
    PlanLimits,
)
from src.domain.billing.models import (
    Money,
    Subscription,
    SubscriptionStatus,
    BillingCycle,
    BillingPeriod,
    Invoice,
    InvoiceStatus,
    InvoiceLine,
    InvoiceLineType,
    PaymentAttempt,
    PaymentStatus,
    PaymentProviderEvent,
    PaymentProviderReference,
    BillingIntegrityError,
    CurrencyMismatchError,
    SubscriptionNotFoundError,
    InvoiceNotFoundError,
)
from src.infrastructure.persistence.data.json.billing_repository import (
    InMemorySubscriptionRepository,
    InMemoryInvoiceRepository,
    InMemoryPaymentAttemptRepository,
    InMemoryPaymentProviderEventRepository,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    InMemoryPlanCatalogRepository,
    InMemoryPlanAssignmentRepository,
)
from src.infrastructure.billing.mock_payment_provider import MockPaymentProvider
from src.application.billing.subscription_service import SubscriptionService
from src.application.billing.billing_service import BillingService
from src.application.plans.plan_entitlement_service import PlanEntitlementService


@pytest.fixture
def plan_catalog():
    repo = InMemoryPlanCatalogRepository()
    # Plan Free (Base price: 0.00)
    free_plan = Plan(
        plan_id="plan_free",
        name="Free Tier",
        tier=PlanTier.FREE,
        version="1.0.0",
        limits=PlanLimits(max_users=2),
        features=[PlanFeature.ADVANCED_ANALYTICS],
        metadata={"base_price": "0.00", "currency": "USD"},
    )
    # Plan Pro (Base price: 99.00)
    pro_plan = Plan(
        plan_id="plan_pro",
        name="Professional Tier",
        tier=PlanTier.PRO,
        version="1.0.0",
        limits=PlanLimits(max_users=10),
        features=[PlanFeature.ADVANCED_ANALYTICS, PlanFeature.MULTI_USER, PlanFeature.MODEL_INFERENCE],
        metadata={"base_price": "99.00", "currency": "USD"},
    )
    repo.save_plan(free_plan)
    repo.save_plan(pro_plan)
    return repo


@pytest.fixture
def plan_assignment_repo():
    return InMemoryPlanAssignmentRepository()


@pytest.fixture
def entitlement_service(plan_catalog, plan_assignment_repo):
    return PlanEntitlementService(
        catalog_repository=plan_catalog,
        assignment_repository=plan_assignment_repo,
    )


@pytest.fixture
def billing_infra():
    sub_repo = InMemorySubscriptionRepository()
    inv_repo = InMemoryInvoiceRepository()
    pay_repo = InMemoryPaymentAttemptRepository()
    evt_repo = InMemoryPaymentProviderEventRepository()
    provider = MockPaymentProvider(provider_name="mock_stripe", auto_succeed=True)
    return sub_repo, inv_repo, pay_repo, evt_repo, provider


def test_1_immutable_subscription_and_checksum():
    """1. Inmutabilidad y Checksum de Subscription."""
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        subscription_id="sub_test_01",
        tenant_id="tenant_alpha",
        plan_id="plan_pro",
        plan_version="1.0.0",
        status=SubscriptionStatus.ACTIVE,
        billing_cycle=BillingCycle.MONTHLY,
        current_period_start=start,
        current_period_end=end,
        base_price=Decimal("99.00"),
        currency="USD",
    )
    assert sub.verify_integrity() is True
    assert sub.is_active is True

    # Verificar inmutabilidad (dataclass frozen)
    with pytest.raises(Exception):
        sub.status = SubscriptionStatus.CANCELED

    # Checksum tampering detection
    with pytest.raises(BillingIntegrityError):
        Subscription(
            subscription_id="sub_test_01",
            tenant_id="tenant_alpha",
            plan_id="plan_pro",
            plan_version="1.0.0",
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=BillingCycle.MONTHLY,
            current_period_start=start,
            current_period_end=end,
            base_price=Decimal("99.00"),
            currency="USD",
            checksum="corrupted_checksum_hex",
        )


def test_2_tenant_required_and_safe_identifier():
    """2. Validación obligatoria y segura de Tenant."""
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        Subscription(
            subscription_id="sub_01",
            tenant_id="",  # Empty tenant
            plan_id="plan_pro",
            plan_version="1.0.0",
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=BillingCycle.MONTHLY,
            current_period_start=start,
            current_period_end=end,
            base_price=Decimal("99.00"),
        )

    with pytest.raises(ValueError):
        Subscription(
            subscription_id="sub_01",
            tenant_id="tenant/bad..id",  # Path traversal attempt
            plan_id="plan_pro",
            plan_version="1.0.0",
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=BillingCycle.MONTHLY,
            current_period_start=start,
            current_period_end=end,
            base_price=Decimal("99.00"),
        )


def test_3_valid_billing_cycle_and_periods():
    """3. Soporte de ciclos de facturación válidos (MONTHLY, ANNUAL)."""
    start = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    monthly_period = BillingPeriod.from_cycle(start, BillingCycle.MONTHLY)
    assert monthly_period.start_time == start
    assert monthly_period.end_time == datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)

    annual_period = BillingPeriod.from_cycle(start, BillingCycle.ANNUAL)
    assert annual_period.start_time == start
    assert annual_period.end_time == datetime(2027, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_4_decimal_money_precision_and_no_floats():
    """4. Precisión monetaria con Decimal y rechazo estricto de floats."""
    # Float no permitido
    with pytest.raises(TypeError, match="Float is not permitted"):
        Money(amount=19.99)

    m1 = Money(Decimal("100.50"), "USD")
    m2 = Money(Decimal("25.25"), "USD")
    m3 = m1 + m2
    assert m3.amount == Decimal("125.75")
    assert m3.currency == "USD"

    # Mismatch de monedas
    m_clp = Money(Decimal("50000"), "CLP")
    with pytest.raises(CurrencyMismatchError):
        _ = m1 + m_clp


def test_5_create_subscription_from_catalog_price(plan_catalog, entitlement_service, billing_infra):
    """5. Creación de suscripción con precio base tomado del catálogo de O.8."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )

    sub = sub_service.create_subscription(
        tenant_id="tenant_alpha",
        plan_id="plan_pro",
        billing_cycle=BillingCycle.MONTHLY,
    )
    assert sub.tenant_id == "tenant_alpha"
    assert sub.plan_id == "plan_pro"
    assert sub.base_price == Decimal("99.00")
    assert sub.currency == "USD"
    assert sub.billing_cycle == BillingCycle.MONTHLY


def test_6_plan_and_version_binding(plan_catalog, entitlement_service, billing_infra):
    """6. Vinculación estricta de Plan y Versión."""
    sub_repo, _, _, _, _ = billing_infra
    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )

    # Error ante plan inexistente
    with pytest.raises(Exception):
        sub_service.create_subscription(
            tenant_id="tenant_alpha",
            plan_id="plan_non_existent",
        )


def test_7_invoice_deterministic_generation(plan_catalog, entitlement_service, billing_infra):
    """7. Generación determinista de Invoices."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )
    bill_service = BillingService(
        subscription_repository=sub_repo,
        invoice_repository=inv_repo,
        payment_attempt_repository=pay_repo,
        event_repository=evt_repo,
        payment_provider=provider,
        subscription_service=sub_service,
    )
    sub_service.set_billing_service(bill_service)

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    invoice = bill_service.generate_period_invoice(sub.subscription_id)

    assert invoice.subscription_id == sub.subscription_id
    assert invoice.tenant_id == "tenant_alpha"
    assert invoice.total == Decimal("99.00")
    assert invoice.status == InvoiceStatus.OPEN
    assert invoice.verify_integrity() is True


def test_8_invoice_totals_and_lines():
    """8. Cálculo de totales con múltiples líneas y descuentos."""
    period = BillingPeriod(
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    line1 = InvoiceLine(
        line_id="line_01",
        line_type=InvoiceLineType.BASE_PLAN,
        description="Plan Pro Base",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        amount=Decimal("100.00"),
        currency="USD",
    )
    line2 = InvoiceLine(
        line_id="line_02",
        line_type=InvoiceLineType.USAGE,
        description="Additional model usage",
        quantity=Decimal("5"),
        unit_price=Decimal("10.00"),
        amount=Decimal("50.00"),
        currency="USD",
    )
    inv = Invoice.create_deterministic(
        invoice_id="inv_tot_01",
        tenant_id="tenant_alpha",
        subscription_id="sub_tot_01",
        period=period,
        lines=[line1, line2],
        discounts=Decimal("10.00"),
        taxes=Decimal("5.00"),
        currency="USD",
    )
    assert inv.subtotal == Decimal("150.00")
    assert inv.total == Decimal("145.00")  # 150 - 10 + 5
    assert inv.verify_integrity() is True


def test_9_duplicate_invoice_idempotent(plan_catalog, entitlement_service, billing_infra):
    """9. Idempotencia en la generación de Invoices para el mismo período."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )
    bill_service = BillingService(
        subscription_repository=sub_repo,
        invoice_repository=inv_repo,
        payment_attempt_repository=pay_repo,
        event_repository=evt_repo,
        payment_provider=provider,
        subscription_service=sub_service,
    )

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    inv1 = bill_service.generate_period_invoice(sub.subscription_id)
    inv2 = bill_service.generate_period_invoice(sub.subscription_id)

    assert inv1.invoice_id == inv2.invoice_id
    all_invoices = inv_repo.list_invoices_for_tenant("tenant_alpha")
    assert len(all_invoices) == 1


def test_10_payment_success_transitions(plan_catalog, entitlement_service, billing_infra):
    """10. Transición de estado tras pago exitoso (Invoice PAID, Subscription ACTIVE)."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    provider.set_auto_succeed(True)

    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )
    bill_service = BillingService(
        subscription_repository=sub_repo,
        invoice_repository=inv_repo,
        payment_attempt_repository=pay_repo,
        event_repository=evt_repo,
        payment_provider=provider,
        subscription_service=sub_service,
    )
    sub_service.set_billing_service(bill_service)

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    invoice = bill_service.generate_period_invoice(sub.subscription_id)

    attempt = bill_service.process_payment(
        invoice_id=invoice.invoice_id,
        idempotency_key="pay_key_01",
    )
    assert attempt.status == PaymentStatus.SUCCEEDED

    updated_inv = inv_repo.get_invoice(invoice.invoice_id)
    assert updated_inv.status == InvoiceStatus.PAID
    assert updated_inv.paid_at is not None

    updated_sub = sub_repo.get_subscription(sub.subscription_id)
    assert updated_sub.status == SubscriptionStatus.ACTIVE


def test_11_payment_failure_transitions(plan_catalog, entitlement_service, billing_infra):
    """11. Transición de estado tras fallo de cobro (Subscription PAST_DUE, Invoice OPEN)."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    provider.set_auto_succeed(False, fail_reason="Insufficient funds")

    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )
    bill_service = BillingService(
        subscription_repository=sub_repo,
        invoice_repository=inv_repo,
        payment_attempt_repository=pay_repo,
        event_repository=evt_repo,
        payment_provider=provider,
        subscription_service=sub_service,
    )
    sub_service.set_billing_service(bill_service)

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    invoice = bill_service.generate_period_invoice(sub.subscription_id)

    attempt = bill_service.process_payment(
        invoice_id=invoice.invoice_id,
        idempotency_key="pay_key_fail_01",
    )
    assert attempt.status == PaymentStatus.FAILED

    updated_inv = inv_repo.get_invoice(invoice.invoice_id)
    assert updated_inv.status == InvoiceStatus.OPEN

    updated_sub = sub_repo.get_subscription(sub.subscription_id)
    assert updated_sub.status == SubscriptionStatus.PAST_DUE


def test_12_duplicate_provider_event_idempotent(plan_catalog, entitlement_service, billing_infra):
    """12. Idempotencia en procesamiento de webhooks repetidos."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )
    bill_service = BillingService(
        subscription_repository=sub_repo,
        invoice_repository=inv_repo,
        payment_attempt_repository=pay_repo,
        event_repository=evt_repo,
        payment_provider=provider,
        subscription_service=sub_service,
    )

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    invoice = bill_service.generate_period_invoice(sub.subscription_id)

    event = provider.generate_mock_webhook_event(
        event_type="payment.succeeded",
        tenant_id="tenant_alpha",
        idempotency_key="evt_key_01",
        subscription_id=sub.subscription_id,
        invoice_id=invoice.invoice_id,
        amount=invoice.total,
    )

    bill_service.process_provider_event(event)
    assert inv_repo.get_invoice(invoice.invoice_id).status == InvoiceStatus.PAID

    # Repetir el mismo webhook
    bill_service.process_provider_event(event)
    assert inv_repo.get_invoice(invoice.invoice_id).status == InvoiceStatus.PAID
    assert evt_repo.is_event_processed(event.event_id) is True


def test_13_cancellation_preserves_history(plan_catalog, entitlement_service, billing_infra):
    """13. Cancelación preserva histórico de facturas."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )
    bill_service = BillingService(
        subscription_repository=sub_repo,
        invoice_repository=inv_repo,
        payment_attempt_repository=pay_repo,
        event_repository=evt_repo,
        payment_provider=provider,
        subscription_service=sub_service,
    )

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    inv = bill_service.generate_period_invoice(sub.subscription_id)

    canceled = sub_service.cancel_subscription(sub.subscription_id, immediately=False)
    assert canceled.cancel_at_period_end is True

    invoices = inv_repo.list_invoices_for_tenant("tenant_alpha")
    assert len(invoices) == 1
    assert invoices[0].invoice_id == inv.invoice_id


def test_14_tenant_isolation_boundary(plan_catalog, entitlement_service, billing_infra):
    """14. Aislamiento estricto multi-tenant (Tenant A != Tenant B)."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )

    sub_a = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")

    ctx_b = TenantContext(tenant_id="tenant_beta", identity_id="usr_b")

    # Tenant B intentando acceder a la suscripción de Tenant A
    with pytest.raises(CrossTenantAccessError):
        sub_service.activate_subscription(sub_a.subscription_id, context=ctx_b)

    # Repositorio particionado por tenant
    assert len(sub_repo.list_subscriptions_for_tenant("tenant_beta")) == 0
    assert len(sub_repo.list_subscriptions_for_tenant("tenant_alpha")) == 1


def test_15_no_card_or_secret_storage(plan_catalog, entitlement_service, billing_infra):
    """15. Seguridad N.5/N.9: Cero almacenamiento de números de tarjeta o secretos."""
    sub_repo, inv_repo, pay_repo, evt_repo, provider = billing_infra
    sub_service = SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_catalog,
        plan_entitlement_service=entitlement_service,
    )
    bill_service = BillingService(
        subscription_repository=sub_repo,
        invoice_repository=inv_repo,
        payment_attempt_repository=pay_repo,
        event_repository=evt_repo,
        payment_provider=provider,
        subscription_service=sub_service,
    )

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    inv = bill_service.generate_period_invoice(sub.subscription_id)
    attempt = bill_service.process_payment(inv.invoice_id, idempotency_key="key_sec_01")

    # Verificar que ni en attempt, ni en invoice, ni en subscription hay campos de tarjeta
    assert not hasattr(attempt, "card_number")
    assert not hasattr(attempt, "cvv")
    assert not hasattr(inv, "card_number")
    assert not hasattr(sub, "card_number")
    assert attempt.provider_reference is not None
    assert "tx_" in attempt.provider_reference


def test_16_no_o13_plus_imported():
    """16. Verificar que O.13+ no fue importado ni tocado accidentalmente."""
    import sys
    assert "src.application.deployment_automation" not in sys.modules
    assert "src.domain.deployment_automation" not in sys.modules
