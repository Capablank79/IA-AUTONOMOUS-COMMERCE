import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List
from types import MappingProxyType

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import (
    SupplierStatus,
    EvidenceProvenanceType,
    SupplierReadiness,
)
from src.domain.supplier_memory.models import SupplierMemoryRecord
from src.domain.supplier_memory.ports import SupplierMemoryRepository


class JsonSupplierMemoryRepositoryError(Exception):
    """Excepción base para errores de JsonSupplierMemoryRepository."""
    pass


class InvalidSupplierMemoryDataError(JsonSupplierMemoryRepositoryError):
    """Se lanza cuando los datos de proveedor leídos están corruptos o son inválidos."""
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


def _decode_supplier_memory_record(data: Dict[str, Any]) -> SupplierMemoryRecord:
    try:
        observed_at = datetime.fromisoformat(data["observed_at"]) if isinstance(data.get("observed_at"), str) else datetime.now(timezone.utc)
        updated_at = datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else datetime.now(timezone.utc)
        provenance = EvidenceProvenanceType(data.get("provenance", EvidenceProvenanceType.DERIVED.value))
        confidence = Confidence(data.get("confidence", Confidence.MEDIUM.value))
        status = SupplierStatus(data.get("status", SupplierStatus.RESEARCH.value))
        verification_status = SupplierReadiness(data.get("verification_status", SupplierReadiness.DISCOVERED.value))

        cost_amount = Decimal(str(data["cost_amount"])) if data.get("cost_amount") is not None else None

        return SupplierMemoryRecord(
            supplier_memory_id=data["supplier_memory_id"],
            supplier_id=data["supplier_id"],
            name=data.get("name", ""),
            status=status,
            sku=data.get("sku"),
            cost_amount=cost_amount,
            cost_currency=data.get("cost_currency", "CLP"),
            moq=data.get("moq"),
            lead_time_days=data.get("lead_time_days"),
            source=data.get("source", "SUPPLIER_DIRECTORY"),
            evidence_reference=data.get("evidence_reference"),
            verification_status=verification_status,
            confidence=confidence,
            provenance=provenance,
            observed_at=observed_at,
            updated_at=updated_at,
            metadata=data.get("metadata", {}),
        )
    except Exception as e:
        raise InvalidSupplierMemoryDataError(f"Failed to decode SupplierMemoryRecord: {e}") from e


class JsonSupplierMemoryRepository(SupplierMemoryRepository):
    """
    Implementación JSON durable del puerto SupplierMemoryRepository.
    Garantiza sanitización de credenciales y escritura atómica en disco.
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
            raise InvalidSupplierMemoryDataError(f"Corrupted JSON file at {self.file_path}: {e}") from e
        except Exception as e:
            raise JsonSupplierMemoryRepositoryError(f"Error reading file {self.file_path}: {e}") from e

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
            raise JsonSupplierMemoryRepositoryError(f"Failed to save supplier memory to {self.file_path}: {e}") from e

    def save(self, record: SupplierMemoryRecord) -> None:
        records = self._load_all()
        raw_dict = {
            "supplier_memory_id": record.supplier_memory_id,
            "supplier_id": record.supplier_id,
            "name": record.name,
            "status": record.status,
            "sku": record.sku,
            "cost_amount": record.cost_amount,
            "cost_currency": record.cost_currency,
            "moq": record.moq,
            "lead_time_days": record.lead_time_days,
            "source": record.source,
            "evidence_reference": record.evidence_reference,
            "verification_status": record.verification_status,
            "confidence": record.confidence,
            "provenance": record.provenance,
            "observed_at": record.observed_at,
            "updated_at": record.updated_at,
            "metadata": record.metadata,
        }
        encoded = _encode_json_value(raw_dict)
        records[record.supplier_memory_id] = encoded
        self._save_all(records)

    def get_by_id(self, supplier_memory_id: str) -> Optional[SupplierMemoryRecord]:
        records = self._load_all()
        data = records.get(supplier_memory_id)
        if not data:
            return None
        return _decode_supplier_memory_record(data)

    def get_by_supplier_id(self, supplier_id: str) -> List[SupplierMemoryRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("supplier_id") == supplier_id:
                results.append(_decode_supplier_memory_record(data))
        return results

    def get_by_sku(self, sku: str) -> List[SupplierMemoryRecord]:
        records = self._load_all()
        results = []
        for data in records.values():
            if data.get("sku") == sku:
                results.append(_decode_supplier_memory_record(data))
        return results

    def exists(self, supplier_memory_id: str) -> bool:
        records = self._load_all()
        return supplier_memory_id in records
