import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, Mapping, Tuple
from types import MappingProxyType

from src.domain.publication.models import SalesChannel
from src.domain.pricing.models import (
    PricingRequest,
    PricingResult,
    PricingStatus,
    PricingError,
    PricingErrorCategory,
)
from src.domain.pricing.ports import PricingPort
from src.application.oauth.connection_service import OAuthConnectionService
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)

logger = logging.getLogger(__name__)


class MercadoLibrePricingAdapter(PricingPort):
    """
    Adaptador de infraestructura para Mercado Libre que implementa el PricingPort (Hito G.4).
    Desacopla completamente el dominio y la aplicación de los detalles HTTP y SDK de Mercado Libre.

    Responsabilidades:
    - Actualización de precio en Mercado Libre: PUT /items/{item_id} con payload {"price": float}.
    - Consulta de precio actual / estado: GET /items/{item_id}.
    - Autenticación segura mediante OAuthConnectionService / MercadoLibreApiClient reutilizando E-01.3.
    - Manejo estructurado de errores y taxonomía: VALIDATION, AUTHORIZATION, NOT_FOUND, CONFLICT, RATE_LIMIT, UNKNOWN.
    - Preservación estricta del estado UNKNOWN ante timeouts, caídas de red o errores 5xx.
    - Reconciliación segura: VERIFY_CURRENT_PRICE antes de asumir FAILED o duplicar operaciones.
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
                "Neither api_client nor oauth_service was provided to MercadoLibrePricingAdapter"
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

    def map_request_to_payload(self, request: PricingRequest) -> Dict[str, Any]:
        """
        Mapea el PricingRequest interno al payload estándar de Mercado Libre PUT /items/{id}.
        """
        # Mercado Libre espera 'price': float/int en PUT /items/{id}
        return {
            "price": float(request.proposed_price)
        }

    def update_price(self, request: PricingRequest) -> PricingResult:
        """
        Ejecuta la actualización de precio en Mercado Libre mediante PUT /items/{item_id}.
        """
        channel = request.channel
        listing_id = request.listing_id
        proposed_price = request.proposed_price

        payload = self.map_request_to_payload(request)

        try:
            client = self._get_api_client(channel)
        except Exception as e:
            logger.warning("Failed to obtain Mercado Libre API client for pricing: %s", str(e))
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.FAILED,
                listing_id=listing_id,
                applied_price=None,
                previous_price=request.current_price,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.AUTHORIZATION,
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
            logger.error("Unexpected error updating price in Mercado Libre: %s", str(exc))
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.UNKNOWN,
                listing_id=listing_id,
                applied_price=None,
                previous_price=request.current_price,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.UNKNOWN,
                        message=f"Unexpected error during price update: {str(exc)}",
                        code="UNEXPECTED_CLIENT_ERROR",
                    ),
                ),
                raw_response=None,
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                correlation_id=request.correlation_id,
            )

    def get_current_price(self, channel: SalesChannel, listing_id: str) -> PricingResult:
        """
        Consulta el estado y precio actual de una publicación en Mercado Libre mediante GET /items/{item_id}.
        Fundamental para la reconciliación tras un estado UNKNOWN.
        """
        try:
            client = self._get_api_client(channel)
        except Exception as e:
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.FAILED,
                listing_id=listing_id,
                applied_price=None,
                previous_price=None,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.AUTHORIZATION,
                        message=f"Failed to authenticate with channel: {str(e)}",
                        code="CHANNEL_AUTH_ERROR",
                    ),
                ),
                raw_response=None,
            )

        try:
            path = f"/items/{listing_id}"
            response_data = client.get(path)
            
            price_val = response_data.get("price")
            current_price = Decimal(str(price_val)) if price_val is not None else None
            status_str = response_data.get("status", "unknown")

            return PricingResult(
                pricing_id=response_data.get("id", listing_id),
                channel=channel,
                status=PricingStatus.APPLIED if current_price is not None else PricingStatus.UNKNOWN,
                listing_id=listing_id,
                applied_price=current_price,
                previous_price=None,
                currency=response_data.get("currency_id", "CLP"),
                errors=(),
                raw_response={
                    "status": status_str,
                    "price": price_val,
                    "currency_id": response_data.get("currency_id"),
                    "title": response_data.get("title"),
                },
            )
        except MercadoLibreApiError as exc:
            return self._handle_api_error(exc, channel, listing_id, None)
        except Exception as exc:
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.UNKNOWN,
                listing_id=listing_id,
                applied_price=None,
                previous_price=None,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.UNKNOWN,
                        message=f"Unexpected error querying listing price: {str(exc)}",
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
        request: PricingRequest,
    ) -> PricingResult:
        applied_price_val = response_data.get("price")
        applied_price = Decimal(str(applied_price_val)) if applied_price_val is not None else request.proposed_price
        currency = response_data.get("currency_id", request.currency)

        safe_raw = {
            "id": response_data.get("id"),
            "price": applied_price_val,
            "currency_id": currency,
            "status": response_data.get("status"),
            "last_updated": response_data.get("last_updated"),
        }

        return PricingResult(
            pricing_id=response_data.get("id", listing_id),
            channel=channel,
            status=PricingStatus.APPLIED,
            listing_id=listing_id,
            applied_price=applied_price,
            previous_price=request.current_price,
            currency=currency,
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
        request: Optional[PricingRequest],
    ) -> PricingResult:
        status_code = exc.status_code
        err_msg = str(exc)
        req_id = request.request_id if request else None
        idem_key = request.idempotency_key if request else None
        corr_id = request.correlation_id if request else None
        prev_price = request.current_price if request else None

        # 1. 400 / 422 -> VALIDATION
        if status_code in (400, 422):
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.FAILED,
                listing_id=listing_id,
                applied_price=None,
                previous_price=prev_price,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.VALIDATION,
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
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.FAILED,
                listing_id=listing_id,
                applied_price=None,
                previous_price=prev_price,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.AUTHORIZATION,
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
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.FAILED,
                listing_id=listing_id,
                applied_price=None,
                previous_price=prev_price,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.NOT_FOUND,
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
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.FAILED,
                listing_id=listing_id,
                applied_price=None,
                previous_price=prev_price,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.CONFLICT,
                        message=f"Conflict updating price on marketplace: {err_msg}",
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
            return PricingResult(
                pricing_id=None,
                channel=channel,
                status=PricingStatus.FAILED,
                listing_id=listing_id,
                applied_price=None,
                previous_price=prev_price,
                errors=(
                    PricingError(
                        category=PricingErrorCategory.RATE_LIMIT,
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
        return PricingResult(
            pricing_id=None,
            channel=channel,
            status=PricingStatus.UNKNOWN,
            listing_id=listing_id,
            applied_price=None,
            previous_price=prev_price,
            errors=(
                PricingError(
                    category=PricingErrorCategory.UNKNOWN,
                    message=(
                        f"External pricing operation outcome is UNKNOWN due to server/network ambiguity "
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
