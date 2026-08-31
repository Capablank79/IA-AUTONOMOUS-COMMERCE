import pytest
from datetime import datetime, timezone
from decimal import Decimal

from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.returns.models import (
    Claim,
    ClaimStage,
    ClaimStatus,
    RefundDetail,
    RefundStatus,
    Return,
    ReturnError,
    ReturnErrorCategory,
    ReturnEvent,
    ReturnQueryResult,
    ReturnReason,
    ReturnReconciliationReport,
    ReturnResolution,
    ReturnStatus,
)
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


def test_refund_detail_creation_and_immutability():
    refund = RefundDetail(
        refund_id="ref_100",
        external_refund_id="ext_ref_200",
        status=RefundStatus.CONFIRMED,
        amount=Decimal("45.50"),
        currency="USD",
        correlation_id="corr_ref_1",
        idempotency_key="ikey_ref_1",
    )
    assert refund.refund_id == "ref_100"
    assert refund.status == RefundStatus.CONFIRMED
    assert refund.amount == Decimal("45.50")
    assert refund.is_terminal is True
    assert refund.provenance == EvidenceProvenanceType.LIVE
    assert refund.confidence == Confidence.HIGH

    with pytest.raises(Exception):
        refund.status = RefundStatus.FAILED  # type: ignore


def test_return_event_and_immutability():
    now = datetime.now(timezone.utc)
    ev = ReturnEvent(
        event_id="rev_1",
        return_id="ret_01",
        external_return_id="ext_ret_01",
        from_status=ReturnStatus.REQUESTED,
        to_status=ReturnStatus.APPROVED,
        timestamp=now,
        description="Return request approved by seller policy",
        correlation_id="corr_ev_1",
    )
    assert ev.event_id == "rev_1"
    assert ev.from_status == ReturnStatus.REQUESTED
    assert ev.to_status == ReturnStatus.APPROVED

    with pytest.raises(Exception):
        ev.to_status = ReturnStatus.RESOLVED  # type: ignore


def test_claim_creation_and_lifecycle():
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre Chile")
    claim = Claim(
        claim_id="clm_01",
        external_claim_id="ext_clm_01",
        order_id="ord_01",
        external_order_id="ext_ord_01",
        channel=channel,
        status=ClaimStatus.OPENED,
        stage=ClaimStage.CLAIM,
        reason=ReturnReason.DEFECTIVE,
    )
    assert claim.claim_id == "clm_01"
    assert claim.status == ClaimStatus.OPENED
    assert claim.stage == ClaimStage.CLAIM
    assert claim.is_terminal is False


def test_return_aggregate_and_properties():
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre Chile")
    ev1 = ReturnEvent(
        event_id="ev1",
        return_id="ret_01",
        external_return_id="ext_01",
        from_status=ReturnStatus.REQUESTED,
        to_status=ReturnStatus.IN_TRANSIT,
        timestamp=datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone.utc),
    )
    ev2 = ReturnEvent(
        event_id="ev2",
        return_id="ret_01",
        external_return_id="ext_01",
        from_status=ReturnStatus.IN_TRANSIT,
        to_status=ReturnStatus.RECEIVED,
        timestamp=datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    refund = RefundDetail(
        refund_id="ref_01",
        status=RefundStatus.CONFIRMED,
        amount=Decimal("99.90"),
        currency="USD",
    )

    ret = Return(
        return_id="ret_01",
        external_return_id="ext_01",
        order_id="ord_01",
        external_order_id="ext_ord_01",
        channel=channel,
        status=ReturnStatus.RESOLVED,
        reason=ReturnReason.DEFECTIVE,
        resolution=ReturnResolution.REFUND,
        shipment_id="shp_01",
        external_shipment_id="ext_shp_01",
        refund=refund,
        events=(ev1, ev2),
    )

    assert ret.return_id == "ret_01"
    assert ret.is_terminal is True
    assert ret.latest_event == ev2
    assert len(ret.events) == 2
    assert ret.refund.status == RefundStatus.CONFIRMED


def test_return_reconciliation_report():
    report = ReturnReconciliationReport(
        return_id="ret_01",
        external_return_id="ext_01",
        order_id="ord_01",
        external_order_id="ext_ord_01",
        is_reconciled=False,
        internal_status=ReturnStatus.REQUESTED,
        external_status=ReturnStatus.RECEIVED,
        internal_refund_status=RefundStatus.NOT_REQUESTED,
        external_refund_status=RefundStatus.PROCESSING,
        refund_reconciled=False,
        discrepancies=("Status mismatch: local=REQUESTED, external=RECEIVED",),
        requires_action=True,
    )
    assert report.is_reconciled is False
    assert report.requires_action is True
    assert len(report.discrepancies) == 1
    assert report.refund_reconciled is False
