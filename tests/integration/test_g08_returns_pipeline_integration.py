import pytest
from decimal import Decimal
from datetime import datetime, timezone

from src.domain.returns.models import (
    Return,
    ReturnStatus,
    ReturnReason,
    ReturnResolution,
    RefundStatus,
    RefundDetail,
    ReturnEvent,
    Claim,
    ClaimStatus,
    ReturnQueryResult,
    ReturnError,
)
from src.domain.returns.rules import ReturnActionPolicyRule
from src.domain.order.models import Order, OrderStatus, PaymentStatus, FulfillmentStatus, OrderItem, BuyerReference
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.policy.engine import PolicyEngine
from src.application.returns.returns_service import ReturnsService
from src.infrastructure.persistence.data.in_memory.returns_repository import InMemoryReturnsRepository
from src.infrastructure.mercadolibre.returns_adapter import MercadoLibreReturnsAdapter
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient


class MockApiClient(MercadoLibreApiClient):
    """API Client mockeado para simular respuestas de Mercado Libre sin red."""
    def __init__(self):
        super().__init__(access_token="test_token")
        self.responses = {}
        self.post_responses = {}
        self.recorded_posts = []

    def get(self, endpoint: str, params=None):
        if endpoint in self.responses:
            resp = self.responses[endpoint]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {}

    def post(self, endpoint: str, payload=None, params=None):
        self.recorded_posts.append({"endpoint": endpoint, "payload": payload})
        if endpoint in self.post_responses:
            resp = self.post_responses[endpoint]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {}


@pytest.fixture
def test_setup():
    api_client = MockApiClient()
    adapter = MercadoLibreReturnsAdapter(api_client=api_client)
    repo = InMemoryReturnsRepository()
    rule = ReturnActionPolicyRule(max_autonomous_refund_amount=Decimal("100.00"))
    policy_engine = PolicyEngine(rules=[rule])
    service = ReturnsService(returns_repository=repo, returns_port=adapter, policy_engine=policy_engine)
    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")

    order = Order(
        order_id="ORD-INT-001",
        external_order_id="EXT-ORD-INT-001",
        channel=channel,
        status=OrderStatus.CLOSED,
        payment_status=PaymentStatus.APPROVED,
        fulfillment_status=FulfillmentStatus.DELIVERED,
        buyer=BuyerReference(buyer_id="cust-1", nickname="Maria Silva"),
        items=(
            OrderItem(item_id="item-1", sku="SKU-PHONE-1", title="Smartphone X", quantity=1, unit_price=Decimal("90.00"), currency="CLP"),
        ),
        total_amount=Decimal("90.00"),
        currency="CLP",
    )
    return {
        "api_client": api_client,
        "adapter": adapter,
        "repo": repo,
        "policy_engine": policy_engine,
        "service": service,
        "channel": channel,
        "order": order,
    }


def test_scenario_a_return_happy_path(test_setup):
    """
    Escenario A — Return happy path:
    Orden válida -> return observado -> estados normalizados -> lifecycle válido -> reconciliación limpia.
    """
    api_client = test_setup["api_client"]
    service = test_setup["service"]
    repo = test_setup["repo"]
    channel = test_setup["channel"]
    order = test_setup["order"]

    # 1. Simular respuesta externa de ML para una devolución en tránsito
    api_client.responses["/post-purchase/v1/returns/RET-EXT-100"] = {
        "id": "RET-EXT-100",
        "order_id": order.external_order_id,
        "status": "shipped",
        "reason": "defective",
        "resolution": "refund",
        "date_created": "2026-08-20T10:00:00Z",
    }

    # 2. Sincronizar devolución desde adapter
    ret = service.sync_return(external_return_id="RET-EXT-100", channel=channel, correlation_id="corr-a")
    assert ret is not None
    assert ret.status == ReturnStatus.IN_TRANSIT
    assert ret.reason == ReturnReason.DEFECTIVE
    assert ret.resolution == ReturnResolution.REFUND
    assert ret.external_order_id == order.external_order_id

    # 3. Registrar evento de llegada al centro de distribución (lifecycle transition)
    evt = ReturnEvent(
        event_id="EVT-A-1",
        return_id=ret.return_id,
        external_return_id="RET-EXT-100",
        from_status=ReturnStatus.IN_TRANSIT,
        to_status=ReturnStatus.RECEIVED,
        description="RETURN_RECEIVED",
        timestamp=datetime.now(timezone.utc),
    )
    processed = service.record_return_event(evt, channel_id="meli_chile")
    assert processed is True

    # 4. Actualizar mock externo para reflejar recepción y reconciliar
    api_client.responses["/post-purchase/v1/returns/RET-EXT-100"]["status"] = "delivered"
    report = service.reconcile_return(return_id=ret.return_id, channel=channel)

    # 5. Validar consistencia limpia
    assert report.is_reconciled is True
    assert len(report.discrepancies) == 0
    assert report.requires_action is False


