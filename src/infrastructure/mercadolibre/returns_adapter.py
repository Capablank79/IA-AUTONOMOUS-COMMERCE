import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlencode

from src.application.oauth.connection_service import OAuthConnectionService
from src.domain.market_intelligence.models import Confidence
from src.domain.publication.models import SalesChannel
from src.domain.returns.models import (
    Claim,
    ClaimStage,
    ClaimStatus,
    RefundDetail,
    RefundStatus,
    Return,
    ReturnError,
    ReturnErrorCategory,
    ReturnEvent,
    ReturnQueryResult,
    ReturnReason,
    ReturnResolution,
    ReturnStatus,
)
from src.domain.returns.ports import ReturnsPort
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)

logger = logging.getLogger(__name__)


class MercadoLibreReturnsAdapter(ReturnsPort):
    """
    Adaptador de infraestructura de Mercado Libre para Devoluciones, Reclamos y Excepciones Postventa (G.8).
    
    Principios de Diseño:
    - Normalización de estados de devolución de Mercado Libre (opened, shipping, delivered, closed, cancelled).
    - Normalización de reclamos postventa (/post-purchase/v1/claims o /claims/{id}).
    - Manejo determinista de incertidumbre (UNKNOWN) ante fallos de red 5xx, timeouts o datos incompletos.
    - Manejo seguro de reembolsos: si la API no permite refund directo sin mediación, retorna NOT_SUPPORTED / FAILED.
    - Cero persistencia o fuga de PII ni secretos financieros.
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

        return MercadoLibreApiClient(access_token="")

    @staticmethod
    def normalize_return_status(status_str: Optional[str]) -> ReturnStatus:
        if not status_str:
            return ReturnStatus.UNKNOWN
        s = status_str.strip().lower()
        if s in ("opened", "requested", "pending"):
            return ReturnStatus.REQUESTED
        elif s in ("approved", "accepted"):
            return ReturnStatus.APPROVED
        elif s in ("shipped", "shipping", "in_transit"):
            return ReturnStatus.IN_TRANSIT
        elif s in ("delivered", "received"):
            return ReturnStatus.RECEIVED
        elif s in ("inspecting", "in_review"):
            return ReturnStatus.INSPECTING
        elif s in ("closed", "resolved", "completed", "refunded"):
            return ReturnStatus.RESOLVED
        elif s in ("rejected", "denied"):
            return ReturnStatus.REJECTED
        elif s in ("cancelled", "canceled"):
            return ReturnStatus.CANCELLED
        return ReturnStatus.UNKNOWN

    @staticmethod
    def normalize_claim_status(status_str: Optional[str]) -> ClaimStatus:
        if not status_str:
            return ClaimStatus.UNKNOWN
        s = status_str.strip().lower()
        if s in ("opened", "new"):
            return ClaimStatus.OPENED
        elif s in ("in_review", "investigating"):
            return ClaimStatus.IN_REVIEW
        elif s in ("waiting_for_buyer", "waiting_buyer"):
            return ClaimStatus.WAITING_BUYER
        elif s in ("waiting_for_seller", "waiting_seller"):
            return ClaimStatus.WAITING_SELLER
        elif s in ("mediation", "dispute"):
            return ClaimStatus.MEDIATION
        elif s in ("closed", "finished", "resolved"):
            return ClaimStatus.CLOSED
        elif s in ("cancelled", "canceled"):
            return ClaimStatus.CANCELLED
        return ClaimStatus.UNKNOWN

    @staticmethod
    def normalize_return_reason(reason_str: Optional[str]) -> ReturnReason:
        if not reason_str:
            return ReturnReason.UNKNOWN
        r = reason_str.strip().lower()
        if "damage" in r or "broken" in r:
            return ReturnReason.DAMAGED
        elif "defect" in r or "faulty" in r or "not_working" in r:
            return ReturnReason.DEFECTIVE
        elif "not_as_described" in r or "different" in r:
            return ReturnReason.NOT_AS_DESCRIBED
        elif "wrong" in r or "mistake" in r:
            return ReturnReason.WRONG_ITEM
        elif "regret" in r or "changed_mind" in r:
            return ReturnReason.CHANGED_MIND
        elif "delivery" in r or "late" in r:
            return ReturnReason.DELIVERY_ISSUE
        elif "missing" in r or "incomplete" in r:
            return ReturnReason.MISSING_PARTS
        return ReturnReason.OTHER

    def fetch_returns(
        self,
        channel: SalesChannel,
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ReturnQueryResult:
        client = self._get_api_client(channel)
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if date_from:
            params["date_from"] = date_from.isoformat()
        if date_to:
            params["date_to"] = date_to.isoformat()

        query_string = urlencode(params)
        path = f"/post-purchase/v1/returns/search?{query_string}"

        try:
            response = client.get(path)
            results = response.get("results", [])
            total = response.get("paging", {}).get("total", len(results))

            domain_returns = []
            for r_data in results:
                ext_ret_id = str(r_data.get("id", ""))
                ext_ord_id = str(r_data.get("order_id", ""))
                st = self.normalize_return_status(r_data.get("status"))
                rs = self.normalize_return_reason(r_data.get("reason_id") or r_data.get("reason"))

                ret_obj = Return(
                    return_id=f"ret_{ext_ret_id}",
                    external_return_id=ext_ret_id,
                    order_id=f"ord_{ext_ord_id}",
                    external_order_id=ext_ord_id,
                    channel=channel,
                    status=st,
                    reason=rs,
                    shipment_id=None,
                    external_shipment_id=str(r_data.get("shipment_id", "")) if r_data.get("shipment_id") else None,
                    provenance=EvidenceProvenanceType.LIVE,
                    confidence=Confidence.HIGH,
                )
                domain_returns.append(ret_obj)

            return ReturnQueryResult(
                returns=domain_returns,
                total_count=total,
                channel=channel,
                is_unknown=False,
            )

        except MercadoLibreApiError as exc:
            logger.error("Mercado Libre API error fetching returns: %s", exc)
            category = ReturnErrorCategory.EXTERNAL_SERVICE
            if exc.status_code == 404:
                category = ReturnErrorCategory.NOT_FOUND
            elif exc.status_code == 429:
                category = ReturnErrorCategory.RATE_LIMIT
            elif exc.status_code and 400 <= exc.status_code < 500:
                category = ReturnErrorCategory.VALIDATION

            return ReturnQueryResult(
                returns=(),
                total_count=0,
                channel=channel,
                errors=(
                    ReturnError(
                        category=category,
                        message=str(exc),
                        code=str(exc.status_code) if exc.status_code else None,
                        retryable=(exc.status_code in (429, 500, 502, 503, 504)),
                    ),
                ),
                is_unknown=True,
            )
        except Exception as exc:
            logger.exception("Unexpected error fetching returns from Mercado Libre")
            return ReturnQueryResult(
                returns=(),
                total_count=0,
                channel=channel,
                errors=(
                    ReturnError(
                        category=ReturnErrorCategory.UNKNOWN,
                        message=f"Unexpected error: {str(exc)}",
                        retryable=False,
                    ),
                ),
                is_unknown=True,
            )

    def get_return_by_external_id(
        self,
        external_return_id: str,
        channel: SalesChannel,
    ) -> ReturnQueryResult:
        client = self._get_api_client(channel)
        path = f"/post-purchase/v1/returns/{external_return_id}"

        try:
            r_data = client.get(path)
            ext_ret_id = str(r_data.get("id", external_return_id))
            ext_ord_id = str(r_data.get("order_id", ""))
            st = self.normalize_return_status(r_data.get("status"))
            rs = self.normalize_return_reason(r_data.get("reason_id") or r_data.get("reason"))

            # Reembolso anidado si existe
            refund_obj = None
            refund_data = r_data.get("refund")
            if refund_data and isinstance(refund_data, dict):
                ref_st = RefundStatus.CONFIRMED if refund_data.get("status") == "approved" else RefundStatus.PROCESSING
                refund_obj = RefundDetail(
                    refund_id=f"ref_{ext_ret_id}",
                    external_refund_id=str(refund_data.get("id", "")),
                    status=ref_st,
                    amount=Decimal(str(refund_data.get("amount", "0.00"))),
                    currency=str(refund_data.get("currency_id", "USD")),
                    provenance=EvidenceProvenanceType.LIVE,
                    confidence=Confidence.HIGH,
                )

            # Resolución
            res_str = (r_data.get("resolution") or "").strip().lower()
            res_enum = ReturnResolution.UNKNOWN
            if res_str == "refund":
                res_enum = ReturnResolution.REFUND
            elif res_str == "replacement":
                res_enum = ReturnResolution.REPLACEMENT
            elif res_str == "return_only":
                res_enum = ReturnResolution.RETURN_ONLY
            elif res_str == "rejected":
                res_enum = ReturnResolution.REJECTED
            elif refund_obj:
                res_enum = ReturnResolution.REFUND

            ret_obj = Return(
                return_id=f"ret_{ext_ret_id}",
                external_return_id=ext_ret_id,
                order_id=f"ord_{ext_ord_id}" if ext_ord_id else "",
                external_order_id=ext_ord_id,
                channel=channel,
                status=st,
                reason=rs,
                resolution=res_enum,
                shipment_id=None,
                external_shipment_id=str(r_data.get("shipment_id", "")) if r_data.get("shipment_id") else None,
                refund=refund_obj,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.HIGH,
            )

            return ReturnQueryResult(
                returns=(ret_obj,),
                total_count=1,
                channel=channel,
                is_unknown=False,
            )
        except MercadoLibreApiError as exc:
            logger.error("Mercado Libre API error getting return %s: %s", external_return_id, exc)
            category = ReturnErrorCategory.EXTERNAL_SERVICE
            if exc.status_code == 404:
                category = ReturnErrorCategory.NOT_FOUND
            elif exc.status_code == 429:
                category = ReturnErrorCategory.RATE_LIMIT

            return ReturnQueryResult(
                returns=(),
                total_count=0,
                channel=channel,
                errors=(
                    ReturnError(
                        category=category,
                        message=str(exc),
                        code=str(exc.status_code) if exc.status_code else None,
                        retryable=(exc.status_code in (429, 500, 502, 503, 504)),
                    ),
                ),
                is_unknown=True,
            )
        except Exception as exc:
            logger.exception("Unexpected error getting return from Mercado Libre")
            return ReturnQueryResult(
                returns=(),
                total_count=0,
                channel=channel,
                errors=(
                    ReturnError(
                        category=ReturnErrorCategory.UNKNOWN,
                        message=f"Unexpected error: {str(exc)}",
                        retryable=False,
                    ),
                ),
                is_unknown=True,
            )

    def get_return_by_external_order_id(
        self,
        external_order_id: str,
        channel: SalesChannel,
    ) -> ReturnQueryResult:
        client = self._get_api_client(channel)
        path = f"/post-purchase/v1/returns/search?order_id={external_order_id}"

        try:
            response = client.get(path)
            results = response.get("results", [])
            if not results:
                return ReturnQueryResult(returns=(), total_count=0, channel=channel)

            return self.get_return_by_external_id(str(results[0].get("id")), channel)
        except Exception as exc:
            logger.warning("Failed to query return for order %s: %s", external_order_id, exc)
            return ReturnQueryResult(
                returns=(),
                total_count=0,
                channel=channel,
                errors=(
                    ReturnError(
                        category=ReturnErrorCategory.EXTERNAL_SERVICE,
                        message=str(exc),
                    ),
                ),
                is_unknown=True,
            )

    def get_claim_by_external_id(
        self,
        external_claim_id: str,
        channel: SalesChannel,
    ) -> Optional[Claim]:
        client = self._get_api_client(channel)
        path = f"/post-purchase/v1/claims/{external_claim_id}"

        try:
            c_data = client.get(path)
            ext_ord_id = str(c_data.get("order_id", ""))
            st = self.normalize_claim_status(c_data.get("status"))
            stage_str = str(c_data.get("stage", "")).upper()
            stage = ClaimStage.DISPUTE if "DISPUTE" in stage_str else ClaimStage.CLAIM

            return Claim(
                claim_id=f"clm_{external_claim_id}",
                external_claim_id=external_claim_id,
                order_id=f"ord_{ext_ord_id}" if ext_ord_id else "",
                external_order_id=ext_ord_id,
                channel=channel,
                status=st,
                stage=stage,
                reason=self.normalize_return_reason(c_data.get("reason_id")),
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.HIGH,
            )
        except Exception as exc:
            logger.warning("Failed to query claim %s: %s", external_claim_id, exc)
            return None

    def create_return_request(
        self,
        external_order_id: str,
        channel: SalesChannel,
        reason: str,
        correlation_id: str,
        idempotency_key: str,
    ) -> ReturnQueryResult:
        """
        Mercado Libre normalmente requiere que el comprador inicie la devolución en la plataforma.
        Si la API rechaza o no expone creación directa de seller returns, maneja NOT_SUPPORTED o UNKNOWN.
        """
        client = self._get_api_client(channel)
        path = "/post-purchase/v1/returns"
        payload = {
            "order_id": int(external_order_id) if external_order_id.isdigit() else external_order_id,
            "reason": reason,
        }

        try:
            response = client.post(path, payload=payload)
            ext_ret_id = str(response.get("id", f"ret_{uuid.uuid4().hex[:8]}"))
            ret = Return(
                return_id=f"ret_{ext_ret_id}",
                external_return_id=ext_ret_id,
                order_id=f"ord_{external_order_id}",
                external_order_id=external_order_id,
                channel=channel,
                status=ReturnStatus.REQUESTED,
                reason=self.normalize_return_reason(reason),
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.HIGH,
            )
            return ReturnQueryResult(returns=(ret,), total_count=1, channel=channel)
        except MercadoLibreApiError as exc:
            # Documentar la limitación o mapear a NOT_SUPPORTED si 403 / 405 / 404
            logger.info("Mercado Libre create_return_request unsupported or failed: %s", exc)
            return ReturnQueryResult(
                returns=(),
                total_count=0,
                channel=channel,
                errors=(
                    ReturnError(
                        category=ReturnErrorCategory.NOT_SUPPORTED if exc.status_code in (403, 404, 405) else ReturnErrorCategory.EXTERNAL_SERVICE,
                        message=f"Return creation API returned HTTP {exc.status_code}: {exc}",
                        code=str(exc.status_code) if exc.status_code else None,
                    ),
                ),
                is_unknown=True,
            )

    def execute_refund(
        self,
        external_return_id: str,
        external_order_id: str,
        amount: Decimal,
        currency: str,
        channel: SalesChannel,
        correlation_id: str,
        idempotency_key: str,
    ) -> Optional[RefundDetail]:
        client = self._get_api_client(channel)
        path = f"/post-purchase/v1/returns/{external_return_id}/refund"
        payload = {
            "amount": float(amount),
            "currency_id": currency,
        }

        try:
            res = client.post(path, payload=payload)
            ref_id = str(res.get("id") or res.get("refund_id") or f"ref_{uuid.uuid4().hex[:8]}")
            st = RefundStatus.CONFIRMED if res.get("status") in ("approved", "completed", "refunded") else RefundStatus.PROCESSING
            return RefundDetail(
                refund_id=f"ref_{ref_id}",
                external_refund_id=ref_id,
                status=st,
                amount=amount,
                currency=currency,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.HIGH,
            )
        except MercadoLibreApiError as exc:
            logger.warning("Mercado Libre refund execution failed: %s", exc)
            return RefundDetail(
                refund_id=f"ref_fail_{uuid.uuid4().hex[:8]}",
                status=RefundStatus.FAILED if exc.status_code and exc.status_code < 500 else RefundStatus.UNKNOWN,
                amount=amount,
                currency=currency,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                provenance=EvidenceProvenanceType.LIVE,
                confidence=Confidence.LOW,
            )
