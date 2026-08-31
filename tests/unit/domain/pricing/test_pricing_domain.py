from decimal import Decimal
import pytest
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.pricing.models import (
    PriceChangeReason,
    PricingStatus,
    PricingErrorCategory,
    PricingError,
    PricingDecision,
    PricingAction,
    PricingRequest,
    PricingResult,
)
from src.domain.pricing.engine import PricingDecisionEngine
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel


def test_pricing_models_creation_and_properties():
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre Chile")
    
    # 1. PricingDecision below floor
    decision = PricingDecision(
        decision_id="dec_001",
        listing_id="MLC12345",
        channel=channel,
        current_price=Decimal("15000"),
        proposed_price=Decimal("9000"),
        minimum_allowed_price=Decimal("10000"),
        rationale="Match aggressive competitor",
    )
    assert decision.is_below_floor is True
    assert decision.price_delta == Decimal("-6000")
    assert decision.price_delta_pct == Decimal("-0.4")

    # 2. PricingDecision above floor
    decision_ok = PricingDecision(
        decision_id="dec_002",
        listing_id="MLC12345",
        channel=channel,
        current_price=Decimal("15000"),
        proposed_price=Decimal("12000"),
        minimum_allowed_price=Decimal("10000"),
    )
    assert decision_ok.is_below_floor is False
    assert decision_ok.price_delta == Decimal("-3000")

    # 3. PricingResult properties
    res_ok = PricingResult(
        pricing_id="res_001",
        channel=channel,
        status=PricingStatus.APPLIED,
        listing_id="MLC12345",
        applied_price=Decimal("12000"),
        previous_price=Decimal("15000"),
    )
    assert res_ok.is_success is True
    assert res_ok.is_unknown is False

    res_unknown = PricingResult(
        pricing_id=None,
        channel=channel,
        status=PricingStatus.UNKNOWN,
        listing_id="MLC12345",
        errors=(PricingError(category=PricingErrorCategory.UNKNOWN, message="timeout"),),
    )
    assert res_unknown.is_success is False
    assert res_unknown.is_unknown is True


def test_pricing_decision_engine_calculate_price_floor():
    # Costo landed 5000, comision 13%, pago 3%, margen minimo 10%
    floor = PricingDecisionEngine.calculate_price_floor(
        unit_landed_cost=Decimal("5000"),
        marketplace_fee_rate=Decimal("0.13"),
        payment_fee_rate=Decimal("0.03"),
        shipping_cost=Decimal("1000"),
        minimum_net_margin_pct=Decimal("0.10"),
    )
    assert floor > Decimal("6000")
    # A ese precio floor, el margen neto debe ser al menos 10%
    ue = PricingDecisionEngine.evaluate_price_economics(
        proposed_price=floor,
        unit_landed_cost=Decimal("5000"),
        marketplace_fee_rate=Decimal("0.13"),
        payment_fee_rate=Decimal("0.03"),
        shipping_cost=Decimal("1000"),
    )
    assert ue.net_margin_pct >= Decimal("0.099")  # Tolerancia por redondeo


def test_pricing_decision_engine_propose_decision_and_action():
    channel = SalesChannel(channel_id="ML-CL", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre Chile")

    decision = PricingDecisionEngine.propose_pricing_decision(
        listing_id="MLC12345",
        channel=channel,
        current_price=Decimal("15000"),
        proposed_price=Decimal("13000"),
        unit_landed_cost=Decimal("6000"),
        marketplace_fee_rate=Decimal("0.13"),
        payment_fee_rate=Decimal("0.03"),
        shipping_cost=Decimal("1000"),
        minimum_net_margin_pct=Decimal("0.10"),
        reason=PriceChangeReason.COMPETITIVE_MATCH,
        rationale="Follow price drop from top competitor",
        evidence={"competitor_price": 13500},
        confidence=Confidence.HIGH,
        risk_level=RiskLevel.LOW,
    )

    assert decision.is_below_floor is False
    assert decision.expected_margin_pct is not None
    assert decision.expected_margin_pct > Decimal("0.10")
    assert decision.requires_approval is False

    action = PricingDecisionEngine.create_pricing_action(decision)
    assert isinstance(action, PricingAction)
    assert action.listing_id == "MLC12345"
    assert action.proposed_price == Decimal("13000")
    assert action.current_price == Decimal("15000")
    assert action.reason == PriceChangeReason.COMPETITIVE_MATCH
