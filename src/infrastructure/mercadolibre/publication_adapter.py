import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, Mapping, Tuple
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import (
    ListingDraft,
    PublicationRequest,
    PublicationResult,
    PublicationStatus,
    PublicationError,
    PublicationErrorCategory,
    SalesChannel,
)
from src.domain.publication.ports import PublicationPort
from src.application.oauth.connection_service import OAuthConnectionService
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)

logger = logging.getLogger(__name__)


class MercadoLibrePublicationAdapter(PublicationPort):
    """
    Adaptador de infraestructura para Mercado Libre que implementa el PublicationPort.
    Desacopla completamente el dominio y la aplicación de los detalles HTTP y SDK de Mercado Libre.

    Responsabilidades:
    - Mapping de ListingDraft -> Mercado Libre /items POST request payload.
    - Autenticación segura mediante OAuthConnectionService / MercadoLibreApiClient.
    - Manejo estructurado de respuestas y errores (VALIDATION, AUTHORIZATION, RATE_LIMIT, TIMEOUT, EXTERNAL_SERVICE, UNKNOWN).
    - Preservación del estado UNKNOWN ante respuestas ambiguas o fallos de conectividad en POST.
    - Implementación de get_status(channel, external_reference) para consulta y recuperación de estado.
    - Cero filtración de secretos (tokens, credenciales) en logs o errores.
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
                "Neither api_client nor oauth_service was provided to MercadoLibrePublicationAdapter"
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

    def publish(self, request: PublicationRequest) -> PublicationResult:
        """
        Ejecuta la publicación de un ListingDraft en Mercado Libre POST /items.
        """
        draft = request.draft
        channel = request.channel

        # 1. Mapear ListingDraft al payload esperado por Mercado Libre
        payload = self.map_draft_to_payload(draft)

        try:
            client = self._get_api_client(channel)
        except Exception as e:
            # Fallo resolviendo autenticación o credenciales
            return PublicationResult(
                publication_id=None,
                channel=channel,
                status=PublicationStatus.FAILED,
                external_reference=None,
                permalink=None,
                errors=(
                    PublicationError(
                        category=PublicationErrorCategory.AUTHORIZATION,
                        message=f"Authentication setup failed: {str(e)}",
                        code="AUTH_SETUP_ERROR",
                        retryable=False,
                    ),
                ),
                metadata=MappingProxyType({
                    "request_id": request.request_id,
                    "correlation_id": request.correlation_id,
                    "idempotency_key": request.idempotency_key,
                }),
                confidence=Confidence.HIGH,
            )

        # 2. Llamada HTTP a la API de Mercado Libre
        try:
            raw_response = client.post("/items", payload=payload)
        except MercadoLibreApiError as exc:
            return self._handle_api_error(exc, channel, request=request)
        except Exception as exc:
            # Fallo no capturado o error de transporte/timeout no estándar
            # En POST, la publicación puede haberse creado remotamente -> UNKNOWN
            return PublicationResult(
                publication_id=None,
                channel=channel,
                status=PublicationStatus.UNKNOWN,
                external_reference=None,
                permalink=None,
                errors=(
                    PublicationError(
                        category=PublicationErrorCategory.UNKNOWN,
                        message=f"Unexpected transport failure during publish: {str(exc)}",
                        code="UNEXPECTED_PUBLISH_FAILURE",
                        retryable=True,
                    ),
                ),
                metadata=MappingProxyType({
                    "request_id": request.request_id,
                    "correlation_id": request.correlation_id,
                    "idempotency_key": request.idempotency_key,
                }),
                confidence=Confidence.LOW,
            )

        # 3. Mapear respuesta exitosa a PublicationResult
        return self.map_response_to_result(raw_response, channel, request=request)

    def get_status(self, channel: SalesChannel, external_reference: str) -> PublicationResult:
        """
        Consulta el estado de una publicación existente en Mercado Libre GET /items/{item_id}.
        Permite verificar y recuperar publicaciones en estado UNKNOWN sin duplicar.
        """
        if not external_reference or not external_reference.strip():
            return PublicationResult(
                publication_id=None,
                channel=channel,
                status=PublicationStatus.FAILED,
                external_reference=None,
                errors=(
                    PublicationError(
                        category=PublicationErrorCategory.VALIDATION,
                        message="external_reference cannot be empty",
                        code="INVALID_EXTERNAL_REFERENCE",
                        retryable=False,
                    ),
                ),
            )

        try:
            client = self._get_api_client(channel)
        except Exception as e:
            return PublicationResult(
                publication_id=None,
                channel=channel,
                status=PublicationStatus.FAILED,
                external_reference=external_reference,
                errors=(
                    PublicationError(
                        category=PublicationErrorCategory.AUTHORIZATION,
                        message=f"Authentication setup failed: {str(e)}",
                        code="AUTH_SETUP_ERROR",
                        retryable=False,
                    ),
                ),
            )

        try:
            raw_response = client.get(f"/items/{external_reference}")
        except MercadoLibreApiError as exc:
            return self._handle_api_error(exc, channel, external_reference=external_reference)
        except Exception as exc:
            return PublicationResult(
                publication_id=None,
                channel=channel,
                status=PublicationStatus.UNKNOWN,
                external_reference=external_reference,
                errors=(
                    PublicationError(
                        category=PublicationErrorCategory.UNKNOWN,
                        message=f"Failed to verify status due to unexpected error: {str(exc)}",
                        code="UNEXPECTED_STATUS_ERROR",
                        retryable=True,
                    ),
                ),
                confidence=Confidence.LOW,
            )

        return self.map_response_to_result(raw_response, channel, external_reference=external_reference)

    def map_draft_to_payload(self, draft: ListingDraft) -> Dict[str, Any]:
        """
        Mapea un ListingDraft puro de dominio a la estructura requerida por Mercado Libre /items.
        """
        # Formateo de precio a float/int estándar
        price_val = float(draft.price) if draft.price % 1 != 0 else int(draft.price)

        # Pictures: lista de URLs [{"source": url}, ...]
        pictures = [{"source": img} for img in draft.images if img]

        # Attributes: lista de dicts [{"id": k, "value_name": str(v)}, ...]
        attributes_list = []
        for k, v in draft.attributes.items():
            attributes_list.append({"id": str(k), "value_name": str(v)})

        # Si viene SKU explícito y no está en attributes, añadir SELLER_SKU
        if draft.sku and not any(a["id"] == "SELLER_SKU" for a in attributes_list):
            attributes_list.append({"id": "SELLER_SKU", "value_name": draft.sku})

        # Category ID
        category_id = draft.category_id or draft.metadata.get("category_id") or "MLC1055"

        # Listing Type ID (ej: gold_special, gold_pro, etc.)
        listing_type_id = draft.metadata.get("listing_type_id", "gold_special")

        # Buying Mode (buy_it_now)
        buying_mode = draft.metadata.get("buying_mode", "buy_it_now")

        payload: Dict[str, Any] = {
            "title": draft.title,
            "category_id": category_id,
            "price": price_val,
            "currency_id": draft.currency,
            "available_quantity": draft.available_quantity,
            "buying_mode": buying_mode,
            "listing_type_id": listing_type_id,
            "condition": draft.condition,
            "description": {"plain_text": draft.description},
        }

        if pictures:
            payload["pictures"] = pictures

        if attributes_list:
            payload["attributes"] = attributes_list

        # Shipping si existe en metadata
        if "shipping" in draft.metadata:
            payload["shipping"] = dict(draft.metadata["shipping"])

        return payload

    def map_response_to_result(
        self,
        response_data: Dict[str, Any],
        channel: SalesChannel,
        request: Optional[PublicationRequest] = None,
        external_reference: Optional[str] = None,
    ) -> PublicationResult:
        """
        Mapea la respuesta JSON de Mercado Libre a un PublicationResult inmutable de dominio.
        """
        item_id = response_data.get("id") or external_reference
        permalink = response_data.get("permalink")
        meli_status = response_data.get("status")  # active, paused, closed, under_review, pending, etc.

        # Determinar status de dominio
        if meli_status in ("active", "paused", "not_yet_active"):
            status = PublicationStatus.PUBLISHED
        elif meli_status in ("pending", "under_review", "payment_required"):
            status = PublicationStatus.PENDING
        elif meli_status in ("closed", "inactive"):
            status = PublicationStatus.PUBLISHED  # Existe remotamente pero está cerrada
        else:
            # Respuesta malformada o estado no reconocido
            if not item_id:
                return PublicationResult(
                    publication_id=None,
                    channel=channel,
                    status=PublicationStatus.UNKNOWN,
                    external_reference=None,
                    permalink=permalink,
                    errors=(
                        PublicationError(
                            category=PublicationErrorCategory.UNKNOWN,
                            message=f"Malformed Mercado Libre response: missing item ID (status={meli_status})",
                            code="MALFORMED_RESPONSE",
                            retryable=True,
                        ),
                    ),
                    confidence=Confidence.LOW,
                )
            status = PublicationStatus.UNKNOWN

        published_at = datetime.now(timezone.utc)
        meta_dict: Dict[str, Any] = {
            "meli_status": meli_status,
            "site_id": response_data.get("site_id"),
            "seller_id": response_data.get("seller_id"),
        }
        if request:
            meta_dict["request_id"] = request.request_id
            meta_dict["correlation_id"] = request.correlation_id
            meta_dict["idempotency_key"] = request.idempotency_key

        return PublicationResult(
            publication_id=item_id,
            channel=channel,
            status=status,
            external_reference=item_id,
            permalink=permalink,
            published_at=published_at if status == PublicationStatus.PUBLISHED else None,
            errors=(),
            metadata=MappingProxyType(meta_dict),
            confidence=Confidence.HIGH,
        )

    def _handle_api_error(
        self,
        exc: MercadoLibreApiError,
        channel: SalesChannel,
        request: Optional[PublicationRequest] = None,
        external_reference: Optional[str] = None,
    ) -> PublicationResult:
        """
        Mapea MercadoLibreApiError a categorías estructuradas de PublicationError y status de dominio.
        """
        status_code = exc.status_code
        err_msg = str(exc)
        parsed_body = {}
        if exc.response_body:
            try:
                parsed_body = json.loads(exc.response_body)
            except Exception:
                pass

        meli_error = parsed_body.get("error") or parsed_body.get("message") or err_msg
        meli_code = parsed_body.get("code") or str(status_code)
        details = parsed_body.get("cause") or {}

        # Mapeo según status code
        if status_code in (400, 422):
            category = PublicationErrorCategory.VALIDATION
            domain_status = PublicationStatus.FAILED
            retryable = False
        elif status_code in (401, 403):
            category = PublicationErrorCategory.AUTHORIZATION
            domain_status = PublicationStatus.FAILED
            retryable = False
        elif status_code == 429:
            category = PublicationErrorCategory.RATE_LIMIT
            domain_status = PublicationStatus.FAILED
            retryable = True
        elif status_code is not None and status_code >= 500:
            category = PublicationErrorCategory.EXTERNAL_SERVICE
            # 5xx en POST puede haber procesado la creación -> UNKNOWN
            domain_status = PublicationStatus.UNKNOWN if request is not None else PublicationStatus.FAILED
            retryable = True
        else:
            # Timeouts de red o errores de conexión (URLError, timeout)
            if "unavailable" in err_msg.lower() or "timeout" in err_msg.lower():
                category = PublicationErrorCategory.TIMEOUT
                domain_status = PublicationStatus.UNKNOWN if request is not None else PublicationStatus.FAILED
                retryable = True
            else:
                category = PublicationErrorCategory.UNKNOWN
                domain_status = PublicationStatus.UNKNOWN if request is not None else PublicationStatus.FAILED
                retryable = True

        pub_error = PublicationError(
            category=category,
            message=str(meli_error),
            code=str(meli_code),
            details=details if isinstance(details, dict) else {"raw_details": details},
            retryable=retryable,
        )

        meta_dict: Dict[str, Any] = {
            "http_status": status_code,
        }
        if request:
            meta_dict["request_id"] = request.request_id
            meta_dict["correlation_id"] = request.correlation_id
            meta_dict["idempotency_key"] = request.idempotency_key

        return PublicationResult(
            publication_id=None,
            channel=channel,
            status=domain_status,
            external_reference=external_reference,
            permalink=None,
            errors=(pub_error,),
            metadata=MappingProxyType(meta_dict),
            confidence=Confidence.LOW if domain_status == PublicationStatus.UNKNOWN else Confidence.HIGH,
        )
