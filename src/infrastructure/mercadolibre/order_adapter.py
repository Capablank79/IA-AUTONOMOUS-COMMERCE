import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlencode

from src.application.oauth.connection_service import OAuthConnectionService
from src.domain.market_intelligence.models import Confidence
from src.domain.order.models import (
    BuyerReference,
    FulfillmentStatus,
    Order,
    OrderError,
    OrderErrorCategory,
    OrderItem,
    OrderQueryResult,
    OrderStatus,
    PaymentStatus,
    ShipmentReference,
)
from src.domain.order.ports import OrderPort
from src.domain.publication.models import SalesChannel
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)

logger = logging.getLogger(__name__)


class MercadoLibreOrderAdapter(OrderPort):
    """
    Adaptador de infraestructura de Mercado Libre para la integración de órdenes (Hito G.6).
    
    Principios Arquitectónicos:
    - Desacoplamiento total del dominio respecto al formato JSON de Mercado Libre.
    - Normalización determinista: mapea DTO externo -> entidad Order inmutable.
    - Manejo taxonómico de errores: VALIDATION, AUTHORIZATION, NOT_FOUND, CONFLICT, RATE_LIMIT, TIMEOUT, UNKNOWN.
    - Preservación estricta de incertidumbre (UNKNOWN) ante fallos 5xx, timeouts o conectividad.
    - Minimización de PII: no almacena datos de pago sensibles ni identificadores innecesarios.
    - Reutilización de clientes autenticados vía OAuthConnectionService / MercadoLibreApiClient.
    """

    def __init__(
        self,
        oauth_service: Optional[OAuthConnectionService] = None,
        api_client: Optional[MercadoLibreApiClient] = None,
        default_user_id: Optional[str] = None,
        provider_name: str = "mercadolibre",
    ):
        self.oauth_service = oauth_service
        self.api_client = api_client
        self.default_user_id = default_user_id
        self.provider_name = provider_name

    def _get_api_client(self, channel: SalesChannel) -> MercadoLibreApiClient:
        """
        Obtiene el cliente autenticado de Mercado Libre.
        """
        if self.api_client is not None:
            return self.api_client

        if self.oauth_service is None:
            raise ValueError(
                "Neither api_client nor oauth_service was provided to MercadoLibreOrderAdapter"
            )

        user_id = channel.metadata.get("user_id") or self.default_user_id
        if not user_id:
            raise ValueError(
                "Cannot resolve user_id for Mercado Libre OAuth: missing in channel metadata and default_user_id"
            )

        connection = self.oauth_service.get_valid_connection(
            provider=self.provider_name,
            user_id=str(user_id),
        )
        return MercadoLibreApiClient(access_token=connection.access_token)

    @staticmethod
    def map_external_order_status(status_str: Optional[str]) -> OrderStatus:
        """
        Mapea el status de Mercado Libre a OrderStatus de dominio.
        Mercado Libre status típicos: confirmed, payment_required, payment_in_process,
        partially_paid, paid, cancelled, invalid.
        """
        if not status_str:
            return OrderStatus.UNKNOWN

        normalized = status_str.strip().lower()
        if normalized == "paid":
            return OrderStatus.PAID
        elif normalized == "confirmed":
            return OrderStatus.CONFIRMED
        elif normalized in ("payment_required", "payment_in_process", "partially_paid"):
            return OrderStatus.PENDING
        elif normalized == "cancelled":
            return OrderStatus.CANCELLED
        elif normalized in ("closed", "delivered"):
            return OrderStatus.CLOSED
        elif normalized == "invalid":
            return OrderStatus.INVALID
        return OrderStatus.UNKNOWN

    @staticmethod
    def map_external_payment_status(raw_order: Mapping[str, Any]) -> PaymentStatus:
        """
        Mapea el estado del pago desde los payments internos de la orden de Mercado Libre.
        """
        payments = raw_order.get("payments", [])
        if not payments:
            order_status = (raw_order.get("status") or "").lower()
            if order_status == "paid":
                return PaymentStatus.APPROVED
            elif order_status in ("payment_required", "payment_in_process"):
                return PaymentStatus.PENDING
            elif order_status == "cancelled":
                return PaymentStatus.CANCELLED
            return PaymentStatus.UNKNOWN

        # Evaluar los estados de los pagos individuales
        approved_count = 0
        rejected_count = 0
        pending_count = 0

        for p in payments:
            p_status = (p.get("status") or "").lower()
            if p_status == "approved":
                approved_count += 1
            elif p_status in ("rejected", "cancelled"):
                rejected_count += 1
            elif p_status in ("pending", "in_process", "authorized"):
                pending_count += 1

        if approved_count > 0 and rejected_count == 0 and pending_count == 0:
            return PaymentStatus.APPROVED
        elif pending_count > 0:
            return PaymentStatus.PENDING
        elif rejected_count > 0 and approved_count == 0:
            return PaymentStatus.REJECTED
        return PaymentStatus.UNKNOWN

    @staticmethod
    def map_external_fulfillment_status(raw_order: Mapping[str, Any]) -> FulfillmentStatus:
        """
        Mapea el estado logístico de referencia (G.6) desde el objeto shipping de Mercado Libre.
        """
        shipping = raw_order.get("shipping") or {}
        ship_status = (shipping.get("status") or "").lower()
        substatus = (shipping.get("substatus") or "").lower()

        if ship_status == "delivered":
            return FulfillmentStatus.DELIVERED
        elif ship_status == "shipped":
            return FulfillmentStatus.SHIPPED
        elif ship_status in ("ready_to_ship", "ready_to_print"):
            return FulfillmentStatus.READY_TO_SHIP
        elif ship_status in ("pending", "to_be_agreed"):
            return FulfillmentStatus.PENDING
        elif ship_status == "cancelled":
            return FulfillmentStatus.CANCELLED
        elif not shipping:
            return FulfillmentStatus.NOT_APPLICABLE
        return FulfillmentStatus.UNKNOWN

    def normalize_order(
        self,
        raw_order: Mapping[str, Any],
        channel: SalesChannel,
        correlation_id: str = "",
    ) -> Order:
        """
        Normaliza deterministamente el DTO JSON de Mercado Libre hacia la entidad de dominio inmutable Order.
        Garantiza minimización de datos sensibles y precisión decimal.
        """
        ext_order_id = str(raw_order.get("id") or "")
        if not ext_order_id:
            raise ValueError("Mercado Libre order missing required 'id' field")

        # 1. Status mappings
        order_status = self.map_external_order_status(raw_order.get("status"))
        payment_status = self.map_external_payment_status(raw_order)
        fulfillment_status = self.map_external_fulfillment_status(raw_order)

        # 2. Line Items
        raw_items = raw_order.get("order_items", [])
        if not raw_items:
            raise ValueError(f"Mercado Libre order {ext_order_id} has no order_items")

        items: List[OrderItem] = []
        calculated_total = Decimal("0")

        for idx, item_data in enumerate(raw_items):
            item_info = item_data.get("item") or {}
            item_id = str(item_info.get("id") or f"item_{idx}")
            title = str(item_info.get("title") or "Unknown Product")
            quantity = int(item_data.get("quantity") or 1)
            
            unit_price_val = item_data.get("unit_price")
            if unit_price_val is None:
                unit_price_val = item_data.get("full_unit_price", 0)
            unit_price = Decimal(str(unit_price_val))

            currency_val = str(item_data.get("currency_id") or raw_order.get("currency_id") or "CLP")
            variation_id = str(item_info.get("variation_id")) if item_info.get("variation_id") else None
            seller_sku = str(item_info.get("seller_sku") or item_info.get("seller_custom_field") or "") or None

            order_item = OrderItem(
                item_id=item_id,
                title=title,
                quantity=quantity,
                unit_price=unit_price,
                currency=currency_val,
                external_item_id=item_id,
                sku=seller_sku,
                listing_id=item_id,
                variation_id=variation_id,
            )
            items.append(order_item)
            calculated_total += order_item.total_amount

        # 3. Currency and total
        currency = str(raw_order.get("currency_id") or (items[0].currency if items else "CLP"))
        total_amount_val = raw_order.get("total_amount")
        total_amount = Decimal(str(total_amount_val)) if total_amount_val is not None else calculated_total

        # 4. Buyer Reference (Privacy & PII minimized)
        raw_buyer = raw_order.get("buyer") or {}
        buyer_id = str(raw_buyer.get("id") or "ANONYMOUS_BUYER")
        nickname = str(raw_buyer.get("nickname")) if raw_buyer.get("nickname") else None
        buyer_ref = BuyerReference(
            buyer_id=buyer_id,
            nickname=nickname,
        )

        # 5. Shipment Reference
        raw_shipping = raw_order.get("shipping") or {}
        shipment_ref: Optional[ShipmentReference] = None
        if raw_shipping:
            shipment_ref = ShipmentReference(
                shipment_id=str(raw_shipping.get("id")) if raw_shipping.get("id") else None,
                shipping_mode=str(raw_shipping.get("shipping_mode")) if raw_shipping.get("shipping_mode") else None,
                logistic_type=str(raw_shipping.get("logistic_type")) if raw_shipping.get("logistic_type") else None,
                status=fulfillment_status,
                tracking_number=str(raw_shipping.get("tracking_number")) if raw_shipping.get("tracking_number") else None,
            )

        # 6. Dates
        date_created_str = raw_order.get("date_created")
        if date_created_str:
            try:
                # ISO format parse
                created_at = datetime.fromisoformat(date_created_str.replace("Z", "+00:00"))
            except Exception:
                created_at = datetime.now(timezone.utc)
        else:
            created_at = datetime.now(timezone.utc)

        date_closed_str = raw_order.get("date_closed") or raw_order.get("last_updated")
        updated_at = None
        if date_closed_str:
            try:
                updated_at = datetime.fromisoformat(date_closed_str.replace("Z", "+00:00"))
            except Exception:
                updated_at = None

        order_id = f"ord_{ext_order_id}"
        idempotency_key = f"idemp_order_{channel.channel_id}_{ext_order_id}"

        return Order(
            order_id=order_id,
            external_order_id=ext_order_id,
            channel=channel,
            status=order_status,
            payment_status=payment_status,
            fulfillment_status=fulfillment_status,
            items=tuple(items),
            total_amount=total_amount,
            currency=currency,
            buyer=buyer_ref,
            shipment=shipment_ref,
            created_at=created_at,
            updated_at=updated_at,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            provenance=EvidenceProvenanceType.LIVE,
            confidence=Confidence.HIGH,
            raw_reference={
                "tags": list(raw_order.get("tags") or []),
                "date_created": date_created_str,
            },
        )

    def fetch_orders(
        self,
        channel: SalesChannel,
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> OrderQueryResult:
        """
        Consulta órdenes en Mercado Libre vía GET /orders/search.
        """
        user_id = channel.metadata.get("user_id") or self.default_user_id
        if not user_id:
            return OrderQueryResult(
                orders=(),
                total_count=0,
                channel=channel,
                errors=(
                    OrderError(
                        category=OrderErrorCategory.AUTHORIZATION,
                        message="Missing seller user_id in channel metadata",
                    ),
                ),
            )

        params: Dict[str, Any] = {
            "seller": str(user_id),
            "limit": min(limit, 50),
            "offset": max(offset, 0),
            "sort": "date_desc",
        }
        if status:
            params["order.status"] = status
        if date_from:
            params["order.date_created.from"] = date_from.isoformat()
        if date_to:
            params["order.date_created.to"] = date_to.isoformat()

        query_path = f"/orders/search?{urlencode(params)}"

        try:
            client = self._get_api_client(channel)
            raw_response = client.get(query_path)
        except MercadoLibreApiError as e:
            logger.warning("Mercado Libre API error fetching orders: %s", str(e))
            category = OrderErrorCategory.UNKNOWN
            retryable = False
            is_unknown = False

            if e.status_code == 404:
                category = OrderErrorCategory.NOT_FOUND
            elif e.status_code in (401, 403):
                category = OrderErrorCategory.AUTHORIZATION
            elif e.status_code == 429:
                category = OrderErrorCategory.RATE_LIMIT
                retryable = True
            elif e.status_code and e.status_code >= 500:
                category = OrderErrorCategory.EXTERNAL_SERVICE
                retryable = True
                is_unknown = True
            elif "unavailable" in str(e).lower() or "timeout" in str(e).lower():
                category = OrderErrorCategory.TIMEOUT
                retryable = True
                is_unknown = True

            return OrderQueryResult(
                orders=(),
                total_count=0,
                channel=channel,
                is_unknown=is_unknown,
                errors=(
                    OrderError(
                        category=category,
                        message=str(e),
                        code=str(e.status_code) if e.status_code else None,
                        retryable=retryable,
                    ),
                ),
            )
        except Exception as e:
            logger.exception("Unexpected error fetching orders: %s", str(e))
            return OrderQueryResult(
                orders=(),
                total_count=0,
                channel=channel,
                is_unknown=True,
                errors=(
                    OrderError(
                        category=OrderErrorCategory.UNKNOWN,
                        message=f"Unexpected error: {str(e)}",
                        retryable=True,
                    ),
                ),
            )

        # Parse results
        results = raw_response.get("results", [])
        total_count = int(raw_response.get("paging", {}).get("total", len(results)))

        normalized_orders: List[Order] = []
        errors: List[OrderError] = []

        for raw_ord in results:
            try:
                norm_ord = self.normalize_order(raw_ord, channel)
                normalized_orders.append(norm_ord)
            except Exception as ex:
                logger.warning("Failed to normalize order %s: %s", raw_ord.get("id"), str(ex))
                errors.append(
                    OrderError(
                        category=OrderErrorCategory.VALIDATION,
                        message=f"Failed to normalize order {raw_ord.get('id')}: {str(ex)}",
                    )
                )

        return OrderQueryResult(
            orders=tuple(normalized_orders),
            total_count=total_count,
            channel=channel,
            errors=tuple(errors),
            is_unknown=False,
        )

    def get_order_by_external_id(
        self,
        external_order_id: str,
        channel: SalesChannel,
    ) -> OrderQueryResult:
        """
        Obtiene una orden específica por ID vía GET /orders/{order_id}.
        """
        if not external_order_id or not external_order_id.strip():
            return OrderQueryResult(
                orders=(),
                total_count=0,
                channel=channel,
                errors=(
                    OrderError(
                        category=OrderErrorCategory.VALIDATION,
                        message="external_order_id cannot be empty",
                    ),
                ),
            )

        query_path = f"/orders/{external_order_id.strip()}"

        try:
            client = self._get_api_client(channel)
            raw_response = client.get(query_path)
        except MercadoLibreApiError as e:
            logger.warning("Mercado Libre API error getting order %s: %s", external_order_id, str(e))
            category = OrderErrorCategory.UNKNOWN
            retryable = False
            is_unknown = False

            if e.status_code == 404:
                category = OrderErrorCategory.NOT_FOUND
            elif e.status_code in (401, 403):
                category = OrderErrorCategory.AUTHORIZATION
            elif e.status_code == 429:
                category = OrderErrorCategory.RATE_LIMIT
                retryable = True
            elif e.status_code and e.status_code >= 500:
                category = OrderErrorCategory.EXTERNAL_SERVICE
                retryable = True
                is_unknown = True
            elif "unavailable" in str(e).lower() or "timeout" in str(e).lower():
                category = OrderErrorCategory.TIMEOUT
                retryable = True
                is_unknown = True

            return OrderQueryResult(
                orders=(),
                total_count=0,
                channel=channel,
                is_unknown=is_unknown,
                errors=(
                    OrderError(
                        category=category,
                        message=str(e),
                        code=str(e.status_code) if e.status_code else None,
                        retryable=retryable,
                    ),
                ),
            )
        except Exception as e:
            logger.exception("Unexpected error getting order %s: %s", external_order_id, str(e))
            return OrderQueryResult(
                orders=(),
                total_count=0,
                channel=channel,
                is_unknown=True,
                errors=(
                    OrderError(
                        category=OrderErrorCategory.UNKNOWN,
                        message=f"Unexpected error: {str(e)}",
                        retryable=True,
                    ),
                ),
            )

        try:
            norm_ord = self.normalize_order(raw_response, channel)
            return OrderQueryResult(
                orders=(norm_ord,),
                total_count=1,
                channel=channel,
                errors=(),
                is_unknown=False,
            )
        except Exception as ex:
            return OrderQueryResult(
                orders=(),
                total_count=0,
                channel=channel,
                errors=(
                    OrderError(
                        category=OrderErrorCategory.VALIDATION,
                        message=f"Failed to normalize order {external_order_id}: {str(ex)}",
                    ),
                ),
            )
