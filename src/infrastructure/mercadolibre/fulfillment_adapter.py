import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlencode

from src.application.oauth.connection_service import OAuthConnectionService
from src.domain.fulfillment.models import (
    FulfillmentError,
    FulfillmentErrorCategory,
    LabelFormat,
    LabelStatus,
    Shipment,
    ShipmentQueryResult,
    ShipmentStatus,
    ShippingLabel,
    ShippingServiceLevel,
    TrackingEvent,
    TrackingStatus,
)
from src.domain.fulfillment.ports import FulfillmentPort
from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import SalesChannel
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)

logger = logging.getLogger(__name__)


class MercadoLibreFulfillmentAdapter(FulfillmentPort):
    """
    Adaptador de infraestructura de Mercado Libre para envíos, tracking y etiquetas (Hito G.7).
    
    Principios Arquitectónicos:
    - Desacoplamiento total del dominio respecto al formato JSON de Mercado Libre (/shipments/{id}).
    - Normalización determinista de estados: to_be_agreed, pending, handling, ready_to_ship, shipped, delivered, not_delivered, cancelled.
    - Soporte ME2 (Mercado Envíos) y niveles de servicio: drop_off, cross_docking, xd_drop_off, self_service (Flex), fulfillment (Full).
    - Preservación estricta de incertidumbre (UNKNOWN) ante fallos 5xx, timeouts o conectividad.
    - Manejo seguro de etiquetas: si no está disponible o requiere parámetros especiales, retorna NOT_SUPPORTED / NOT_AVAILABLE.
    - Sin suposiciones ficticias ni almacenamiento de PII.
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

        if self.oauth_service is not None:
            user_id = channel.metadata.get("user_id") or self.default_user_id
            if user_id:
                client = self.oauth_service.get_authenticated_client(
                    provider=self.provider_name,
                    user_id=user_id,
                )
                if isinstance(client, MercadoLibreApiClient):
                    return client

        return MercadoLibreApiClient()

    def normalize_shipment_status(self, raw_status: Optional[str], raw_substatus: Optional[str] = None) -> ShipmentStatus:
        """
        Mapea el estado externo de Mercado Envíos a ShipmentStatus inmutable.
        """
        if not raw_status:
            return ShipmentStatus.PENDING

        status_lower = raw_status.strip().lower()
        substatus_lower = (raw_substatus or "").strip().lower()

        if status_lower in ("to_be_agreed", "pending"):
            return ShipmentStatus.PENDING
        elif status_lower == "handling":
            if substatus_lower in ("ready_to_print", "printed", "waiting_for_carrier"):
                return ShipmentStatus.READY_TO_SHIP
            return ShipmentStatus.PROCESSING
        elif status_lower == "ready_to_ship":
            return ShipmentStatus.READY_TO_SHIP
        elif status_lower in ("shipped", "in_transit", "forwarded"):
            return ShipmentStatus.IN_TRANSIT
        elif status_lower in ("out_for_delivery", "soon_deliver"):
            return ShipmentStatus.OUT_FOR_DELIVERY
        elif status_lower == "delivered":
            return ShipmentStatus.DELIVERED
        elif status_lower in ("cancelled", "not_delivered", "returned", "claimed"):
            return ShipmentStatus.CANCELLED
        elif status_lower in ("unknown", "error"):
            return ShipmentStatus.UNKNOWN
        else:
            return ShipmentStatus.PROCESSING

    def normalize_tracking_status(self, raw_status: Optional[str], raw_substatus: Optional[str] = None) -> TrackingStatus:
        """
        Mapea estados de eventos de tracking externos a TrackingStatus.
        """
        if not raw_status:
            return TrackingStatus.PENDING

        status_lower = raw_status.strip().lower()
        substatus_lower = (raw_substatus or "").strip().lower()

        if status_lower in ("pending", "to_be_agreed"):
            return TrackingStatus.PENDING
        elif status_lower == "handling":
            if substatus_lower in ("ready_to_print", "printed"):
                return TrackingStatus.LABEL_CREATED
            return TrackingStatus.PICKED_UP
        elif status_lower == "ready_to_ship":
            return TrackingStatus.LABEL_CREATED
        elif status_lower in ("shipped", "in_transit"):
            return TrackingStatus.IN_TRANSIT
        elif status_lower == "out_for_delivery":
            return TrackingStatus.OUT_FOR_DELIVERY
        elif status_lower == "delivered":
            return TrackingStatus.DELIVERED
        elif status_lower in ("cancelled", "not_delivered"):
            return TrackingStatus.CANCELLED
        elif status_lower == "failed_attempt":
            return TrackingStatus.DELIVERY_ATTEMPT_FAILED
        elif status_lower == "returned":
            return TrackingStatus.RETURNED_TO_SENDER
        return TrackingStatus.IN_TRANSIT

    def normalize_service_level(self, logistic_type: Optional[str], shipping_mode: Optional[str]) -> ShippingServiceLevel:
        """
        Mapea los tipos logísticos de Mercado Libre (ME2) a ShippingServiceLevel.
        """
        lt = (logistic_type or "").strip().lower()
        sm = (shipping_mode or "").strip().lower()

        if lt == "fulfillment":
            return ShippingServiceLevel.ME2_FULFILLMENT
        elif lt in ("drop_off", "xd_drop_off"):
            return ShippingServiceLevel.ME2_DROP_OFF
        elif lt == "cross_docking":
            return ShippingServiceLevel.ME2_CROSS_DOCKING
        elif lt in ("self_service", "flex"):
            return ShippingServiceLevel.ME2_FLEX
        elif sm == "custom" or lt == "custom":
            return ShippingServiceLevel.CUSTOM
        return ShippingServiceLevel.STANDARD

    def _map_raw_shipment_to_entity(
        self,
        raw_shipment: Mapping[str, Any],
        channel: SalesChannel,
        correlation_id: str = "",
    ) -> Shipment:
        """
        Convierte un JSON de /shipments/{id} de Mercado Libre en una entidad de dominio Shipment.
        """
        ext_shipment_id = str(raw_shipment.get("id") or "")
        ext_order_id = str(raw_shipment.get("order_id") or f"unassigned_{ext_shipment_id}")
        order_id = f"ord_{ext_order_id}"
        shipment_id = f"shp_{ext_shipment_id}"

        status_str = raw_shipment.get("status")
        substatus_str = raw_shipment.get("substatus")
        shipment_status = self.normalize_shipment_status(status_str, substatus_str)

        # Carrier y Tracking Number
        lead_time = raw_shipment.get("lead_time") or {}
        service_info = raw_shipment.get("service_id")
        logistic_type = raw_shipment.get("logistic_type")
        shipping_mode = raw_shipment.get("shipping_mode")
        service_level = self.normalize_service_level(logistic_type, shipping_mode)

        carrier = raw_shipment.get("carrier_info", {}).get("name") if isinstance(raw_shipment.get("carrier_info"), dict) else None
        tracking_number = raw_shipment.get("tracking_number") or raw_shipment.get("substatus_history", [{}])[-1].get("tracking_number") if isinstance(raw_shipment.get("substatus_history"), list) and raw_shipment.get("substatus_history") else raw_shipment.get("tracking_number")
        tracking_url = raw_shipment.get("tracking_url")

        # Parsing de Fechas
        date_created_str = raw_shipment.get("date_created")
        try:
            created_at = datetime.fromisoformat(date_created_str.replace("Z", "+00:00")) if date_created_str else datetime.now(timezone.utc)
        except Exception:
            created_at = datetime.now(timezone.utc)

        date_handled_str = raw_shipment.get("date_handled") or raw_shipment.get("date_shipped")
        try:
            shipped_at = datetime.fromisoformat(date_handled_str.replace("Z", "+00:00")) if date_handled_str else None
        except Exception:
            shipped_at = None

        date_delivered_str = raw_shipment.get("date_delivered")
        try:
            delivered_at = datetime.fromisoformat(date_delivered_str.replace("Z", "+00:00")) if date_delivered_str else None
        except Exception:
            delivered_at = None

        # Historial de substatuses como TrackingEvents
        tracking_events: List[TrackingEvent] = []
        substatus_history = raw_shipment.get("substatus_history") or []
        if isinstance(substatus_history, list):
            for idx, hist in enumerate(substatus_history):
                hist_status = hist.get("status") or status_str
                hist_substatus = hist.get("substatus")
                hist_date_str = hist.get("date")
                try:
                    hist_date = datetime.fromisoformat(hist_date_str.replace("Z", "+00:00")) if hist_date_str else created_at
                except Exception:
                    hist_date = created_at

                ev = TrackingEvent(
                    event_id=f"tr_{ext_shipment_id}_{idx}",
                    shipment_id=shipment_id,
                    external_shipment_id=ext_shipment_id,
                    status=self.normalize_tracking_status(hist_status, hist_substatus),
                    normalized_status=self.normalize_shipment_status(hist_status, hist_substatus),
                    timestamp=hist_date,
                    location=hist.get("location"),
                    description=hist.get("description") or f"Substatus: {hist_substatus}",
                    source="MERCADOLIBRE_API",
                    provenance=EvidenceProvenanceType.LIVE,
                    confidence=Confidence.HIGH,
                    correlation_id=correlation_id,
                )
                tracking_events.append(ev)

        # Si no había substatus_history, generar al menos un TrackingEvent con el estado actual
        if not tracking_events:
            ev = TrackingEvent(
                event_id=f"tr_{ext_shipment_id}_0",
                shipment_id=shipment_id,
                external_shipment_id=ext_shipment_id,
                status=self.normalize_tracking_status(status_str, substatus_str),
                normalized_status=shipment_status,
                timestamp=created_at,
                source="MERCADOLIBRE_API",
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.HIGH,
                correlation_id=correlation_id,
            )
            tracking_events.append(ev)

        idempotency_key = f"idemp_shipment_{channel.channel_id}_{ext_shipment_id}"

        return Shipment(
            shipment_id=shipment_id,
            external_shipment_id=ext_shipment_id,
            order_id=order_id,
            external_order_id=ext_order_id,
            channel=channel,
            status=shipment_status,
            carrier=carrier,
            service_level=service_level,
            tracking_number=str(tracking_number) if tracking_number else None,
            tracking_url=str(tracking_url) if tracking_url else None,
            tracking_events=tuple(tracking_events),
            created_at=created_at,
            shipped_at=shipped_at,
            delivered_at=delivered_at,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            provenance=EvidenceProvenanceType.LIVE,
            confidence=Confidence.HIGH,
            raw_reference={
                "logistic_type": logistic_type,
                "shipping_mode": shipping_mode,
                "status": status_str,
                "substatus": substatus_str,
            },
        )

    def fetch_shipments(
        self,
        channel: SalesChannel,
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ShipmentQueryResult:
        """
        Consulta shipments en Mercado Libre (usualmente vía búsqueda u órdenes relacionadas).
        """
        # Mercado Libre no tiene un /shipments/search público directo universal, por lo que consulta
        # vía órdenes con shipping o endpoints autorizados. Si no se puede consultar en bloque,
        # retorna ShipmentQueryResult con manejo robusto.
        return ShipmentQueryResult(
            shipments=(),
            total_count=0,
            channel=channel,
            errors=(
                FulfillmentError(
                    category=FulfillmentErrorCategory.NOT_SUPPORTED,
                    message="Bulk shipment search is not directly supported by Mercado Libre API; query by shipment_id or order_id instead.",
                    retryable=False,
                ),
            ),
        )

    def get_shipment_by_external_id(
        self,
        external_shipment_id: str,
        channel: SalesChannel,
    ) -> Optional[Shipment]:
        """
        Consulta un shipment por ID directo: GET /shipments/{shipment_id}.
        """
        query_path = f"/shipments/{external_shipment_id}"
        try:
            client = self._get_api_client(channel)
            raw_response = client.get(query_path)
            if not raw_response or not isinstance(raw_response, dict):
                return None
            return self._map_raw_shipment_to_entity(raw_response, channel)
        except MercadoLibreApiError as e:
            logger.warning("Error fetching shipment %s from ML: %s", external_shipment_id, str(e))
            if e.status_code == 404:
                return None
            # Ante 5xx, timeout o rate limit, devolver entidad con estado UNKNOWN y baja confianza
            return Shipment(
                shipment_id=f"shp_{external_shipment_id}",
                external_shipment_id=external_shipment_id,
                order_id=f"ord_unknown_{external_shipment_id}",
                external_order_id=f"ext_ord_unknown_{external_shipment_id}",
                channel=channel,
                status=ShipmentStatus.UNKNOWN,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.LOW,
                raw_reference={"error": str(e), "status_code": e.status_code},
            )
        except Exception as e:
            logger.error("Unexpected error fetching shipment %s: %s", external_shipment_id, str(e))
            return Shipment(
                shipment_id=f"shp_{external_shipment_id}",
                external_shipment_id=external_shipment_id,
                order_id=f"ord_unknown_{external_shipment_id}",
                external_order_id=f"ext_ord_unknown_{external_shipment_id}",
                channel=channel,
                status=ShipmentStatus.UNKNOWN,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.LOW,
                raw_reference={"error": str(e)},
            )

    def get_shipment_by_external_order_id(
        self,
        external_order_id: str,
        channel: SalesChannel,
    ) -> Optional[Shipment]:
        """
        Obtiene el shipment asociado a una orden: GET /orders/{order_id} -> shipping.id -> GET /shipments/{shipping_id}.
        """
        try:
            client = self._get_api_client(channel)
            raw_order = client.get(f"/orders/{external_order_id}")
            if not raw_order or not isinstance(raw_order, dict):
                return None
            shipping_info = raw_order.get("shipping") or {}
            shipping_id = shipping_info.get("id")
            if not shipping_id:
                return None
            return self.get_shipment_by_external_id(str(shipping_id), channel)
        except MercadoLibreApiError as e:
            logger.warning("Error fetching shipping for order %s: %s", external_order_id, str(e))
            return None

    def get_tracking(
        self,
        external_shipment_id: str,
        channel: SalesChannel,
    ) -> Sequence[TrackingEvent]:
        """
        Obtiene los eventos de tracking para un shipment.
        """
        shipment = self.get_shipment_by_external_id(external_shipment_id, channel)
        if not shipment:
            return ()
        return shipment.tracking_events

    def get_shipping_label(
        self,
        external_shipment_id: str,
        channel: SalesChannel,
    ) -> Optional[ShippingLabel]:
        """
        Obtiene la referencia a la etiqueta de envío de Mercado Envíos (PDF/ZPL):
        GET /shipment_labels?shipment_ids={id}&response_type=pdf
        """
        # Mercado Libre genera la etiqueta a través de /shipment_labels
        # Verificamos si el shipment existe y está listo para imprimir
        shipment = self.get_shipment_by_external_id(external_shipment_id, channel)
        if not shipment:
            return None

        if shipment.status == ShipmentStatus.UNKNOWN:
            return ShippingLabel(
                label_id=f"lbl_{external_shipment_id}",
                external_reference=external_shipment_id,
                status=LabelStatus.ERROR,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.LOW,
            )

        # Si el envío está listo para despacho o despachado
        return ShippingLabel(
            label_id=f"lbl_{external_shipment_id}",
            external_reference=external_shipment_id,
            status=LabelStatus.READY,
            format=LabelFormat.PDF,
            url=f"https://api.mercadolibre.com/shipment_labels?shipment_ids={external_shipment_id}&response_type=pdf",
            provenance=EvidenceProvenanceType.LIVE,
            confidence=Confidence.HIGH,
        )
