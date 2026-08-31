import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Mapping, Any, Dict

from src.domain.publication.models import SalesChannel
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel
from .models import (
    StockLevel,
    InventoryDecision,
    InventoryAction,
    InventoryChangeReason,
)


class InventoryDecisionEngine:
    """
    Motor determinista de decisiones de stock/inventario (Hito G.5).
    Calcula stock vendible, aplica buffers de seguridad y genera decisiones
    y acciones de inventario respetando estrictamente la protección contra overselling.
    """

    def __init__(self, default_safety_buffer: int = 1):
        if default_safety_buffer < 0:
            raise ValueError("default_safety_buffer cannot be negative")
        self.default_safety_buffer = default_safety_buffer

    @staticmethod
    def calculate_stock_levels(
        supplier_stock: int = 0,
        owned_stock: int = 0,
        reserved_stock: int = 0,
        safety_buffer: int = 1,
        in_transit_stock: int = 0,
        listed_stock: Optional[int] = None,
    ) -> StockLevel:
        """
        Construye el StockLevel determinista (Source of Truth).
        """
        return StockLevel(
            supplier_stock=supplier_stock,
            owned_stock=owned_stock,
            reserved_stock=reserved_stock,
            safety_buffer=safety_buffer,
            in_transit_stock=in_transit_stock,
            listed_stock=listed_stock,
        )

    @staticmethod
    def evaluate_inventory_decision(
        listing_id: str,
        channel: SalesChannel,
        stock_levels: Optional[StockLevel] = None,
        stock_level: Optional[StockLevel] = None,
        current_quantity: int = 0,
        current_stock: Optional[int] = None,
        proposed_quantity: Optional[int] = None,
        proposed_stock: Optional[int] = None,
        product_id: Optional[str] = None,
        reason: InventoryChangeReason = InventoryChangeReason.SUPPLIER_SYNC,
        rationale: str = "",
        evidence: Optional[Mapping[str, Any]] = None,
        confidence: Confidence = Confidence.HIGH,
        risk_level: RiskLevel = RiskLevel.LOW,
        decision_id: Optional[str] = None,
    ) -> InventoryDecision:
        """
        Evalúa y construye una InventoryDecision inmutable garantizando no exceder available_to_sell.
        """
        target_stock_levels = stock_levels or stock_level
        if target_stock_levels is None:
            raise ValueError("stock_levels or stock_level must be provided")

        cur_qty = current_quantity if current_stock is None else current_stock
        prop_qty = proposed_quantity if proposed_quantity is not None else proposed_stock

        available = target_stock_levels.available_to_sell
        final_proposed_stock = prop_qty if prop_qty is not None else available

        req_approval = False
        # Si la variación de stock es muy grande o el riesgo es elevado, marcar requerimiento de aprobación
        stock_delta = final_proposed_stock - cur_qty
        if cur_qty > 0 and abs(stock_delta) >= max(10, cur_qty * 2):
            req_approval = True
        if risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            req_approval = True

        evidence_dict = dict(evidence or {})
        evidence_dict["total_backed_stock"] = target_stock_levels.total_backed_stock
        evidence_dict["safety_buffer"] = target_stock_levels.safety_buffer
        evidence_dict["reserved_stock"] = target_stock_levels.reserved_stock
        evidence_dict["available_to_sell"] = target_stock_levels.available_to_sell

        dec_id = decision_id or f"inv_dec_{uuid.uuid4().hex[:12]}"

        return InventoryDecision(
            decision_id=dec_id,
            listing_id=listing_id,
            channel=channel,
            current_stock=cur_qty,
            proposed_stock=final_proposed_stock,
            stock_levels=target_stock_levels,
            product_id=product_id,
            reason=reason,
            rationale=rationale or f"Inventory decision syncing available stock {final_proposed_stock} (reason: {reason.value})",
            evidence=evidence_dict,
            confidence=confidence,
            risk_level=risk_level,
            constraints={"max_allowed_stock": available},
            requires_approval=req_approval,
            timestamp=datetime.now(timezone.utc),
        )

    @staticmethod
    def formulate_inventory_action(
        decision: InventoryDecision,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> InventoryAction:
        """
        Formula una InventoryAction ejecutable a partir de una InventoryDecision.
        """
        action_id = f"inv_act_{uuid.uuid4().hex[:12]}"
        req_id = request_id or f"inv_req_{uuid.uuid4().hex[:12]}"
        corr_id = correlation_id or f"corr_{uuid.uuid4().hex[:12]}"
        idemp_key = idempotency_key or f"idemp_{decision.listing_id}_{decision.proposed_stock}_{decision.decision_id}"

        return InventoryAction(
            action_id=action_id,
            decision_id=decision.decision_id,
            listing_id=decision.listing_id,
            channel=decision.channel,
            proposed_stock=decision.proposed_stock,
            current_stock=decision.current_stock,
            old_quantity=decision.current_stock,
            new_quantity=decision.proposed_stock,
            reason=decision.reason,
            request_id=req_id,
            idempotency_key=idemp_key,
            correlation_id=corr_id,
            created_at=datetime.now(timezone.utc),
        )
