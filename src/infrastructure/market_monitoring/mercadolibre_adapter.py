import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Any, Dict
from urllib.parse import urlencode

from src.domain.market_monitoring.models import (
    MarketObservation,
    ObservationStatus,
    ObservationSourceType,
    NormalizedPrice,
    ObservedSellerInfo,
    ObservedCompetitionInfo,
)
from src.domain.market_monitoring.ports import MarketObservationSourcePort
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient


class MercadoLibreObservationAdapter(MarketObservationSourcePort):
    """
    Adaptador de fuente de mercado para Mercado Libre (Hito J.2).
    Reutiliza MercadoLibreApiClient sin duplicar lógica de autenticación ni llamadas HTTP.
    Implementa el puerto MarketObservationSourcePort con manejo seguro de caídas (timeouts/500),
    preservando estados UNKNOWN sin fabricar datos falsos.
    """

    SITE_ID = "MLC"

    def __init__(self, api_client: MercadoLibreApiClient, source_name: str = "MERCADOLIBRE_LIVE"):
        self.api_client = api_client
        self._source_name = source_name

    @property
    def source_name(self) -> str:
        return self._source_name

    def observe(
        self,
        query: Optional[str] = None,
        entity_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 50,
        correlation_id: Optional[str] = None,
    ) -> List[MarketObservation]:
        corr_id = correlation_id or f"corr-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        results: List[MarketObservation] = []

        # 1. Observación directa de un item / producto puntual por entity_id
        if entity_id:
            try:
                item_data = self.api_client.get(f"/items/{entity_id}")
                obs = self._map_item_to_observation(item_data, corr_id, now)
                results.append(obs)
                return results
            except Exception as e:
                err_msg = str(e)
                status = ObservationStatus.TIMEOUT if "timeout" in err_msg.lower() else ObservationStatus.SOURCE_FAILURE
                obs_fail = MarketObservation(
                    observation_id=f"obs-ml-{uuid.uuid4().hex[:12]}",
                    source=self.source_name,
                    source_type=ObservationSourceType.MARKETPLACE_API,
                    observed_at=now,
                    collected_at=now,
                    marketplace=Marketplace.MERCADO_LIBRE,
                    entity_id=entity_id,
                    status=status,
                    provenance="LIVE",
                    confidence=Confidence.UNKNOWN,
                    signal_type=SignalType.OBSERVED,
                    correlation_id=corr_id,
                    error_message=f"Failed to fetch item {entity_id}: {err_msg}",
                )
                results.append(obs_fail)
                return results

        # 2. Observación por query / categoría utilizando /products/search o /sites/{site_id}/search
        if query or category:
            params: Dict[str, Any] = {
                "limit": limit,
                "site_id": self.SITE_ID,
            }
            if query:
                params["q"] = query
            if category:
                params["category"] = category

            query_str = urlencode(params)
            try:
                data = self.api_client.get(f"/products/search?{query_str}")
                items = data.get("results", [])
                for it in items:
                    try:
                        obs = self._map_search_result_to_observation(it, corr_id, now, category)
                        results.append(obs)
                    except Exception:
                        continue
            except Exception as e:
                err_msg = str(e)
                status = ObservationStatus.TIMEOUT if "timeout" in err_msg.lower() else ObservationStatus.SOURCE_FAILURE
                obs_fail = MarketObservation(
                    observation_id=f"obs-ml-{uuid.uuid4().hex[:12]}",
                    source=self.source_name,
                    source_type=ObservationSourceType.MARKETPLACE_API,
                    observed_at=now,
                    collected_at=now,
                    marketplace=Marketplace.MERCADO_LIBRE,
                    entity_id=query or category or "UNKNOWN_QUERY",
                    status=status,
                    provenance="LIVE",
                    confidence=Confidence.UNKNOWN,
                    signal_type=SignalType.OBSERVED,
                    correlation_id=corr_id,
                    error_message=f"Failed to search market with params {params}: {err_msg}",
                )
                results.append(obs_fail)

        return results

    def _map_item_to_observation(self, data: Dict[str, Any], corr_id: str, now: datetime) -> MarketObservation:
        entity_id = data.get("id", "UNKNOWN_ITEM")
        title = data.get("title")
        category_id = data.get("category_id")

        price = None
        if data.get("price") is not None:
            raw_amt = data.get("price")
            currency = data.get("currency_id", "CLP")
            price = NormalizedPrice(amount=Decimal(str(raw_amt)), currency=currency)

        stock = data.get("available_quantity")
        sold_qty = data.get("sold_quantity")
        status_str = data.get("status", "active")
        availability = "IN_STOCK" if (stock and stock > 0) else ("OUT_OF_STOCK" if stock == 0 else "UNKNOWN")

        seller_id = str(data.get("seller_id", "")) if data.get("seller_id") else None
        seller_info = None
        if seller_id:
            seller_info = ObservedSellerInfo(
                seller_id=seller_id,
                seller_name=None,
                reputation_level=None,
                is_official_store=data.get("official_store_id") is not None,
                raw_seller_data={"seller_id": seller_id},
            )

        return MarketObservation(
            observation_id=f"obs-ml-{uuid.uuid4().hex[:12]}",
            source=self.source_name,
            source_type=ObservationSourceType.MARKETPLACE_API,
            observed_at=now,
            collected_at=now,
            marketplace=Marketplace.MERCADO_LIBRE,
            entity_id=entity_id,
            status=ObservationStatus.SUCCESS,
            category=category_id,
            title=title,
            price=price,
            availability=availability,
            stock=stock,
            sold_quantity=sold_qty,
            seller_info=seller_info,
            provenance="LIVE",
            confidence=Confidence.HIGH,
            signal_type=SignalType.OBSERVED,
            correlation_id=corr_id,
            raw_payload=data,
        )

    def _map_search_result_to_observation(
        self,
        item: Dict[str, Any],
        corr_id: str,
        now: datetime,
        default_category: Optional[str]
    ) -> MarketObservation:
        entity_id = item.get("id", "UNKNOWN_PROD")
        title = item.get("name") or item.get("title")
        domain_id = item.get("domain_id") or default_category

        winner = item.get("buy_box_winner")
        price = None
        stock = None
        sold_qty = item.get("sold_quantity")
        seller_info = None
        competition_info = None

        if winner:
            raw_price = winner.get("price")
            currency = winner.get("currency_id", "CLP")
            if raw_price is not None:
                price = NormalizedPrice(amount=Decimal(str(raw_price)), currency=currency)
            stock = winner.get("available_quantity")
            if winner.get("sold_quantity") is not None:
                sold_qty = winner.get("sold_quantity")
            seller_id = str(winner.get("seller_id", "")) if winner.get("seller_id") else None
            if seller_id:
                seller_info = ObservedSellerInfo(
                    seller_id=seller_id,
                    seller_name=None,
                    raw_seller_data={"seller_id": seller_id},
                )
            competition_info = ObservedCompetitionInfo(
                buy_box_winner_price=price,
                has_buy_box=True,
            )

        availability = "IN_STOCK" if (stock and stock > 0) else ("OUT_OF_STOCK" if stock == 0 else "UNKNOWN")

        return MarketObservation(
            observation_id=f"obs-ml-{uuid.uuid4().hex[:12]}",
            source=self.source_name,
            source_type=ObservationSourceType.MARKETPLACE_API,
            observed_at=now,
            collected_at=now,
            marketplace=Marketplace.MERCADO_LIBRE,
            entity_id=entity_id,
            status=ObservationStatus.SUCCESS,
            category=domain_id,
            title=title,
            price=price,
            availability=availability,
            stock=stock,
            sold_quantity=sold_qty,
            seller_info=seller_info,
            competition_info=competition_info,
            provenance="LIVE",
            confidence=Confidence.HIGH,
            signal_type=SignalType.OBSERVED,
            correlation_id=corr_id,
            raw_payload=item,
        )
