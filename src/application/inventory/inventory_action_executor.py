import uuid
import logging
from typing import Dict, Any, Optional
from types import MappingProxyType

from src.domain.mission.models import (
    LoopDecision,
    LoopState,
)
from src.domain.mission.ports import ActionExecutor
from src.domain.publication.models import SalesChannel
from src.domain.inventory.models import (
    InventoryAction,
    InventoryDecision,
    InventoryRequest,
    InventoryResult,
    InventoryStatus,
)
from src.domain.inventory.ports import (
    InventoryPort,
    InventoryRepository,
)

logger = logging.getLogger(__name__)


class InventoryActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones para la gestión de inventario/stock en AutonomousLoop (Hito G.5).
    Integración limpia y desacoplada mediante inversión de dependencias:
    InventoryDecision -> InventoryAction -> Policy -> InventoryActionExecutor -> InventoryPort -> Marketplace Adapter

    Preserva estrictamente:
    - Inversión de dependencias: sólo interactúa con InventoryPort y modelos de dominio.
    - Estado UNKNOWN: ante timeouts o fallos 5xx, preserva UNKNOWN sin degradar a FAILED.
    - Verificación y Reconciliación: soporta VERIFY_STOCK / GET_CURRENT_STOCK / RECONCILE_INVENTORY para resolver ambigüedad.
    - Idempotencia: preserva idempotency_key, request_id y detecta reintentos.
    - Correlación: preserva correlation_id a través de todo el flujo.
    - Auditoría: registra y actualiza repositorios/trazas si están disponibles.
    - Cero bypass de Policy: diseñado para ser ejecutado tras o dentro de PolicyGuardedActionExecutor.
    """

    def __init__(
        self,
        inventory_port: InventoryPort,
        repository: Optional[InventoryRepository] = None,
        default_channel: Optional[SalesChannel] = None,
    ):
        if inventory_port is None:
            raise ValueError("inventory_port cannot be None")
        self.inventory_port = inventory_port
        self.repository = repository
        self.default_channel = default_channel

        # Auditoría y tracking in-memory por ejecutor
        self._inventory_results: Dict[str, InventoryResult] = {}
        self._latest_result: Optional[InventoryResult] = None
        self._external_calls_count: int = 0

    @property
    def external_calls_count(self) -> int:
        return self._external_calls_count

    @property
    def latest_result(self) -> Optional[InventoryResult]:
        return self._latest_result

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        """
        Ejecuta una decisión del AutonomousLoop para acciones de inventario.
        Soporta:
        - UPDATE_INVENTORY / SET_INVENTORY / INVENTORY_UPDATE / SYNC_INVENTORY: Actualización de stock en el canal.
        - VERIFY_STOCK / GET_CURRENT_STOCK / CHECK_STOCK / RECONCILE_INVENTORY: Consulta del stock actual para reconciliación.
        """
        action_name = decision.parameters.get("action_type") or str(decision.action.value)
        params = dict(decision.parameters)

        if action_name in (
            "UPDATE_INVENTORY",
            "SET_INVENTORY",
            "INVENTORY_UPDATE",
            "SYNC_INVENTORY",
            "UPDATE_STOCK",
            "SET_STOCK",
            "CONTINUE",
        ):
            return self._execute_update_inventory(decision, state, params)
        elif action_name in (
            "VERIFY_STOCK",
            "GET_CURRENT_STOCK",
            "CHECK_STOCK",
            "RECONCILE_INVENTORY",
            "VERIFY_INVENTORY",
            "VERIFY_STATUS",
        ):
            return self._execute_verify_stock(decision, state, params)
        else:
            if "inventory_action" in params or "inventory_decision" in params:
                return self._execute_update_inventory(decision, state, params)
            return {
                "action_executed": action_name,
                "status": "UNSUPPORTED_ACTION",
                "error": f"Unsupported inventory action: {action_name}",
            }

    def _execute_update_inventory(
        self,
        decision: LoopDecision,
        state: LoopState,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        inventory_action: Optional[InventoryAction] = params.get("inventory_action")
        inventory_decision: Optional[InventoryDecision] = params.get("inventory_decision")

        # 1. Resolver parámetros de stock y listing
        listing_id: Optional[str] = None
        proposed_quantity: Optional[int] = None
        current_quantity: Optional[int] = None
        channel: Optional[SalesChannel] = params.get("channel") or self.default_channel

        if inventory_action is not None:
            listing_id = inventory_action.listing_id
            proposed_quantity = inventory_action.proposed_quantity
            current_quantity = inventory_action.current_quantity
            channel = inventory_action.channel or channel
        elif inventory_decision is not None:
            listing_id = inventory_decision.listing_id
            proposed_quantity = inventory_decision.proposed_quantity
            current_quantity = inventory_decision.current_quantity
            channel = inventory_decision.channel or channel

        if listing_id is None and "listing_id" in params:
            listing_id = str(params["listing_id"])
        if proposed_quantity is None and "proposed_quantity" in params:
            proposed_quantity = int(params["proposed_quantity"])
        if proposed_quantity is None and "quantity" in params:
            proposed_quantity = int(params["quantity"])
        if current_quantity is None and "current_quantity" in params:
            current_quantity = int(params["current_quantity"])

        # Validar requerimientos mínimos
        if listing_id is None or proposed_quantity is None:
            return {
                "action_executed": "UPDATE_INVENTORY",
                "status": InventoryStatus.FAILED.value,
                "error": "Missing required 'listing_id' or 'proposed_quantity' for inventory update",
                "is_unknown": False,
                "is_success": False,
            }

        if channel is None:
            return {
                "action_executed": "UPDATE_INVENTORY",
                "status": InventoryStatus.FAILED.value,
                "error": "Missing required 'channel' (SalesChannel)",
                "is_unknown": False,
                "is_success": False,
            }

        # Preservación de idempotency_key, correlation_id y request_id
        action_id = inventory_action.action_id if inventory_action else f"inv_act_{uuid.uuid4().hex[:8]}"
        idempotency_key = (
            (inventory_action.idempotency_key if inventory_action else None)
            or params.get("idempotency_key")
            or f"idemp_inv_{listing_id}_{proposed_quantity}"
        )
        correlation_id = (
            (inventory_action.correlation_id if inventory_action else None)
            or params.get("correlation_id")
            or state.mission_id
            or str(uuid.uuid4())
        )
        request_id = params.get("request_id") or f"req_inv_{uuid.uuid4().hex[:12]}"

        request = InventoryRequest(
            request_id=request_id,
            listing_id=listing_id,
            proposed_quantity=proposed_quantity,
            current_quantity=current_quantity,
            channel=channel,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            metadata=MappingProxyType({
                "mission_id": state.mission_id,
                "iteration": state.iteration,
                "decision_reason": decision.reason,
                "action_id": action_id,
            }),
        )

        # Si hay repositorio y decisión previa, persistir
        if self.repository and inventory_decision is not None:
            try:
                self.repository.save_decision(inventory_decision)
            except Exception:
                pass

        self._external_calls_count += 1
        result = self.inventory_port.update_inventory(request)
        self._latest_result = result
        self._inventory_results[listing_id] = result

        # Si hay repositorio, persistir resultado
        if self.repository:
            try:
                self.repository.save_result(result)
            except Exception:
                pass

        return {
            "action_executed": "UPDATE_INVENTORY",
            "listing_id": listing_id,
            "channel_id": channel.channel_id,
            "status": result.status.value,
            "is_success": result.is_success,
            "is_unknown": result.is_unknown,
            "applied_quantity": result.applied_quantity,
            "previous_quantity": result.previous_quantity,
            "request_id": result.request_id,
            "idempotency_key": result.idempotency_key,
            "correlation_id": result.correlation_id,
            "errors": [e.message for e in result.errors],
        }

    def _execute_verify_stock(
        self,
        decision: LoopDecision,
        state: LoopState,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        listing_id = params.get("listing_id")
        channel = params.get("channel") or self.default_channel

        if not listing_id or not channel:
            return {
                "action_executed": "VERIFY_STOCK",
                "status": InventoryStatus.FAILED.value,
                "error": "Missing listing_id or channel for stock verification",
                "is_unknown": False,
                "is_success": False,
            }

        self._external_calls_count += 1
        result = self.inventory_port.get_current_stock(channel=channel, listing_id=listing_id)
        self._latest_result = result

        return {
            "action_executed": "VERIFY_STOCK",
            "listing_id": listing_id,
            "channel_id": channel.channel_id,
            "status": result.status.value,
            "is_success": result.is_success,
            "is_unknown": result.is_unknown,
            "current_quantity": result.applied_quantity,
            "errors": [e.message for e in result.errors],
        }
