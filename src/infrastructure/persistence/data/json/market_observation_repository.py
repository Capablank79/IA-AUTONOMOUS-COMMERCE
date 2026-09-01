import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_monitoring.models import (
    MarketObservation,
    NormalizedPrice,
    ObservedSellerInfo,
    ObservedCompetitionInfo,
    ObservationSourceType,
    ObservationStatus,
)
from src.domain.market_monitoring.ports import MarketObservationRepository
from src.domain.market_intelligence.models import Marketplace, Confidence, SignalType


class JsonMarketObservationRepositoryError(Exception):
    """Excepción base para errores en el repositorio de observaciones."""
    pass


class CorruptedMarketObservationDataError(JsonMarketObservationRepositoryError):
    """Se lanza cuando los datos leídos de una observación están corruptos."""
    pass


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
}


def _encode_json_value(val: Any) -> Any:
    """Serializa estructuras de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                continue
            cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_market_observation(data: Dict[str, Any]) -> MarketObservation:
    """Reconstruye una instancia de MarketObservation a partir de un dict JSON."""
    try:
        obs_id = data["observation_id"]
        source = data["source"]
        source_type = ObservationSourceType(data.get("source_type", ObservationSourceType.MARKETPLACE_API.value))

        observed_at = datetime.fromisoformat(data["observed_at"])
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)

        collected_at = datetime.fromisoformat(data["collected_at"])
        if collected_at.tzinfo is None:
            collected_at = collected_at.replace(tzinfo=timezone.utc)

        marketplace = Marketplace(data.get("marketplace", Marketplace.GENERIC.value))
        entity_id = data["entity_id"]
        status = ObservationStatus(data.get("status", ObservationStatus.SUCCESS.value))

        product_sku = data.get("product_sku")
        category = data.get("category")
        title = data.get("title")

        # Precio normalizado
        price = None
        if data.get("price"):
            price_data = data["price"]
            price = NormalizedPrice(
                amount=Decimal(str(price_data["amount"])),
                currency=price_data["currency"]
            )

        availability = data.get("availability")
        stock = data.get("stock")
        sold_quantity = data.get("sold_quantity")

        # Seller info
        seller_info = None
        if data.get("seller_info"):
            s_data = data["seller_info"]
            seller_info = ObservedSellerInfo(
                seller_id=s_data.get("seller_id"),
                seller_name=s_data.get("seller_name"),
                reputation_level=s_data.get("reputation_level"),
                is_official_store=s_data.get("is_official_store"),
                raw_seller_data=s_data.get("raw_seller_data", {}),
            )

        # Competition info
        competition_info = None
        if data.get("competition_info"):
            c_data = data["competition_info"]

            bb_price = None
            if c_data.get("buy_box_winner_price"):
                bb_price = NormalizedPrice(
                    amount=Decimal(str(c_data["buy_box_winner_price"]["amount"])),
                    currency=c_data["buy_box_winner_price"]["currency"],
                )

            low_price = None
            if c_data.get("lowest_competitor_price"):
                low_price = NormalizedPrice(
                    amount=Decimal(str(c_data["lowest_competitor_price"]["amount"])),
                    currency=c_data["lowest_competitor_price"]["currency"],
                )

            competition_info = ObservedCompetitionInfo(
                total_competitors=c_data.get("total_competitors"),
                buy_box_winner_price=bb_price,
                lowest_competitor_price=low_price,
                has_buy_box=c_data.get("has_buy_box"),
            )

        provenance = data.get("provenance", "LIVE")
        confidence = Confidence(data.get("confidence", Confidence.HIGH.value))
        signal_type = SignalType(data.get("signal_type", SignalType.OBSERVED.value))
        correlation_id = data.get("correlation_id", "default-correlation")
        idempotency_key = data.get("idempotency_key", "")
        error_message = data.get("error_message")
        raw_payload = data.get("raw_payload", {})
        metadata = data.get("metadata", {})

        return MarketObservation(
            observation_id=obs_id,
            source=source,
            source_type=source_type,
            observed_at=observed_at,
            collected_at=collected_at,
            marketplace=marketplace,
            entity_id=entity_id,
            status=status,
            product_sku=product_sku,
            category=category,
            title=title,
            price=price,
            availability=availability,
            stock=stock,
            sold_quantity=sold_quantity,
            seller_info=seller_info,
            competition_info=competition_info,
            provenance=provenance,
            confidence=confidence,
            signal_type=signal_type,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            error_message=error_message,
            raw_payload=raw_payload,
            metadata=metadata,
        )
    except Exception as e:
        raise CorruptedMarketObservationDataError(f"Failed to decode MarketObservation: {e}") from e


class JsonMarketObservationRepository(MarketObservationRepository):
    """
    Implementación JSON durable y atómica del puerto MarketObservationRepository.
    - Soporta escrituras atómicas con '.tmp' y 'os.replace'.
    - Garantiza idempotencia mediante índice de 'idempotency_key'.
    - Soporta reinicio y recarga completa sin duplicación.
    - Sanitiza recursivamente datos sensibles.
    """

    def __init__(self, storage_dir: Union[str, Path]):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._index_file = self.storage_dir / "observations_index.json"
        self._idempotency_index: Dict[str, str] = {}  # idempotency_key -> observation_id
        self._load_index()

    def _load_index(self) -> None:
        if self._index_file.exists():
            try:
                with open(self._index_file, "r", encoding="utf-8") as f:
                    self._idempotency_index = json.load(f)
            except Exception:
                self._idempotency_index = {}
        else:
            self._idempotency_index = {}

    def _save_index(self) -> None:
        tmp_file = self.storage_dir / f"observations_index.json.tmp.{os.getpid()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(self._idempotency_index, f, indent=2)
        os.replace(tmp_file, self._index_file)

    def save(self, observation: MarketObservation) -> None:
        # Idempotencia: si ya existe por idempotency_key, no sobreescribir ni duplicar
        if observation.idempotency_key in self._idempotency_index:
            return

        file_path = self.storage_dir / f"{observation.observation_id}.json"
        tmp_path = self.storage_dir / f"{observation.observation_id}.json.tmp.{os.getpid()}"

        data = {
            "observation_id": observation.observation_id,
            "source": observation.source,
            "source_type": observation.source_type.value,
            "observed_at": observation.observed_at.isoformat(),
            "collected_at": observation.collected_at.isoformat(),
            "marketplace": observation.marketplace.value,
            "entity_id": observation.entity_id,
            "status": observation.status.value,
            "product_sku": observation.product_sku,
            "category": observation.category,
            "title": observation.title,
            "price": {
                "amount": str(observation.price.amount),
                "currency": observation.price.currency,
            } if observation.price else None,
            "availability": observation.availability,
            "stock": observation.stock,
            "sold_quantity": observation.sold_quantity,
            "seller_info": {
                "seller_id": observation.seller_info.seller_id,
                "seller_name": observation.seller_info.seller_name,
                "reputation_level": observation.seller_info.reputation_level,
                "is_official_store": observation.seller_info.is_official_store,
                "raw_seller_data": _encode_json_value(observation.seller_info.raw_seller_data),
            } if observation.seller_info else None,
            "competition_info": {
                "total_competitors": observation.competition_info.total_competitors,
                "buy_box_winner_price": {
                    "amount": str(observation.competition_info.buy_box_winner_price.amount),
                    "currency": observation.competition_info.buy_box_winner_price.currency,
                } if observation.competition_info.buy_box_winner_price else None,
                "lowest_competitor_price": {
                    "amount": str(observation.competition_info.lowest_competitor_price.amount),
                    "currency": observation.competition_info.lowest_competitor_price.currency,
                } if observation.competition_info.lowest_competitor_price else None,
                "has_buy_box": observation.competition_info.has_buy_box,
            } if observation.competition_info else None,
            "provenance": observation.provenance,
            "confidence": observation.confidence.value,
            "signal_type": observation.signal_type.value,
            "correlation_id": observation.correlation_id,
            "idempotency_key": observation.idempotency_key,
            "error_message": observation.error_message,
            "raw_payload": _encode_json_value(observation.raw_payload),
            "metadata": _encode_json_value(observation.metadata),
        }

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        os.replace(tmp_path, file_path)

        self._idempotency_index[observation.idempotency_key] = observation.observation_id
        self._save_index()

    def save_all(self, observations: List[MarketObservation]) -> int:
        saved_count = 0
        for obs in observations:
            if obs.idempotency_key not in self._idempotency_index:
                self.save(obs)
                saved_count += 1
        return saved_count

    def get_by_id(self, observation_id: str) -> Optional[MarketObservation]:
        file_path = self.storage_dir / f"{observation_id}.json"
        if not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _decode_market_observation(data)
        except Exception as e:
            raise CorruptedMarketObservationDataError(f"Error loading observation {observation_id}: {e}") from e

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[MarketObservation]:
        obs_id = self._idempotency_index.get(idempotency_key)
        if not obs_id:
            # Búsqueda de rescate en archivos si el índice no lo tuviera
            for file_path in self.storage_dir.glob("*.json"):
                if file_path.name == "observations_index.json":
                    continue
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("idempotency_key") == idempotency_key:
                        self._idempotency_index[idempotency_key] = data["observation_id"]
                        return _decode_market_observation(data)
                except Exception:
                    continue
            return None
        return self.get_by_id(obs_id)

    def list_by_entity(self, entity_id: str, limit: int = 100) -> List[MarketObservation]:
        results: List[MarketObservation] = []
        for file_path in self.storage_dir.glob("*.json"):
            if file_path.name == "observations_index.json":
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("entity_id") == entity_id:
                    results.append(_decode_market_observation(data))
            except Exception:
                continue

        results.sort(key=lambda o: o.observed_at)
        return results[:limit]

    def list_all(self, limit: int = 1000) -> List[MarketObservation]:
        results: List[MarketObservation] = []
        for file_path in self.storage_dir.glob("*.json"):
            if file_path.name == "observations_index.json":
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append(_decode_market_observation(data))
            except Exception:
                continue

        results.sort(key=lambda o: o.observed_at, reverse=True)
        return results[:limit]
