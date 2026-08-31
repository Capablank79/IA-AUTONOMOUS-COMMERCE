import uuid
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, List, Sequence, Tuple, Mapping
from types import MappingProxyType

from src.domain.mission.models import (
    LoopDecision,
    LoopState,
    LoopAction,
)
from src.domain.mission.ports import ActionExecutor
from src.domain.publication.models import SalesChannel
from src.domain.pricing.models import (
    PricingAction,
    PricingDecision,
    PricingRequest,
    PricingResult,
    PricingStatus,
    PricingError,
    PricingErrorCategory,
)
from src.domain.pricing.ports import (
    PricingPort,
    PricingRepository,
)

logger = logging.getLogger(__name__)


class PricingActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones para la gestión de precios en AutonomousLoop (Hito G.4).
    Integración limpia y desacoplada mediante inversión de dependencias:
    PricingDecision -> PricingAction -> Policy -> PricingActionExecutor -> PricingPort -> Marketplace Adapter

    Preserva estrictamente:
    - Inversión de dependencias: sólo interactúa con PricingPort y modelos de dominio.
    - Estado UNKNOWN: ante timeouts o fallos 5xx, preserva UNKNOWN sin degradar a FAILED.
    - Verificación y Reconciliación: soporta VERIFY_PRICE / GET_CURRENT_PRICE para resolver ambigüedad.
    - Idempotencia: preserva idempotency_key, request_id y detecta reintentos.
    - Correlación: preserva correlation_id a través de todo el flujo.
    - Auditoría: registra y actualiza repositorios/trazas si están disponibles.
    - Cero bypass de Policy: diseñado para ser ejecutado tras o dentro de PolicyGuardedActionExecutor.
    """

    def __init__(
        self,
        pricing_port: PricingPort,
        repository: Optional[PricingRepository] = None,
        default_channel: Optional[SalesChannel] = None,
    ):
        if pricing_port is None:
            raise ValueError("pricing_port cannot be None")
        self.pricing_port = pricing_port
        self.repository = repository
        self.default_channel = default_channel

        # Auditoría y tracking in-memory por ejecutor
        self._price_results: Dict[str, PricingResult] = {}
        self._latest_result: Optional[PricingResult] = None
        self._external_calls_count: int = 0

    @property
    def external_calls_count(self) -> int:
        return self._external_calls_count

    @property
    def latest_result(self) -> Optional[PricingResult]:
        return self._latest_result

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        """
        Ejecuta una decisión del AutonomousLoop para acciones de pricing.
        Soporta:
        - UPDATE_PRICE / SET_PRICE / PRICING_UPDATE / CHANGE_PRICE: Actualización de precio en el canal.
        - VERIFY_PRICE / GET_CURRENT_PRICE / CHECK_PRICE: Consulta del precio actual para reconciliación.
        """
        action_name = decision.parameters.get("action_type") or str(decision.action.value)
        params = dict(decision.parameters)

        if action_name in ("UPDATE_PRICE", "SET_PRICE", "PRICING_UPDATE", "CHANGE_PRICE", "CONTINUE"):
            return self._execute_update_price(decision, state, params)
        elif action_name in ("VERIFY_PRICE", "GET_CURRENT_PRICE", "CHECK_PRICE", "VERIFY_STATUS"):
            return self._execute_verify_price(decision, state, params)
        else:
            if "pricing_action" in params or "pricing_decision" in params:
                return self._execute_update_price(decision, state, params)
            return {
                "action_executed": action_name,
                "status": "UNSUPPORTED_ACTION",
                "error": f"Unsupported pricing action: {action_name}",
            }

    def _execute_update_price(
        self,
        decision: LoopDecision,
        state: LoopState,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        pricing_action: Optional[PricingAction] = params.get("pricing_action")
        pricing_decision: Optional[PricingDecision] = params.get("pricing_decision")

        # 1. Resolver parámetros de precio y listing
        listing_id: Optional[str] = None
        proposed_price: Optional[Decimal] = None
        current_price: Optional[Decimal] = None
        currency: str = "CLP"
        channel: Optional[SalesChannel] = params.get("channel") or self.default_channel

        if pricing_action is not None:
            listing_id = pricing_action.listing_id
            proposed_price = pricing_action.proposed_price
            current_price = pricing_action.current_price
            currency = pricing_action.currency
            channel = pricing_action.channel or channel
        elif pricing_decision is not None:
            listing_id = pricing_decision.listing_id
            proposed_price = pricing_decision.proposed_price
            current_price = pricing_decision.current_price
            currency = pricing_decision.currency
            channel = pricing_decision.channel or channel

        if listing_id is None and "listing_id" in params:
            listing_id = str(params["listing_id"])
        if proposed_price is None and "proposed_price" in params:
            proposed_price = Decimal(str(params["proposed_price"]))
        if current_price is None and "current_price" in params:
            current_price = Decimal(str(params["current_price"]))
        if "currency" in params:
            currency = str(params["currency"])

        # Validar requerimientos mínimos
        if listing_id is None or proposed_price is None:
            return {
                "action_executed": "UPDATE_PRICE",
                "status": PricingStatus.FAILED.value,
                "error": "Missing required 'listing_id' or 'proposed_price' for pricing update",
                "is_unknown": False,
                "is_success": False,
            }

        if channel is None:
            return {
                "action_executed": "UPDATE_PRICE",
                "status": PricingStatus.FAILED.value,
                "error": "Missing required 'channel' (SalesChannel)",
                "is_unknown": False,
                "is_success": False,
            }

        # Preservación de idempotency_key, correlation_id y request_id
        action_id = pricing_action.action_id if pricing_action else f"act_{uuid.uuid4().hex[:8]}"
        idempotency_key = (
            (pricing_action.idempotency_key if pricing_action else None)
            or params.get("idempotency_key")
            or f"idemp_{listing_id}_{proposed_price}"
        )
        correlation_id = (
            (pricing_action.correlation_id if pricing_action else None)
            or params.get("correlation_id")
            or state.mission_id
            or str(uuid.uuid4())
        )
        request_id = params.get("request_id") or f"req_{uuid.uuid4().hex[:12]}"

        request = PricingRequest(
            request_id=request_id,
            listing_id=listing_id,
            proposed_price=proposed_price,
            current_price=current_price,
            channel=channel,
            currency=currency,
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
        if self.repository and pricing_decision is not None:
            try:
                self.repository.save_decision(pricing_decision)
            except Exception:
                pass

        self._external_calls_count += 1
        result = self.pricing_port.update_price(request)
        self._latest_result = result
        self._price_results[listing_id] = result

        # Si hay repositorio, persistir resultado
        if self.repository:
            try:
                self.repository.save_result(result)
            except Exception:
                pass

        return {
            "action_executed": "UPDATE_PRICE",
            "listing_id": listing_id,
            "channel_id": channel.channel_id,
            "status": result.status.value,
            "is_success": result.is_success,
            "is_unknown": result.is_unknown,
            "applied_price": float(result.applied_price) if result.applied_price is not None else None,
            "previous_price": float(result.previous_price) if result.previous_price is not None else None,
            "currency": result.currency,
            "request_id": result.request_id,
            "idempotency_key": result.idempotency_key,
            "correlation_id": result.correlation_id,
            "errors": [e.message for e in result.errors],
        }

    def _execute_verify_price(
        self,
        decision: LoopDecision,
        state: LoopState,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        listing_id = params.get("listing_id")
        channel = params.get("channel") or self.default_channel

        if not listing_id or not channel:
            return {
                "action_executed": "VERIFY_PRICE",
                "status": PricingStatus.FAILED.value,
                "error": "Missing listing_id or channel for price verification",
                "is_unknown": False,
                "is_success": False,
            }

        self._external_calls_count += 1
        result = self.pricing_port.get_current_price(channel=channel, listing_id=listing_id)
        self._latest_result = result

        return {
            "action_executed": "VERIFY_PRICE",
            "listing_id": listing_id,
            "channel_id": channel.channel_id,
            "status": result.status.value,
            "is_success": result.is_success,
            "is_unknown": result.is_unknown,
            "current_price": float(result.applied_price) if result.applied_price is not None else None,
            "currency": result.currency,
            "errors": [e.message for e in result.errors],
        }