def test_scenario_b_duplicate_events_idempotency(test_setup):
    """
    Escenario B — Duplicate:
    Mismo evento dos veces -> primero procesado -> segundo DUPLICATE_IGNORED -> cero efectos duplicados.
    """
    service = test_setup["service"]
    repo = test_setup["repo"]
    channel = test_setup["channel"]

    ret = Return(
        return_id="RET-DUP-1",
        external_return_id="EXT-DUP-1",
        order_id="ORD-DUP-1",
        external_order_id="EXT-ORD-DUP-1",
        channel=channel,
        status=ReturnStatus.REQUESTED,
    )
    repo.save_return(ret)

    evt = ReturnEvent(
        event_id="EVT-DUP-UNIQUE-999",
        return_id="RET-DUP-1",
        external_return_id="EXT-DUP-1",
        from_status=ReturnStatus.REQUESTED,
        to_status=ReturnStatus.APPROVED,
        description="RETURN_APPROVED",
    )

    # Primera entrega del webhook -> procesado con éxito
    first_res = service.record_return_event(evt, channel_id="meli_chile")
    assert first_res is True
    updated = repo.get_return_by_id("RET-DUP-1")
    assert updated.status == ReturnStatus.APPROVED
    assert len(updated.events) == 1

    # Segunda entrega duplicada del mismo webhook -> ignorado sin mutaciones extra
    second_res = service.record_return_event(evt, channel_id="meli_chile")
    assert second_res is False
    assert len(repo.get_return_by_id("RET-DUP-1").events) == 1


def test_scenario_c_unknown_and_recovery(test_setup):
    """
    Escenario C — UNKNOWN:
    Timeout / 5xx -> UNKNOWN -> no asumir refund/return exitoso -> preservar estado local.
    """
    api_client = test_setup["api_client"]
    service = test_setup["service"]
    repo = test_setup["repo"]
    channel = test_setup["channel"]

    local_ret = Return(
        return_id="RET-UNK-1",
        external_return_id="EXT-UNK-1",
        order_id="ORD-UNK-1",
        external_order_id="EXT-ORD-UNK-1",
        channel=channel,
        status=ReturnStatus.INSPECTING,
    )
    repo.save_return(local_ret)

    # Simular caída de ML (500 Internal Server Error)
    from src.infrastructure.mercadolibre.api_client import MercadoLibreApiError
    api_client.responses["/post-purchase/v1/returns/EXT-UNK-1"] = MercadoLibreApiError("500 Server Error", status_code=500)

    # Sincronización falla elegantemente
    synced = service.sync_return(external_return_id="EXT-UNK-1", channel=channel)
    assert synced is None

    # El estado local no fue destruido ni mutado a un estado falso
    current = repo.get_return_by_id("RET-UNK-1")
    assert current.status == ReturnStatus.INSPECTING

    # Reconciliación ante falla externa no genera excepciones, reporta discrepancia o estado desconocido
    report = service.reconcile_return(return_id="RET-UNK-1", channel=channel)
    assert report.is_reconciled is False
    assert any("not found" in d.lower() or "external" in d.lower() for d in report.discrepancies)


def test_scenario_d_discrepancy_detection(test_setup):
    """
    Escenario D — Discrepancy:
    Estado interno != externo -> discrepancy detectada -> requires_action -> sin overwrite destructivo.
    """
    api_client = test_setup["api_client"]
    service = test_setup["service"]
    repo = test_setup["repo"]
    channel = test_setup["channel"]

    local_ret = Return(
        return_id="RET-DISC-1",
        external_return_id="EXT-DISC-1",
        order_id="ORD-DISC-1",
        external_order_id="EXT-ORD-DISC-1",
        channel=channel,
        status=ReturnStatus.APPROVED,
        reason=ReturnReason.DEFECTIVE,
        resolution=ReturnResolution.RETURN_ONLY,
    )
    repo.save_return(local_ret)

    # En ML el comprador canceló la devolución externamente
    api_client.responses["/post-purchase/v1/returns/EXT-DISC-1"] = {
        "id": "EXT-DISC-1",
        "order_id": "EXT-ORD-DISC-1",
        "status": "cancelled",
        "reason": "defective",
        "resolution": "none",
        "date_created": "2026-08-20T10:00:00Z",
    }

    report = service.reconcile_return(return_id="RET-DISC-1", channel=channel)
    assert report.is_reconciled is False
    assert report.requires_action is True
    assert "status: local APPROVED != external CANCELLED" in report.discrepancies

    # El estado local en base de datos NO se sobreescribe silenciosamente
    assert repo.get_return_by_id("RET-DISC-1").status == ReturnStatus.APPROVED


