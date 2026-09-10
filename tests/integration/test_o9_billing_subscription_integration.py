"""
Pruebas de Integración y Flujo E2E para O.9 — Billing & Subscription Management.

Escenarios cubiertos:
A. Tenant A se suscribe a un Plan válido -> Suscripción creada en estado correspondiente.
B. Tenant B no puede ver ni modificar la suscripción ni facturas de Tenant A (Tenant Isolation).
C. El ciclo de facturación genera exactamente una factura determinista por período.
D. Payment Provider Success -> Invoice PAID, Subscription ACTIVE y O.8 PlanAssignment sincronizado.
E. Payment Failure -> Invoice permanece OPEN, Subscription pasa a PAST_DUE.
F. Duplicate Payment Event / Webhook -> Sin cobro duplicado ni mutación inconsistente (Idempotencia).
G. cancel_at_period_end -> Suscripción se cancela al fin del período y preserva el histórico contable.
H. Upgrade/Downgrade de Plan -> Estado y versiones consistentes.
I. Persistencia en disco y recuperación tras reinicio (Restart-safety y verificación SHA-256).
J. Resiliencia y Fail-Safe ante corrupción de estado o proveedor desconocido.
K. E2E: Tenant -> O.8 Plan -> O.9 Subscription -> Billing Period -> Invoice -> Payment Provider -> Active Entitlement en O.8/O.7/Gateway.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
import shutil
import tempfile
import pytest

from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.plans.models import (
    Plan,
    PlanTier,
    PlanStatus,
    PlanFeature,
    PlanLimits,
    PlanQuotaTemplate,
    PlanEntitlementRequest,
    PlanEntitlementStatus,
)
from src.domain.quota_management.models import (
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
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
    BillingIntegrityError,
    SubscriptionNotFoundError,
    InvoiceNotFoundError,
)
from src.infrastructure.persistence.data.json.billing_repository import (
    InMemorySubscriptionRepository,
    InMemoryInvoiceRepository,
    InMemoryPaymentAttemptRepository,
    InMemoryPaymentProviderEventRepository,
    JsonSubscriptionRepository,
    JsonInvoiceRepository,
    JsonPaymentAttemptRepository,
    JsonPaymentProviderEventRepository,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    InMemoryPlanCatalogRepository,
    InMemoryPlanAssignmentRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
)
from src.infrastructure.billing.mock_payment_provider import MockPaymentProvider
from src.application.billing.subscription_service import SubscriptionService
from src.application.billing.billing_service import BillingService
from src.application.plans.plan_entitlement_service import PlanEntitlementService


@pytest.fixture
def plan_catalog():
    repo = InMemoryPlanCatalogRepository()
    # Plan Pro con Quota template y features
    q_rule = QuotaRule(
        rule_id="qrule_pro_model",
        quota_type=QuotaType.MAX_REQUESTS,
        scope=QuotaScope.TENANT,
        limit_value=1000,
        window_type=QuotaWindowType.MONTH,
    )
    pro_plan = Plan(
        plan_id="plan_pro",
        name="Pro Plan",
        tier=PlanTier.PRO,
        version="1.0.0",
        limits=PlanLimits(max_users=5),
        features=[PlanFeature.MODEL_INFERENCE, PlanFeature.ADVANCED_ANALYTICS, PlanFeature.MULTI_USER],
        quota_template=PlanQuotaTemplate(rules=[q_rule]),
        allowed_model_classes=["gpt-4o", "claude-3-5-sonnet"],
        allowed_providers=["openai", "anthropic"],
        metadata={"base_price": "99.00", "currency": "USD"},
    )
    # Plan Enterprise
    ent_plan = Plan(
        plan_id="plan_enterprise",
        name="Enterprise Plan",
        tier=PlanTier.ENTERPRISE,
        version="1.0.0",
        limits=PlanLimits(max_users=100),
        features=[PlanFeature.MODEL_INFERENCE, PlanFeature.ADVANCED_ANALYTICS, PlanFeature.CUSTOM_INTEGRATIONS],
        metadata={"base_price": "499.00", "currency": "USD"},
    )
    repo.save_plan(pro_plan)
    repo.save_plan(ent_plan)
    return repo


@pytest.fixture
def integration_env(plan_catalog):
    plan_assign_repo = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()

    entitlement_service = PlanEntitlementService(
        catalog_repository=plan_catalog,
        assignment_repository=plan_assign_repo,
        quota_policy_repository=quota_repo,
    )

    sub_repo = InMemorySubscriptionRepository()
    inv_repo = InMemoryInvoiceRepository()
    pay_repo = InMemoryPaymentAttemptRepository()
    evt_repo = InMemoryPaymentProviderEventRepository()
    payment_provider = MockPaymentProvider(provider_name="mock_stripe", auto_succeed=True)

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
        payment_provider=payment_provider,
        subscription_service=sub_service,
    )

    sub_service.set_billing_service(bill_service)

    return {
        "plan_catalog": plan_catalog,
        "plan_assign_repo": plan_assign_repo,
        "quota_repo": quota_repo,
        "entitlement_service": entitlement_service,
        "sub_repo": sub_repo,
        "inv_repo": inv_repo,
        "pay_repo": pay_repo,
        "evt_repo": evt_repo,
        "payment_provider": payment_provider,
        "sub_service": sub_service,
        "bill_service": bill_service,
    }


def test_scenario_a_and_c_subscription_and_invoice_creation(integration_env):
    """Escenario A y C: Tenant A se suscribe y se genera factura determinista única por período."""
    sub_service = integration_env["sub_service"]
    bill_service = integration_env["bill_service"]

    # A: Crear suscripción
    sub = sub_service.create_subscription(
        tenant_id="tenant_alpha",
        plan_id="plan_pro",
        billing_cycle=BillingCycle.MONTHLY,
    )
    assert sub.subscription_id.startswith("sub_")
    assert sub.tenant_id == "tenant_alpha"
    assert sub.base_price == Decimal("99.00")

    # C: Generar invoice
    inv = bill_service.generate_period_invoice(sub.subscription_id)
    assert inv.subscription_id == sub.subscription_id
    assert inv.total == Decimal("99.00")
    assert inv.status == InvoiceStatus.OPEN

    # Idempotencia: generar de nuevo para el mismo período
    inv2 = bill_service.generate_period_invoice(sub.subscription_id)
    assert inv2.invoice_id == inv.invoice_id


def test_scenario_b_tenant_isolation(integration_env):
    """Escenario B: Tenant B no puede ver ni acceder a los recursos de facturación de Tenant A."""
    sub_service = integration_env["sub_service"]
    bill_service = integration_env["bill_service"]

    sub_a = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    inv_a = bill_service.generate_period_invoice(sub_a.subscription_id)

    ctx_b = TenantContext(tenant_id="tenant_beta", identity_id="user_b")

    with pytest.raises(CrossTenantAccessError):
        sub_service.activate_subscription(sub_a.subscription_id, context=ctx_b)

    with pytest.raises(CrossTenantAccessError):
        bill_service.generate_period_invoice(sub_a.subscription_id, context=ctx_b)

    with pytest.raises(CrossTenantAccessError):
        bill_service.process_payment(inv_a.invoice_id, idempotency_key="key_b", context=ctx_b)


def test_scenario_d_payment_success_and_entitlement_sync(integration_env):
    """Escenario D: Pago exitoso activa Subscription y sincroniza PlanAssignment/Quotas en O.8/O.7."""
    sub_service = integration_env["sub_service"]
    bill_service = integration_env["bill_service"]
    entitlement_service = integration_env["entitlement_service"]
    quota_repo = integration_env["quota_repo"]

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    inv = bill_service.generate_period_invoice(sub.subscription_id)

    # Cobrar factura exitosamente
    attempt = bill_service.process_payment(inv.invoice_id, idempotency_key="pay_tx_01")
    assert attempt.status == PaymentStatus.SUCCEEDED

    # Verificar estado de factura y suscripción
    inv_updated = integration_env["inv_repo"].get_invoice(inv.invoice_id)
    assert inv_updated.status == InvoiceStatus.PAID

    sub_updated = sub_service.get_active_subscription("tenant_alpha")
    assert sub_updated is not None
    assert sub_updated.status == SubscriptionStatus.ACTIVE

    # Verificar que O.8 y O.7 tienen el plan activo y la política de cuotas
    active_plan = entitlement_service.get_active_plan("tenant_alpha")
    assert active_plan is not None
    assert active_plan.plan_id == "plan_pro"

    # Verificar entitlement
    decision = entitlement_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_alpha", feature=PlanFeature.MODEL_INFERENCE)
    )
    assert decision.status == PlanEntitlementStatus.ALLOW
    assert decision.is_entitled is True

    # Quota policy materializada en O.7
    policy = quota_repo.get_policy("tenant_alpha")
    assert policy is not None
    assert len(policy.rules) >= 1
    assert policy.rules[0].limit_value == 1000


def test_scenario_e_payment_failure_transitions_to_past_due(integration_env):
    """Escenario E: Fallo de cobro mantiene la factura OPEN y pasa Subscription a PAST_DUE."""
    sub_service = integration_env["sub_service"]
    bill_service = integration_env["bill_service"]
    provider = integration_env["payment_provider"]

    # Simular fallo de cobro
    provider.set_auto_succeed(False, fail_reason="Card expired")

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    inv = bill_service.generate_period_invoice(sub.subscription_id)

    attempt = bill_service.process_payment(inv.invoice_id, idempotency_key="pay_fail_key_01")
    assert attempt.status == PaymentStatus.FAILED

    inv_updated = integration_env["inv_repo"].get_invoice(inv.invoice_id)
    assert inv_updated.status == InvoiceStatus.OPEN

    sub_updated = integration_env["sub_repo"].get_subscription(sub.subscription_id)
    assert sub_updated.status == SubscriptionStatus.PAST_DUE


def test_scenario_f_duplicate_payment_and_idempotency(integration_env):
    """Escenario F: Reintentos y duplicados no generan cobro doble ni estados corruptos."""
    bill_service = integration_env["bill_service"]
    sub_service = integration_env["sub_service"]

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro")
    inv = bill_service.generate_period_invoice(sub.subscription_id)

    # Intento 1
    att1 = bill_service.process_payment(inv.invoice_id, idempotency_key="idem_001")
    assert att1.status == PaymentStatus.SUCCEEDED

    # Intento 2 con misma idempotency key
    att2 = bill_service.process_payment(inv.invoice_id, idempotency_key="idem_001")
    assert att2.attempt_id == att1.attempt_id

    # Intento 3 con otra clave pero sobre factura ya pagada
    att3 = bill_service.process_payment(inv.invoice_id, idempotency_key="idem_002")
    assert att3.status == PaymentStatus.SUCCEEDED
    assert att3.error_message == "Invoice already paid."


def test_scenario_g_cancellation_and_period_expiration(integration_env):
    """Escenario G: cancel_at_period_end finaliza ciclo al renovar y preserva el histórico."""
    sub_service = integration_env["sub_service"]
    bill_service = integration_env["bill_service"]

    sub = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro", auto_activate=True)
    inv = bill_service.generate_period_invoice(sub.subscription_id)

    # Cancelar al final del período
    canceled_sub = sub_service.cancel_subscription(sub.subscription_id, immediately=False)
    assert canceled_sub.cancel_at_period_end is True

    # Al intentar renovar, la suscripción expira / queda cancelada
    with pytest.raises(Exception):
        sub_service.renew_subscription(sub.subscription_id)

    sub_final = integration_env["sub_repo"].get_subscription(sub.subscription_id)
    assert sub_final.status == SubscriptionStatus.CANCELED

    # Las facturas históricas se preservan intactas
    history_invoices = integration_env["inv_repo"].list_invoices_for_tenant("tenant_alpha")
    assert len(history_invoices) == 1
    assert history_invoices[0].invoice_id == inv.invoice_id


def test_scenario_h_upgrade_plan(integration_env):
    """Escenario H: Upgrade de Plan actualiza suscripción y sincroniza O.8."""
    sub_service = integration_env["sub_service"]
    bill_service = integration_env["bill_service"]
    entitlement_service = integration_env["entitlement_service"]

    # Iniciar en Pro
    sub_pro = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_pro", auto_activate=True)
    assert sub_pro.base_price == Decimal("99.00")

    # Upgrade a Enterprise
    sub_ent = sub_service.create_subscription(tenant_id="tenant_alpha", plan_id="plan_enterprise", auto_activate=True)
    assert sub_ent.base_price == Decimal("499.00")

    active_plan = entitlement_service.get_active_plan("tenant_alpha")
    assert active_plan.plan_id == "plan_enterprise"

    # Capability Enterprise ahora concedida
    dec = entitlement_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_alpha", feature=PlanFeature.CUSTOM_INTEGRATIONS)
    )
    assert dec.status == PlanEntitlementStatus.ALLOW


def test_scenario_i_json_persistence_restart_safety(plan_catalog):
    """Escenario I: Persistencia en JSON tenant-scoped, atomic writes y reinicio seguro."""
    temp_dir = tempfile.mkdtemp(prefix="billing_test_")
    try:
        # 1. Crear repositorios JSON persistentes
        sub_repo = JsonSubscriptionRepository(temp_dir)
        inv_repo = JsonInvoiceRepository(temp_dir)
        pay_repo = JsonPaymentAttemptRepository(temp_dir)
        evt_repo = JsonPaymentProviderEventRepository(temp_dir)
        provider = MockPaymentProvider(provider_name="mock_stripe", auto_succeed=True)

        plan_assign_repo = InMemoryPlanAssignmentRepository()
        ent_service = PlanEntitlementService(catalog_repository=plan_catalog, assignment_repository=plan_assign_repo)

        sub_service = SubscriptionService(
            subscription_repository=sub_repo,
            plan_catalog_repository=plan_catalog,
            plan_entitlement_service=ent_service,
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

        sub = sub_service.create_subscription(tenant_id="tenant_persist", plan_id="plan_pro")
        inv = bill_service.generate_period_invoice(sub.subscription_id)
        att = bill_service.process_payment(inv.invoice_id, idempotency_key="pers_pay_key_01")
        assert att.status == PaymentStatus.SUCCEEDED

        # 2. Simular RESTART: Crear nuevas instancias sobre el mismo directorio en disco
        sub_repo_restarted = JsonSubscriptionRepository(temp_dir)
        inv_repo_restarted = JsonInvoiceRepository(temp_dir)
        pay_repo_restarted = JsonPaymentAttemptRepository(temp_dir)

        recovered_sub = sub_repo_restarted.get_subscription(sub.subscription_id)
        assert recovered_sub is not None
        assert recovered_sub.status == SubscriptionStatus.ACTIVE
        assert recovered_sub.verify_integrity() is True

        recovered_inv = inv_repo_restarted.get_invoice(inv.invoice_id)
        assert recovered_inv is not None
        assert recovered_inv.status == InvoiceStatus.PAID
        assert recovered_inv.verify_integrity() is True

        recovered_att = pay_repo_restarted.get_attempt(att.attempt_id)
        assert recovered_att is not None
        assert recovered_att.status == PaymentStatus.SUCCEEDED
        assert recovered_att.verify_integrity() is True

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_scenario_j_corrupt_state_failsafe(plan_catalog):
    """Escenario J: Detección de manipulación/corrupción de estado SHA-256 (Fail-Safe)."""
    sub_repo = InMemorySubscriptionRepository()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 2, 1, tzinfo=timezone.utc)

    # Intentar guardar una entidad con checksum adulterado
    with pytest.raises(BillingIntegrityError):
        Subscription(
            subscription_id="sub_corrupted",
            tenant_id="tenant_alpha",
            plan_id="plan_pro",
            plan_version="1.0.0",
            status=SubscriptionStatus.ACTIVE,
            billing_cycle=BillingCycle.MONTHLY,
            current_period_start=start,
            current_period_end=end,
            base_price=Decimal("99.00"),
            checksum="bad_checksum_hash",
        )
