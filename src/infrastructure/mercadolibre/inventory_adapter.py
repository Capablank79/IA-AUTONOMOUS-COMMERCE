import logging
from typing import Dict, Any, Optional
from decimal import Decimal

from src.domain.publication.models import SalesChannel
from src.domain.inventory.models import (
    InventoryRequest,
    InventoryResult,
    InventoryStatus,
    InventoryError,
    InventoryErrorCategory,
)
from src.domain.inventory.ports import InventoryPort
from src.application.oauth.connection_service import OAuthConnectionService
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)

logger = logging.getLogger(__name__)


class MercadoLibreInventoryAdapter(InventoryPort):
    """
    Adaptador de infraestructura para Mercado Libre que implementa el InventoryPort (Hito G.5).
    Desacopla completamente el dominio y la aplicación de los detalles HTTP y SDK de Mercado Libre.

    Responsabilidades:
    - Actualización de inventario/stock en Mercado Libre: PUT /items/{item_id} con payload {"available_quantity": int}.
    - Consulta de stock actual / estado: GET /items/{item_id}.
    - Autenticación segura mediante OAuthConnectionService / MercadoLibreApiClient reutilizando E-01.3.
    - Manejo estructurado de errores y taxonomía: VALIDATION, AUTHORIZATION, NOT_FOUND, CONFLICT, RATE_LIMIT, UNKNOWN.
    - Preservación estricta del estado UNKNOWN ante timeouts, caídas de red o errores 5xx.
    - Reconciliación segura: VERIFY_CURRENT_STOCK antes de asumir FAILED o duplicar operaciones.
    - Cero exposición o almacenamiento de credenciales en logs o errores.
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
        Obtiene o construye el cliente HTTP autenticado de Mercado Libre para el canal.
        """
        if self.api_client is not None:
            return self.api_client

        if self.oauth_service is None:
            raise ValueError(
                "Neither api_client nor oauth_service was provided to MercadoLibreInventoryAdapter"
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

    def map_request_to_payload(self, request: InventoryRequest) -> Dict[str, Any]:
        """
        Mapea el InventoryRequest interno al payload estándar de Mercado Libre PUT /items/{id}.
        """
        # Mercado Libre espera 'available_quantity': int en PUT /items/{id}
        return {
            "available_quantity": int(request.proposed_quantity)
        }

    def update_inventory(self, request: InventoryRequest) -> InventoryResult:
        """
        Ejecuta la actualización de inventario/stock en Mercado Libre mediante PUT /items/{item_id}.
        """
        channel = request.channel
        listing_id = request.listing_id

        payload = self.map_request_to_payload(request)

        try:
            client = self._get_api_client(channel)
        except Exception as e:
            logger.warning("Failed to obtain Mercado Libre API client for inventory: %s", str(e))
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.FAILED,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=request.current_quantity,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.AUTHORIZATION,
                        message=f"Failed to authenticate with sales channel: {str(e)}",
                        code="CHANNEL_AUTH_ERROR",
                    ),
                ),
                raw_response=None,
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                correlation_id=request.correlation_id,
            )

        try:
            path = f"/items/{listing_id}"
            response_data = client.put(path, payload=payload)
            return self._parse_successful_response(response_data, channel, listing_id, request)

        except MercadoLibreApiError as exc:
            return self._handle_api_error(exc, channel, listing_id, request)
        except Exception as exc:
            logger.error("Unexpected error updating inventory in Mercado Libre: %s", str(exc))
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.UNKNOWN,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=request.current_quantity,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.UNKNOWN,
                        message=f"Unexpected error during inventory update: {str(exc)}",
                        code="UNEXPECTED_CLIENT_ERROR",
                    ),
                ),
                raw_response=None,
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                correlation_id=request.correlation_id,
            )

    def get_current_stock(self, channel: SalesChannel, listing_id: str) -> InventoryResult:
        """
        Consulta el estado y stock actual de una publicación en Mercado Libre mediante GET /items/{item_id}.
        Fundamental para la reconciliación tras un estado UNKNOWN o para sincronización proactiva.
        """
        try:
            client = self._get_api_client(channel)
        except Exception as e:
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.FAILED,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=None,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.AUTHORIZATION,
                        message=f"Failed to authenticate with channel: {str(e)}",
                        code="CHANNEL_AUTH_ERROR",
                    ),
                ),
                raw_response=None,
            )

        try:
            path = f"/items/{listing_id}"
            response_data = client.get(path)
            
            qty_val = response_data.get("available_quantity")
            current_qty = int(qty_val) if qty_val is not None else None
            status_str = response_data.get("status", "unknown")

            return InventoryResult(
                inventory_id=response_data.get("id", listing_id),
                channel=channel,
                status=InventoryStatus.APPLIED if current_qty is not None else InventoryStatus.UNKNOWN,
                listing_id=listing_id,
                applied_quantity=current_qty,
                previous_quantity=None,
                errors=(),
                raw_response={
                    "status": status_str,
                    "available_quantity": qty_val,
                    "title": response_data.get("title"),
                },
            )
        except MercadoLibreApiError as exc:
            return self._handle_api_error(exc, channel, listing_id, None)
        except Exception as exc:
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.UNKNOWN,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=None,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.UNKNOWN,
                        message=f"Unexpected error querying listing inventory: {str(exc)}",
                        code="UNEXPECTED_CLIENT_ERROR",
                    ),
                ),
                raw_response=None,
            )

    def _parse_successful_response(
        self,
        response_data: Dict[str, Any],
        channel: SalesChannel,
        listing_id: str,
        request: InventoryRequest,
    ) -> InventoryResult:
        applied_qty_val = response_data.get("available_quantity")
        applied_qty = int(applied_qty_val) if applied_qty_val is not None else request.proposed_quantity

        safe_raw = {
            "id": response_data.get("id"),
            "available_quantity": applied_qty_val,
            "status": response_data.get("status"),
            "last_updated": response_data.get("last_updated"),
        }

        return InventoryResult(
            inventory_id=response_data.get("id", listing_id),
            channel=channel,
            status=InventoryStatus.APPLIED,
            listing_id=listing_id,
            applied_quantity=applied_qty,
            previous_quantity=request.current_quantity,
            errors=(),
            raw_response=safe_raw,
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            correlation_id=request.correlation_id,
        )

    def _handle_api_error(
        self,
        exc: MercadoLibreApiError,
        channel: SalesChannel,
        listing_id: str,
        request: Optional[InventoryRequest],
    ) -> InventoryResult:
        status_code = exc.status_code
        err_msg = str(exc)
        req_id = request.request_id if request else None
        idem_key = request.idempotency_key if request else None
        corr_id = request.correlation_id if request else None
        prev_qty = request.current_quantity if request else None

        # 1. 400 / 422 -> VALIDATION
        if status_code in (400, 422):
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.FAILED,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=prev_qty,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.VALIDATION,
                        message=f"Validation failed on marketplace: {err_msg}",
                        code=f"HTTP_{status_code}",
                        details={"status_code": status_code, "body": exc.response_body},
                    ),
                ),
                raw_response={"error": exc.response_body},
                request_id=req_id,
                idempotency_key=idem_key,
                correlation_id=corr_id,
            )

        # 2. 401 / 403 -> AUTHORIZATION
        if status_code in (401, 403):
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.FAILED,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=prev_qty,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.AUTHORIZATION,
                        message=f"Authorization rejected by marketplace: {err_msg}",
                        code=f"HTTP_{status_code}",
                        details={"status_code": status_code},
                    ),
                ),
                raw_response={"error": exc.response_body},
                request_id=req_id,
                idempotency_key=idem_key,
                correlation_id=corr_id,
            )

        # 3. 404 -> NOT_FOUND
        if status_code == 404:
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.FAILED,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=prev_qty,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.NOT_FOUND,
                        message=f"Listing resource '{listing_id}' not found on marketplace.",
                        code="HTTP_404",
                        details={"status_code": 404},
                    ),
                ),
                raw_response={"error": exc.response_body},
                request_id=req_id,
                idempotency_key=idem_key,
                correlation_id=corr_id,
            )

        # 4. 409 -> CONFLICT
        if status_code == 409:
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.FAILED,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=prev_qty,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.CONFLICT,
                        message=f"Conflict updating inventory on marketplace: {err_msg}",
                        code="HTTP_409",
                        details={"status_code": 409, "body": exc.response_body},
                    ),
                ),
                raw_response={"error": exc.response_body},
                request_id=req_id,
                idempotency_key=idem_key,
                correlation_id=corr_id,
            )

        # 5. 429 -> RATE_LIMIT
        if status_code == 429:
            return InventoryResult(
                inventory_id=None,
                channel=channel,
                status=InventoryStatus.FAILED,
                listing_id=listing_id,
                applied_quantity=None,
                previous_quantity=prev_qty,
                errors=(
                    InventoryError(
                        category=InventoryErrorCategory.RATE_LIMIT,
                        message=f"Rate limit exceeded on marketplace: {err_msg}",
                        code="HTTP_429",
                        details={"status_code": 429},
                    ),
                ),
                raw_response={"error": exc.response_body},
                request_id=req_id,
                idempotency_key=idem_key,
                correlation_id=corr_id,
            )

        # 6. 5xx o timeout (status_code is None or >= 500) -> UNKNOWN
        # NUNCA degradar a FAILED cuando hay ambigüedad de ejecución en el servidor externo
        return InventoryResult(
            inventory_id=None,
            channel=channel,
            status=InventoryStatus.UNKNOWN,
            listing_id=listing_id,
            applied_quantity=None,
            previous_quantity=prev_qty,
            errors=(
                InventoryError(
                    category=InventoryErrorCategory.UNKNOWN,
                    message=(
                        f"External inventory operation outcome is UNKNOWN due to server/network ambiguity "
                        f"(HTTP {status_code or 'TIMEOUT/NETWORK'}): {err_msg}"
                    ),
                    code=f"HTTP_{status_code or 'UNKNOWN'}",
                    details={"status_code": status_code, "body": exc.response_body},
                ),
            ),
            raw_response={"error": exc.response_body},
            request_id=req_id,
            idempotency_key=idem_key,
            correlation_id=corr_id,
        )
