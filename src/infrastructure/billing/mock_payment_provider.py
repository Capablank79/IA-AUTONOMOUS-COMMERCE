"""
Adaptadores de infraestructura para Billing & Subscription Management SaaS (Hito O.9 — Billing & Subscription Management).

Define:
- MockPaymentProvider: Adaptador determinista desacoplado para pruebas y simulación controlada de pasarelas de pago.
- Soporta simulación de éxito, rechazo/fallo, idempotencia, timeouts y generación de webhooks.
- Cumple con N.5 (Secret Management) y N.9 (Sensitive Data Handling): nunca almacena ni manipula tarjetas reales.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import threading
from types import MappingProxyType
from typing import Optional, Dict, Mapping, Any, List
import uuid

from src.domain.billing.models import (
    Money,
    PaymentStatus,
    PaymentAttempt,
    PaymentProviderReference,
    PaymentProviderEvent,
    PaymentProviderError,
    BillingError,
    BillingIntegrityError,
)
from src.domain.billing.ports import PaymentProviderPort
from src.domain.secrets.models import SecretValue, SecretReference


class MockPaymentProvider(PaymentProviderPort):
    """
    Adaptador Mock/Testable desacoplado de pasarela de pago para O.9.
    Simula cobros directos, generación de sesiones de checkout y respuestas de webhooks.
    """

    def __init__(
        self,
        provider_name: str = "mock_stripe",
        auto_succeed: bool = True,
        fail_reason: Optional[str] = None,
        secret_reference: Optional[SecretReference] = None,
    ):
        self.provider_name = provider_name.strip().lower()
        self.auto_succeed = auto_succeed
        self.fail_reason = fail_reason
        self.secret_reference = secret_reference
        self._payments: Dict[str, Dict[str, Any]] = {}
        self._checkout_sessions: Dict[str, PaymentProviderReference] = {}
        self._lock = threading.RLock()

    def set_auto_succeed(self, succeed: bool, fail_reason: Optional[str] = None) -> None:
        with self._lock:
            self.auto_succeed = succeed
            self.fail_reason = fail_reason

    def create_checkout_session(
        self,
        tenant_id: str,
        subscription_id: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> PaymentProviderReference:
        with self._lock:
            # Idempotencia: si ya existe una sesión con esa clave, retornar la misma referencia
            session_key = f"chk_{idempotency_key}"
            if session_key in self._checkout_sessions:
                return self._checkout_sessions[session_key]

            ref_id = f"cs_{uuid.uuid4().hex[:16]}"
            cust_ref = f"cus_{hashlib.sha256(tenant_id.encode('utf-8')).hexdigest()[:12]}"
            checkout_url = f"https://mock-billing.domain.internal/checkout/{ref_id}"

            ref = PaymentProviderReference(
                provider=self.provider_name,
                reference_id=ref_id,
                customer_reference=cust_ref,
                checkout_url=checkout_url,
            )
            self._checkout_sessions[session_key] = ref
            return ref

    def charge_invoice(
        self,
        tenant_id: str,
        invoice_id: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> PaymentAttempt:
        with self._lock:
            attempt_id = f"payatt_{uuid.uuid4().hex[:16]}"
            now = datetime.now(timezone.utc)

            if self.auto_succeed:
                status = PaymentStatus.SUCCEEDED
                provider_ref = f"tx_{uuid.uuid4().hex[:16]}"
                error_msg = None
            else:
                status = PaymentStatus.FAILED
                provider_ref = f"tx_failed_{uuid.uuid4().hex[:12]}"
                error_msg = self.fail_reason or "Card declined by issuing bank (mock)."

            attempt = PaymentAttempt(
                attempt_id=attempt_id,
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                amount=amount,
                currency=currency,
                status=status,
                provider=self.provider_name,
                idempotency_key=idempotency_key,
                provider_reference=provider_ref,
                error_message=error_msg,
                attempted_at=now,
                completed_at=now,
                metadata=metadata or {},
            )

            self._payments[attempt_id] = {
                "attempt": attempt,
                "status": status,
                "provider_ref": provider_ref,
            }

            return attempt

    def fetch_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        with self._lock:
            for item in self._payments.values():
                if item.get("provider_ref") == provider_payment_id:
                    return item["status"]
            return PaymentStatus.UNKNOWN

    def generate_mock_webhook_event(
        self,
        event_type: str,
        tenant_id: str,
        idempotency_key: str,
        subscription_id: Optional[str] = None,
        invoice_id: Optional[str] = None,
        provider_payment_id: Optional[str] = None,
        amount: Optional[Decimal] = None,
        currency: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> PaymentProviderEvent:
        """Helper para pruebas: genera un PaymentProviderEvent válido."""
        event_id = f"pevt_{uuid.uuid4().hex[:16]}"
        return PaymentProviderEvent(
            event_id=event_id,
            provider=self.provider_name,
            event_type=event_type,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            subscription_id=subscription_id,
            invoice_id=invoice_id,
            provider_payment_id=provider_payment_id or f"tx_{uuid.uuid4().hex[:12]}",
            amount=amount,
            currency=currency or "USD",
            occurred_at=datetime.now(timezone.utc),
            metadata=metadata or {},
        )
