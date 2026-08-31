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
    ReturnQueryResult,
)
from src.domain.returns.ports import ReturnsPort
from src.domain.returns.rules import ReturnActionPolicyRule
from src.domain.order.models import Order, OrderStatus, PaymentStatus, FulfillmentStatus, OrderItem, BuyerReference
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.policy.engine import PolicyEngine
from src.application.returns.returns_service import ReturnsService
from src.infrastructure.persistence.data.in_memory.returns_repository import InMemoryReturnsRepository


class MockReturnsPort(ReturnsPort):
    def __init__(self):
        self.returns_db = {}
        self.claims_db = {}
        self.refund_calls = []
        self.fail_mode = False

    def fetch_returns(self, channel: SalesChannel, status=None, limit=50, offset=0, correlation_id=""):
        return ReturnQueryResult(returns=list(self.returns_db.values()))

    def get_return_by_external_id(self, external_return_id: str, channel: SalesChannel, correlation_id=""):
        if self.fail_mode:
            from src.domain.returns.models import ReturnError, ReturnErrorCategory
            return ReturnQueryResult(returns=[], errors=[ReturnError(category=ReturnErrorCategory.TIMEOUT, message="Gateway timeout")])
        if external_return_id in self.returns_db:
            return ReturnQueryResult(returns=[self.returns_db[external_return_id]])
        return ReturnQueryResult(returns=[])

    def get_return_by_external_order_id(self, external_order_id: str, channel: SalesChannel, correlation_id=""):
        for ret in self.returns_db.values():
            if ret.external_order_id == external_order_id:
                return ReturnQueryResult(returns=[ret])
        return ReturnQueryResult(returns=[])

    def get_claim_by_external_id(self, external_claim_id: str, channel: SalesChannel, correlation_id=""):
        return self.claims_db.get(external_claim_id)

    def create_return_request(self, external_order_id: str, channel: SalesChannel, reason: str, details="", correlation_id="", idempotency_key=""):
        ext_id = f"ext-ret-{external_order_id}"
        ret = Return(
            return_id=f"ret-{external_order_id}",
            external_return_id=ext_id,
            order_id=f"ord-{external_order_id}",
            external_order_id=external_order_id,
            channel=channel,
            status=ReturnStatus.REQUESTED,
            reason=ReturnReason.DAMAGED,
            resolution=ReturnResolution.RETURN_ONLY,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        self.returns_db[ext_id] = ret
        return ReturnQueryResult(returns=[ret])

    def execute_refund(self, external_return_id: str, external_order_id: str, amount: Decimal, currency: str, channel: SalesChannel = None, correlation_id="", idempotency_key=""):
        self.refund_calls.append({
            "external_return_id": external_return_id,
            "external_order_id": external_order_id,
            "amount": amount,
            "currency": currency,
            "idempotency_key": idempotency_key,
        })
        return RefundDetail(
            refund_id=f"ref-{external_return_id}",
            status=RefundStatus.CONFIRMED,
            amount=amount,
            currency=currency,
            external_refund_id=f"ext-ref-{external_return_id}",
        )


def _make_dummy_order(order_id="ORD-100", ext_order_id="EXT-ORD-100") -> Order:
    return Order(
        order_id=order_id,
        external_order_id=ext_order_id,
        channel=SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre"),
        status=OrderStatus.CLOSED,
        payment_status=PaymentStatus.APPROVED,
        fulfillment_status=FulfillmentStatus.DELIVERED,
        buyer=BuyerReference(buyer_id="cust-1", nickname="Juan Perez"),
        items=(
            OrderItem(item_id="item-1", sku="SKU-TEST-1", title="Producto Test", quantity=1, unit_price=Decimal("45.00"), currency="CLP"),
        ),
        total_amount=Decimal("45.00"),
        currency="CLP",
    )


def test_returns_service_create_return_request():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    service = ReturnsService(returns_repository=repo, returns_port=adapter)

    order = _make_dummy_order()
    ret = service.create_return_request(
        order=order,
        reason=ReturnReason.DAMAGED,
        correlation_id="corr-1",
        idempotency_key="idem-1",
    )

    assert ret.status == ReturnStatus.REQUESTED
    assert ret.external_order_id == "EXT-ORD-100"
    assert ret.reason == ReturnReason.DAMAGED

    # Verificar que se guardó en repo
    saved = repo.get_return_by_id(ret.return_id)
    assert saved is not None
    assert saved.return_id == ret.return_id


def test_returns_service_sync_return_success():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    service = ReturnsService(returns_repository=repo, returns_port=adapter)

    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")
    # Adapter tiene un retorno
    adapter.returns_db["ext-ret-999"] = Return(
        return_id="local-ret-999",
        external_return_id="ext-ret-999",
        order_id="ord-999",
        external_order_id="ext-ord-999",
        channel=SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre"),
        status=ReturnStatus.RECEIVED,
        reason=ReturnReason.DEFECTIVE,
    )

    synced = service.sync_return(
        external_return_id="ext-ret-999",
        channel=channel,
        correlation_id="corr-sync",
    )

    assert synced is not None
    assert synced.status == ReturnStatus.RECEIVED
    # Validar persistencia
    persisted = repo.get_return_by_external_id("ext-ret-999", "meli_chile")
    assert persisted is not None
    assert persisted.status == ReturnStatus.RECEIVED


def test_returns_service_sync_return_unknown_on_error():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    adapter.fail_mode = True
    service = ReturnsService(returns_repository=repo, returns_port=adapter)

    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")
    # Si ya existía localmente, no debe sobreescribirse destructivamente
    local_ret = Return(
        return_id="local-ret-1",
        external_return_id="ext-ret-1",
        order_id="ord-1",
        external_order_id="ext-ord-1",
        channel=channel,
        status=ReturnStatus.APPROVED,
    )
    repo.save_return(local_ret)

    synced = service.sync_return(
        external_return_id="ext-ret-1",
        channel=channel,
        correlation_id="corr-err",
    )

    assert synced is None
    # El estado local se preserva
    assert repo.get_return_by_id("local-ret-1").status == ReturnStatus.APPROVED


def test_returns_service_record_return_event_idempotent():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    service = ReturnsService(returns_repository=repo, returns_port=adapter)

    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")
    ret = Return(
        return_id="ret-evt-1",
        external_return_id="ext-ret-evt-1",
        order_id="ord-1",
        external_order_id="ext-ord-1",
        channel=channel,
        status=ReturnStatus.REQUESTED,
    )
    repo.save_return(ret)

    evt = ReturnEvent(
        event_id="evt-unique-123",
        return_id="ret-evt-1",
        external_return_id="ext-ret-evt-1",
        from_status=ReturnStatus.REQUESTED,
        to_status=ReturnStatus.APPROVED,
        description="RETURN_APPROVED",
    )

    # Primer evento -> procesado
    assert service.record_return_event(evt, channel_id="meli_chile") is True
    updated = repo.get_return_by_id("ret-evt-1")
    assert updated.status == ReturnStatus.APPROVED
    assert len(updated.events) == 1

    # Segundo evento con el mismo event_id -> ignorado por idempotencia
    assert service.record_return_event(evt, channel_id="meli_chile") is False
    assert len(repo.get_return_by_id("ret-evt-1").events) == 1


def test_returns_service_reconcile_return_clean():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    service = ReturnsService(returns_repository=repo, returns_port=adapter)

    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")
    ret = Return(
        return_id="ret-rec-1",
        external_return_id="ext-ret-rec-1",
        order_id="ord-1",
        external_order_id="ext-ord-1",
        channel=channel,
        status=ReturnStatus.IN_TRANSIT,
        reason=ReturnReason.WRONG_ITEM,
        resolution=ReturnResolution.RETURN_ONLY,
    )
    repo.save_return(ret)
    adapter.returns_db["ext-ret-rec-1"] = ret

    report = service.reconcile_return(return_id="ret-rec-1", channel=channel)

    assert report.is_reconciled is True
    assert len(report.discrepancies) == 0
    assert report.requires_action is False


def test_returns_service_reconcile_return_with_discrepancy():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    service = ReturnsService(returns_repository=repo, returns_port=adapter)

    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")
    local_ret = Return(
        return_id="ret-rec-2",
        external_return_id="ext-ret-rec-2",
        order_id="ord-2",
        external_order_id="ext-ord-2",
        channel=channel,
        status=ReturnStatus.REQUESTED,
        reason=ReturnReason.DAMAGED,
        resolution=ReturnResolution.RETURN_ONLY,
    )
    repo.save_return(local_ret)

    # Estado externo difiere: fue recibido y tiene resolución REFUND
    external_ret = Return(
        return_id="ret-rec-2",
        external_return_id="ext-ret-rec-2",
        order_id="ord-2",
        external_order_id="ext-ord-2",
        channel=channel,
        status=ReturnStatus.RECEIVED,
        reason=ReturnReason.DAMAGED,
        resolution=ReturnResolution.REFUND,
    )
    adapter.returns_db["ext-ret-rec-2"] = external_ret

    report = service.reconcile_return(return_id="ret-rec-2", channel=channel)

    assert report.is_reconciled is False
    assert len(report.discrepancies) == 2
    assert "status: local REQUESTED != external RECEIVED" in report.discrepancies
    assert "resolution: local RETURN_ONLY != external REFUND" in report.discrepancies
    assert report.requires_action is True


def test_returns_service_execute_action_guarded_policy_allow():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    rule = ReturnActionPolicyRule(max_autonomous_refund_amount=Decimal("100.00"))
    policy_engine = PolicyEngine(rules=[rule])
    service = ReturnsService(returns_repository=repo, returns_port=adapter, policy_engine=policy_engine)

    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")
    ret = Return(
        return_id="ret-pol-1",
        external_return_id="ext-ret-pol-1",
        order_id="ord-pol-1",
        external_order_id="ext-ord-pol-1",
        channel=channel,
        status=ReturnStatus.RECEIVED,
    )
    repo.save_return(ret)

    # Reembolso de $50.00 -> dentro del umbral de $100.00 -> ALLOW
    evaluation, result = service.execute_return_action_guarded(
        action_type="ISSUE_REFUND",
        return_id="ret-pol-1",
        channel=channel,
        amount=Decimal("50.00"),
        currency="CLP",
        idempotency_key="idem-refund-1",
    )

    assert evaluation.decision.value == "ALLOW"
    assert result is not None
    assert isinstance(result, RefundDetail)
    assert result.status == RefundStatus.CONFIRMED

    # Validar que se actualizó el aggregate Return con el RefundDetail
    updated = repo.get_return_by_id("ret-pol-1")
    assert updated.refund is not None
    assert updated.refund.status == RefundStatus.CONFIRMED


def test_returns_service_execute_action_guarded_policy_requires_approval():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    rule = ReturnActionPolicyRule(max_autonomous_refund_amount=Decimal("100.00"))
    policy_engine = PolicyEngine(rules=[rule])
    service = ReturnsService(returns_repository=repo, returns_port=adapter, policy_engine=policy_engine)

    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")
    ret = Return(
        return_id="ret-pol-2",
        external_return_id="ext-ret-pol-2",
        order_id="ord-pol-2",
        external_order_id="ext-ord-pol-2",
        channel=channel,
        status=ReturnStatus.RECEIVED,
    )
    repo.save_return(ret)

    # Reembolso de $250.00 -> supera umbral de $100.00 sin human_approved -> REQUIRE_APPROVAL
    evaluation, result = service.execute_return_action_guarded(
        action_type="ISSUE_REFUND",
        return_id="ret-pol-2",
        channel=channel,
        amount=Decimal("250.00"),
        currency="CLP",
        human_approved=False,
    )

    assert evaluation.decision.value == "REQUIRE_APPROVAL"
    assert result is None
    assert len(adapter.refund_calls) == 0


def test_returns_service_execute_action_guarded_idempotency():
    repo = InMemoryReturnsRepository()
    adapter = MockReturnsPort()
    rule = ReturnActionPolicyRule(max_autonomous_refund_amount=Decimal("100.00"))
    policy_engine = PolicyEngine(rules=[rule])
    service = ReturnsService(returns_repository=repo, returns_port=adapter, policy_engine=policy_engine)

    channel = SalesChannel(channel_id="meli_chile", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre")
    ret = Return(
        return_id="ret-pol-3",
        external_return_id="ext-ret-pol-3",
        order_id="ord-pol-3",
        external_order_id="ext-ord-pol-3",
        channel=channel,
        status=ReturnStatus.RECEIVED,
    )
    repo.save_return(ret)

    # 1era llamada
    eval1, res1 = service.execute_return_action_guarded(
        action_type="ISSUE_REFUND",
        return_id="ret-pol-3",
        channel=channel,
        amount=Decimal("30.00"),
        currency="CLP",
        idempotency_key="idem-refund-repeat",
    )
    assert eval1.decision.value == "ALLOW"
    assert res1 is not None
    assert len(adapter.refund_calls) == 1

    # 2da llamada con la misma idempotency_key -> bloqueada por idempotencia
    eval2, res2 = service.execute_return_action_guarded(
        action_type="ISSUE_REFUND",
        return_id="ret-pol-3",
        channel=channel,
        amount=Decimal("30.00"),
        currency="CLP",
        idempotency_key="idem-refund-repeat",
    )
    assert eval2.decision.value == "DENY"
    assert res2 is None
    # No se generó una segunda llamada de refund en el adapter
    assert len(adapter.refund_calls) == 1
