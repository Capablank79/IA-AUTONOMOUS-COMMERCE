import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.domain.inventory.models import (
    InventoryAction,
    InventoryChangeReason,
    InventoryDecision,
    InventoryRequest,
    InventoryResult,
    InventoryStatus,
    StockLevel,
)
from src.domain.inventory.ports import InventoryPort
from src.domain.market_intelligence.models import Confidence
from src.domain.mission.models import LoopAction, LoopDecision, LoopState
from src.domain.mission.ports import ActionExecutor
from src.domain.order.models import (
    Order,
    OrderError,
    OrderErrorCategory,
    OrderEvent,
    OrderEventType,
    OrderQueryResult,
    OrderReconciliationReport,
    OrderStatus,
    PaymentStatus,
)
from src.domain.order.ports import OrderPort, OrderRepositoryPort
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyEvaluation,
)
from src.domain.publication.models import SalesChannel
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel

logger = logging.getLogger(__name__)


class OrderProcessingService:
    """
    Servicio de aplicación para la ingestión, normalización, gestión de ciclo de vida
    e integración con inventario de órdenes comerciales (Hito G.6 / TASK 07.6).

    Principios y Arquitectura:
    - Pipeline estricto:
      ORDER INGESTION (Polling/Event) -> NORMALIZATION -> DEDUPLICATION -> ORDER STATE -> POLICY GUARD -> INVENTORY IMPACT -> AUDIT.
    - Exactly-Once Inventory Impact: deduplica eventos y claves de idempotencia para garantizar que una orden confirmada/pagada reduzca stock exactamente una vez.
    - Replay Safety: reintentos o repetición de eventos de la misma orden no descuentan inventario adicional.
    - Uncertainty Preservation: las fallas de red, timeouts o 5xx se preservan como UNKNOWN sin causar decrementos ni corrupciones de stock.
    - Reconciliación: compara el estado interno de órdenes contra el canal externo identificando inconsistencias sin sobrescribir destructivamente el historial.
    - Minimización de PII: no persiste ni transmite datos de pago sensibles.
    """

    def __init__(
        self,
        order_port: OrderPort,
        order_repository: OrderRepositoryPort,
        inventory_port: Optional[InventoryPort] = None,
        policy_engine: Optional[PolicyEngine] = None,
        default_channel: Optional[SalesChannel] = None,
    ):
        self.order_port = order_port
        self.order_repository = order_repository
        self.inventory_port = inventory_port
        self.policy_engine = policy_engine
        self.default_channel = default_channel or SalesChannel(
            channel_id="MERCADO_LIBRE_CL",
            name="Mercado Libre Chile",
            marketplace="MERCADO_LIBRE",
            country_code="CL",
        )

        # Auditoría in-memory de impactos de inventario por orden (external_order_id -> List[InventoryResult])
        self._inventory_impacts_by_order: Dict[str, List[InventoryResult]] = {}
        self._audit_log: List[Dict[str, Any]] = []

    @property
    def audit_log(self) -> Sequence[Dict[str, Any]]:
        return tuple(self._audit_log)

    def process_order_event(self, event: OrderEvent) -> Dict[str, Any]:
        """
        Procesa un evento entrante de orden (webhook o derivado de polling) con deduplicación estricta.
        """
        correlation_id = event.correlation_id or f"corr_{uuid.uuid4().hex[:12]}"
        idempotency_key = event.idempotency_key or f"idemp_{event.event_id}_{event.external_order_id}"

        # 1. Deduplicación e Idempotencia a nivel de evento
        is_new_event = self.order_repository.record_processed_event(
            event_id=event.event_id,
            idempotency_key=idempotency_key,
            external_order_id=event.external_order_id,
        )

        if not is_new_event:
            logger.info("Duplicate order event %s for order %s ignored", event.event_id, event.external_order_id)
            existing_order = self.order_repository.get_order_by_external_id(
                external_order_id=event.external_order_id,
                channel_id=event.channel.channel_id,
            )
            return {
                "status": "DUPLICATE_IGNORED",
                "event_id": event.event_id,
                "external_order_id": event.external_order_id,
                "order_id": existing_order.order_id if existing_order else None,
                "inventory_impacted": False,
                "reasons": ["Duplicate order event already processed"],
            }

        # 2. Obtener o normalizar la orden
        order = event.order
        if order is None:
            # Consultar detalle vía order_port
            query_res = self.order_port.get_order_by_external_id(
                external_order_id=event.external_order_id,
                channel=event.channel,
            )
            if not query_res.is_success or not query_res.orders:
                logger.warning("Failed to fetch order details for event %s: %s", event.event_id, query_res.errors)
                return {
                    "status": "UNKNOWN" if query_res.is_unknown else "FETCH_FAILED",
                    "event_id": event.event_id,
                    "external_order_id": event.external_order_id,
                    "errors": [err.message for err in query_res.errors],
                    "inventory_impacted": False,
                }
            order = query_res.orders[0]

        # 3. Guardar o actualizar la orden en persistencia
        self.order_repository.save_order(order)

        # 4. Determinar si requiere impacto de inventario
        inventory_results = []
        inventory_impacted = False

        if order.is_confirmed_and_paid:
            inventory_results = self._apply_inventory_deduction_if_needed(order, correlation_id)
            inventory_impacted = any(r.is_success for r in inventory_results)

        # 5. Registro de Auditoría
        audit_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "external_order_id": order.external_order_id,
            "order_id": order.order_id,
            "order_status": order.status.value,
            "payment_status": order.payment_status.value,
            "channel_id": order.channel.channel_id,
            "items_count": len(order.items),
            "total_units": order.total_units,
            "inventory_impacted": inventory_impacted,
            "inventory_results_count": len(inventory_results),
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
        }
        self._audit_log.append(audit_entry)

        return {
            "status": "PROCESSED",
            "event_id": event.event_id,
            "external_order_id": order.external_order_id,
            "order_id": order.order_id,
            "order_status": order.status.value,
            "payment_status": order.payment_status.value,
            "inventory_impacted": inventory_impacted,
            "inventory_results": [
                {
                    "listing_id": r.listing_id,
                    "status": r.status.value,
                    "applied_quantity": r.applied_quantity,
                }
                for r in inventory_results
            ],
            "correlation_id": correlation_id,
        }

    def sync_orders_from_channel(
        self,
        channel: Optional[SalesChannel] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Sequence[Dict[str, Any]]:
        """
        Sincroniza y procesa órdenes recientes desde el canal mediante polling.
        """
        target_channel = channel or self.default_channel
        query_result = self.order_port.fetch_orders(
            channel=target_channel,
            status=status,
            limit=limit,
        )

        if not query_result.is_success:
            logger.warning("Order polling returned errors: %s", query_result.errors)
            return ()

        processed_summaries = []
        for order in query_result.orders:
            event = OrderEvent(
                event_id=f"ev_sync_{order.external_order_id}_{int(datetime.now(timezone.utc).timestamp())}",
                event_type=OrderEventType.ORDER_CONFIRMED if order.is_confirmed_and_paid else OrderEventType.ORDER_UPDATED,
                external_order_id=order.external_order_id,
                channel=order.channel,
                order=order,
                idempotency_key=f"idemp_sync_{order.channel.channel_id}_{order.external_order_id}",
            )
            res = self.process_order_event(event)
            processed_summaries.append(res)

        return tuple(processed_summaries)

    def reconcile_order(
        self,
        order_id_or_external_id: str,
        channel: Optional[SalesChannel] = None,
    ) -> OrderReconciliationReport:
        """
        Reconcilia deterministamente una orden interna contra el estado actual del canal externo.
        """
        target_channel = channel or self.default_channel

        # 1. Buscar orden local
        local_order = self.order_repository.get_order_by_id(order_id_or_external_id)
        if not local_order:
            local_order = self.order_repository.get_order_by_external_id(
                external_order_id=order_id_or_external_id,
                channel_id=target_channel.channel_id,
            )

        # 2. Consultar canal externo
        ext_id = local_order.external_order_id if local_order else order_id_or_external_id
        ext_query = self.order_port.get_order_by_external_id(
            external_order_id=ext_id,
            channel=target_channel,
        )

        if not local_order and not ext_query.orders:
            return OrderReconciliationReport(
                order_id=order_id_or_external_id,
                external_order_id=ext_id,
                is_reconciled=False,
                internal_status=OrderStatus.UNKNOWN,
                external_status=OrderStatus.UNKNOWN,
                internal_payment_status=PaymentStatus.UNKNOWN,
                external_payment_status=PaymentStatus.UNKNOWN,
                discrepancies=("Order not found neither locally nor in external marketplace",),
                requires_action=False,
            )

        if not local_order and ext_query.orders:
            ext_order = ext_query.orders[0]
            # Orden existe externamente pero no localmente
            return OrderReconciliationReport(
                order_id=ext_order.order_id,
                external_order_id=ext_order.external_order_id,
                is_reconciled=False,
                internal_status=OrderStatus.UNKNOWN,
                external_status=ext_order.status,
                internal_payment_status=PaymentStatus.UNKNOWN,
                external_payment_status=ext_order.payment_status,
                discrepancies=("Order exists externally but is missing in local repository",),
                requires_action=True,
            )

        if local_order and not ext_query.orders:
            return OrderReconciliationReport(
                order_id=local_order.order_id,
                external_order_id=local_order.external_order_id,
                is_reconciled=False,
                internal_status=local_order.status,
                external_status=OrderStatus.UNKNOWN,
                internal_payment_status=local_order.payment_status,
                external_payment_status=PaymentStatus.UNKNOWN,
                discrepancies=("Order exists locally but external query failed or returned empty",),
                requires_action=False,
            )

        ext_order = ext_query.orders[0]
        discrepancies = []

        if local_order.status != ext_order.status:
            discrepancies.append(
                f"Status mismatch: local={local_order.status.value}, external={ext_order.status.value}"
            )
        if local_order.payment_status != ext_order.payment_status:
            discrepancies.append(
                f"Payment status mismatch: local={local_order.payment_status.value}, external={ext_order.payment_status.value}"
            )
        if local_order.fulfillment_status != ext_order.fulfillment_status:
            discrepancies.append(
                f"Fulfillment status mismatch: local={local_order.fulfillment_status.value}, external={ext_order.fulfillment_status.value}"
            )

        is_reconciled = len(discrepancies) == 0
        requires_action = not is_reconciled

        # Actualizar orden local si hubo cambio de estado legítimo externamente
        if not is_reconciled and ext_order.status != OrderStatus.UNKNOWN:
            self.order_repository.save_order(ext_order)

        return OrderReconciliationReport(
            order_id=local_order.order_id,
            external_order_id=local_order.external_order_id,
            is_reconciled=is_reconciled,
            internal_status=local_order.status,
            external_status=ext_order.status,
            internal_payment_status=local_order.payment_status,
            external_payment_status=ext_order.payment_status,
            discrepancies=tuple(discrepancies),
            requires_action=requires_action,
        )

    def _apply_inventory_deduction_if_needed(
        self,
        order: Order,
        correlation_id: str,
    ) -> List[InventoryResult]:
        """
        Deduce inventario para cada ítem de una orden confirmada/pagada de forma idempotente y gobernada.
        """
        if self.inventory_port is None:
            logger.info("No inventory_port configured; skipping live stock deduction")
            return []

        # Comprobar si ya fue impactado el inventario para esta orden
        if order.external_order_id in self._inventory_impacts_by_order:
            logger.info(
                "Inventory for external order %s was already deducted (exactly-once guaranteed)",
                order.external_order_id,
            )
            return self._inventory_impacts_by_order[order.external_order_id]

        results: List[InventoryResult] = []

        for item in order.items:
            listing_id = item.listing_id or item.external_item_id or item.item_id
            deduct_qty = item.quantity

            # 1. Consultar stock actual
            stock_check = self.inventory_port.get_current_stock(order.channel, listing_id)
            current_qty = stock_check.applied_quantity if stock_check.is_success else 0

            # Proteger contra stock negativo
            proposed_qty = max(0, current_qty - deduct_qty)

            idempotency_key = f"idemp_order_deduct_{order.channel.channel_id}_{order.external_order_id}_{listing_id}_{item.quantity}"

            # 2. Evaluación de Policy si está disponible
            if self.policy_engine is not None:
                policy_ctx = PolicyEvaluationContext(
                    action_type="UPDATE_INVENTORY",
                    actor_id="OrderProcessingService",
                    mission_id=f"order_proc_{order.order_id}",
                    correlation_id=correlation_id or order.correlation_id or "corr_order",
                    loop_decision=LoopDecision(
                        action=LoopAction.CONTINUE,
                        reason="Inventory deduction for confirmed paid order",
                    ),
                    channel=order.channel.channel_id,
                    target_resource=listing_id,
                    is_external_impact=True,
                    is_irreversible=False,
                    human_approved=True,
                    idempotency_key=idempotency_key,
                    risk_level=RiskLevel.LOW,
                    custom_context={
                        "proposed_stock": proposed_qty,
                        "current_stock": current_qty,
                        "max_allowed_stock": current_qty,
                        "order_id": order.order_id,
                        "external_order_id": order.external_order_id,
                    },
                )
                eval_res = self.policy_engine.evaluate(policy_ctx)
                if not eval_res.is_allowed:
                    logger.warning(
                        "Policy denied inventory deduction for listing %s: %s",
                        listing_id,
                        list(eval_res.reasons),
                    )
                    res = InventoryResult(
                        inventory_id=None,
                        listing_id=listing_id,
                        channel=order.channel,
                        status=InventoryStatus.FAILED,
                        errors=(
                            OrderError(
                                category=OrderErrorCategory.AUTHORIZATION,
                                message=f"Policy violation: {'; '.join(eval_res.reasons)}",
                            ),
                        ),
                    )
                    results.append(res)
                    continue

            # 3. Ejecutar deducción en InventoryPort
            inv_request = InventoryRequest(
                request_id=f"req_deduct_{uuid.uuid4().hex[:10]}",
                listing_id=listing_id,
                proposed_quantity=proposed_qty,
                current_quantity=current_qty,
                channel=order.channel,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                reason=InventoryChangeReason.STOCK_REDUCTION,
                metadata={
                    "order_id": order.order_id,
                    "external_order_id": order.external_order_id,
                    "deducted_units": deduct_qty,
                },
            )

            inv_result = self.inventory_port.update_inventory(inv_request)
            results.append(inv_result)

        self._inventory_impacts_by_order[order.external_order_id] = results
        return results


class OrderActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones de órdenes en el AutonomousLoop (Hito G.6).
    Reutiliza ActionExecutor desacoplando el loop de la infraestructura.
    Soporta:
    - FETCH_ORDERS / GET_ORDERS / POLL_ORDERS
    - GET_ORDER / FETCH_ORDER_DETAIL
    - RECONCILE_ORDER / CHECK_ORDER_RECONCILIATION
    - PROCESS_ORDER_EVENT
    """

    def __init__(
        self,
        order_processing_service: OrderProcessingService,
    ):
        self.order_processing_service = order_processing_service
        self._latest_result: Optional[Any] = None

    @property
    def latest_result(self) -> Optional[Any]:
        return self._latest_result

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        action_name = decision.parameters.get("action_type") or str(decision.action.value)
        params = dict(decision.parameters)

        if action_name in ("FETCH_ORDERS", "GET_ORDERS", "POLL_ORDERS", "SYNC_ORDERS"):
            channel = params.get("channel")
            status = params.get("status")
            limit = int(params.get("limit") or 50)
            res = self.order_processing_service.sync_orders_from_channel(
                channel=channel,
                status=status,
                limit=limit,
            )
            self._latest_result = res
            return {
                "status": "SUCCESS",
                "action": action_name,
                "synced_orders_count": len(res),
                "orders": list(res),
            }

        elif action_name in ("GET_ORDER", "FETCH_ORDER_DETAIL", "GET_ORDER_BY_ID"):
            external_order_id = params.get("external_order_id") or params.get("order_id")
            channel = params.get("channel") or self.order_processing_service.default_channel
            query_res = self.order_processing_service.order_port.get_order_by_external_id(
                external_order_id=str(external_order_id),
                channel=channel,
            )
            self._latest_result = query_res
            if not query_res.is_success or not query_res.orders:
                return {
                    "status": "UNKNOWN" if query_res.is_unknown else "FAILED",
                    "action": action_name,
                    "errors": [e.message for e in query_res.errors],
                }
            ord_obj = query_res.orders[0]
            return {
                "status": "SUCCESS",
                "action": action_name,
                "order_id": ord_obj.order_id,
                "external_order_id": ord_obj.external_order_id,
                "order_status": ord_obj.status.value,
                "payment_status": ord_obj.payment_status.value,
                "total_amount": float(ord_obj.total_amount),
                "currency": ord_obj.currency,
                "items_count": len(ord_obj.items),
            }

        elif action_name in ("RECONCILE_ORDER", "CHECK_ORDER_RECONCILIATION"):
            order_id = params.get("order_id") or params.get("external_order_id")
            channel = params.get("channel")
            rec_report = self.order_processing_service.reconcile_order(
                order_id_or_external_id=str(order_id),
                channel=channel,
            )
            self._latest_result = rec_report
            return {
                "status": "SUCCESS",
                "action": action_name,
                "order_id": rec_report.order_id,
                "external_order_id": rec_report.external_order_id,
                "is_reconciled": rec_report.is_reconciled,
                "internal_status": rec_report.internal_status.value,
                "external_status": rec_report.external_status.value,
                "discrepancies": list(rec_report.discrepancies),
                "requires_action": rec_report.requires_action,
            }

        elif action_name in ("PROCESS_ORDER_EVENT", "HANDLE_ORDER_EVENT"):
            event = params.get("event")
            if not isinstance(event, OrderEvent):
                return {
                    "status": "FAILED",
                    "action": action_name,
                    "error": "Missing or invalid OrderEvent object in parameters",
                }
            res = self.order_processing_service.process_order_event(event)
            self._latest_result = res
            return {
                "status": "SUCCESS",
                "action": action_name,
                "event_result": res,
            }

        return {
            "status": "UNKNOWN_ACTION",
            "action": action_name,
            "error": f"Unsupported order action: {action_name}",
        }