def test_scenario_e_policy_governance(test_setup):
    """
    Escenario E — Policy:
    Acción externa propuesta -> policy DENY o APPROVAL_REQUIRED cuando corresponda -> no ejecución no autorizada.
    """
    api_client = test_setup["api_client"]
    service = test_setup["service"]
    repo = test_setup["repo"]
    channel = test_setup["channel"]

    ret = Return(
        return_id="RET-POL-E",
        external_return_id="EXT-POL-E",
        order_id="ORD-POL-E",
        external_order_id="EXT-ORD-POL-E",
        channel=channel,
        status=ReturnStatus.RECEIVED,
    )
    repo.save_return(ret)

    # 1. Intento de reembolso de $500.00 (supera umbral de $100.00) sin human_approved
    eval_req, res_req = service.execute_return_action_guarded(
        action_type="ISSUE_REFUND",
        return_id="RET-POL-E",
        channel=channel,
        amount=Decimal("500.00"),
        currency="CLP",
        human_approved=False,
    )
    assert eval_req.decision.value == "REQUIRE_APPROVAL"
    assert res_req is None
    assert len(api_client.recorded_posts) == 0

    # 2. Intento de rechazo de devolución sin human_approved
    eval_rej, res_rej = service.execute_return_action_guarded(
        action_type="REJECT_RETURN",
        return_id="RET-POL-E",
        channel=channel,
        human_approved=False,
    )
    assert eval_rej.decision.value == "REQUIRE_APPROVAL"
    assert res_rej is None


def test_scenario_f_refund_supported_lifecycle_and_idempotency(test_setup):
    """
    Escenario F — Refund, sólo si realmente soportado:
    Return resolution -> refund request -> resultado confirmado -> idempotencia.
    """
    api_client = test_setup["api_client"]
    service = test_setup["service"]
    repo = test_setup["repo"]
    channel = test_setup["channel"]

    ret = Return(
        return_id="RET-REF-F",
        external_return_id="EXT-REF-F",
        order_id="ORD-REF-F",
        external_order_id="EXT-ORD-REF-F",
        channel=channel,
        status=ReturnStatus.RECEIVED,
    )
    repo.save_return(ret)

    # Configurar mock de ML para endpoint de refund
    api_client.post_responses["/post-purchase/v1/returns/EXT-REF-F/refund"] = {
        "refund_id": "REF-ML-888",
        "status": "approved",
        "amount": 75.50,
        "currency_id": "CLP",
        "date_created": "2026-08-25T15:00:00Z",
    }

    # 1. Ejecutar reembolso dentro del umbral de $100 ($75.50)
    evaluation, result = service.execute_return_action_guarded(
        action_type="ISSUE_REFUND",
        return_id="RET-REF-F",
        channel=channel,
        amount=Decimal("75.50"),
        currency="CLP",
        idempotency_key="IDEM-REF-UNIQUE-001",
    )

    assert evaluation.decision.value == "ALLOW"
    assert result is not None
    assert isinstance(result, RefundDetail)
    assert result.status == RefundStatus.CONFIRMED
    assert result.amount == Decimal("75.50")
    assert result.external_refund_id == "REF-ML-888"

    # Verificar que el aggregate Return tiene el refund adjunto
    updated_ret = repo.get_return_by_id("RET-REF-F")
    assert updated_ret.refund is not None
    assert updated_ret.refund.status == RefundStatus.CONFIRMED

    # 2. Reintento de la misma llamada con la misma idempotency_key -> Bloqueado por Idempotencia
    eval_repeat, res_repeat = service.execute_return_action_guarded(
        action_type="ISSUE_REFUND",
        return_id="RET-REF-F",
        channel=channel,
        amount=Decimal("75.50"),
        currency="CLP",
        idempotency_key="IDEM-REF-UNIQUE-001",
    )
    assert eval_repeat.decision.value == "DENY"
    assert res_repeat is None
    # No se disparó un segundo POST a ML
    assert len(api_client.recorded_posts) == 1
