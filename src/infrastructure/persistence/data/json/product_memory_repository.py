import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.product_memory.models import ProductMemoryRecord
from src.domain.product_memory.ports import ProductMemoryRepository


class JsonProductMemoryRepositoryError(Exception):
    """Excepción base para errores de JsonProductMemoryRepository."""
    pass


class InvalidProductMemoryDataError(JsonProductMemoryRepositoryError):
    """Se lanza cuando los datos de producto leídos están corruptos o son inválidos."""
    pass


SENSITIVE_KEYS = {"password", "secret", "token", "api_key", "apikey", "pan", "cvv", "private_key", "credential"}


def _encode_json_value(val: Any) -> Any:
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                continue
            cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_product_memory_record(data: Dict[str, Any]) -> ProductMemoryRecord:
    try:
        observed_at = datetime.fromisoformat(data["observed_at"]) if isinstance(data.get("observed_at"), str) else datetime.now(timezone.utc)
        updated_at = datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else datetime.now(timezone.utc)
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))
        marketplace = Marketplace(data.get("marketplace", Marketplace.MERCADO_LIBRE.value))
        price_amount = Decimal(str(data.get("price_amount", "0")))

        return ProductMemoryRecord(
            product_memory_id=data["product_memory_id"],
            sku=data["sku"],
            external_id=data["external_id"],
            marketplace=marketplace,
            title=data.get("title", ""),
            category=data.get("category", ""),
            price_amount=price_amount,
            price_currency=data.get("price_currency", "CLP"),
            sold_quantity=data.get("sold_quantity"),
            available_quantity=data.get("available_quantity", 0),
            seller_id=data.get("seller_id", "UNKNOWN"),
            evidence_reference=data.get("evidence_reference"),
            observed_at=observed_at,
            updated_at=updated_at,
            confidence=confidence,
            provenance=provenance,
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidProductMemoryDataError(f"Failed to decode ProductMemoryRecord: {e}") from e


class JsonProductMemoryRepository(ProductMemoryRepository):
    """
    Implementación JSON durable del puerto ProductMemoryRepository.
    Sanitiza datos sensibles y ofrece escritura atómica en disco.
    """

    def __init__(self, file_path: Union[str, Path]):
        self.file_path = Path(file_path)

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except json.JSONDecodeError as e:
            raise InvalidProductMemoryDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonProductMemoryRepositoryError(f"Error reading file {self.file_path}: {e}") from e

    def _save_all(self, data: Dict[str, Dict[str, Any]]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.file_path)
        except Exception as e:
            if tmp_path.exists():
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise JsonProductMemoryRepositoryError(f"Failed to save product memory to {self.file_path}: {e}") from e

    def save(self, record: ProductMemoryRecord) -> None:
        records = self._load_all()
        raw_dict = {
            "product_memory_id": record.product_memory_id,
            "sku": record.sku,
            "external_id": record.external_id,
            "marketplace": record.marketplace,
            "title": record.title,
            "category": record.category,
            "price_amount": record.price_amount,
            "price_currency": record.price_currency,
            "sold_quantity": record.sold_quantity,
            "available_quantity": record.available_quantity,
            "seller_id": record.seller_id,
            "evidence_reference": record.evidence_reference,
            "observed_at": record.observed_at,
            "updated_at": record.updated_at,
            "confidence": record.confidence,
            "provenance": record.provenance,
            "metadata": record.metadata,
        }
        encoded = _encode_json_value(raw_dict)
        records[record.product_memory_id] = encoded
        self._save_all(records)

    def get_by_id(self, product_memory_id: str) -> Optional[ProductMemoryRecord]:
        records = self._load_all()
        data = records.get(product_memory_id)
        if not data:
            return None
        return _decode_product_memory_record(data)

    def get_by_sku(self, sku: str) -> Optional[ProductMemoryRecord]:
        records = self._load_all()
        for data in records.values():
            if data.get("sku") == sku:
                return _decode_product_memory_record(data)
        return None

    def get_by_external_id(self, external_id: str) -> List[ProductMemoryRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("external_id") == external_id:
                results.append(_decode_product_memory_record(data))
        return results

    def exists(self, product_memory_id: str) -> bool:
        records = self._load_all()
        return product_memory_id in records
