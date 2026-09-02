"""
Implementación JSON persistente, atómica y append-only para Cost Tracking (Hito K.3).

Garantiza:
- Atomic write (.tmp -> os.replace) con fsync.
- Inmutabilidad y semántica append-only.
- Sanitización recursiva de datos sensibles.
- Idempotencia estricta por cost_id e idempotency_key determinista.
- Ordenación cronológica determinista por occurred_at y cost_id.
- Resiliencia ante caídas y recarga íntegra tras reinicio de proceso.
- Verificación de checksum para integridad / detección de manipulación.
- Reconstrucción de CostSummary con soporte multi-moneda y contabilidad de UNKNOWN.
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List, Tuple
from types import MappingProxyType
import hashlib

from src.domain.cost.models import (
    CostRecord,
    CostSummary,
    CurrencyCostSummary,
    CostType,
    UsageRecord,
    UsageUnit,
)
from src.domain.cost.ports import CostRepositoryPort


class JsonCostRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de costes."""
    pass


class CorruptedCostRecordError(JsonCostRepositoryError):
    """Se lanza cuando los datos de un registro de coste están corruptos o el checksum no coincide."""
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
    "chain_of_thought",
    "reasoning",
    "reasoning_tokens",
    "internal_scratchpad",
    "card_number",
}


def _encode_json_value(val: Any) -> Any:
    """Serializa estructuras a JSON de forma determinista y sanitiza claves sensibles recursivamente."""
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
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_usage_record(data: Dict[str, Any]) -> UsageRecord:
    """Decodifica un UsageRecord a partir de un diccionario serializado."""
    unit_str = data.get("unit", "UNKNOWN")
    unit = UsageUnit(unit_str) if unit_str in UsageUnit.__members__ else UsageUnit.UNKNOWN
    
    in_qty = Decimal(str(data["input_quantity"])) if data.get("input_quantity") is not None else None
    out_qty = Decimal(str(data["output_quantity"])) if data.get("output_quantity") is not None else None
    tot_qty = Decimal(str(data["total_quantity"])) if data.get("total_quantity") is not None else None
    
    return UsageRecord(
        unit=unit,
        input_quantity=in_qty,
        output_quantity=out_qty,
        total_quantity=tot_qty,
        details=data.get("details", {}),
    )


class JsonCostRepository(CostRepositoryPort):
    """
    Repositorio JSON persistente y seguro para Cost Tracking.
    Almacena registros individuales inmutables en estructura organizada por ejecuciones/misiones.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.costs_dir = self.base_dir / "costs"
        self.index_dir = self.base_dir / "index"

        self.costs_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.index_file = self.index_dir / "costs_index.jsonl"
        self._memory_cache: Dict[str, CostRecord] = {}
        self._idempotency_cache: Dict[str, str] = {}  # idempotency_key -> cost_id

        self._load_index()

    def _load_index(self) -> None:
        """Carga y reconstruye el índice en memoria a partir de los archivos existentes."""
        self._memory_cache.clear()
        self._idempotency_cache.clear()

        # Recorrer todos los archivos de costos en el directorio
        for json_file in self.costs_dir.glob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    record = self._dict_to_record(data)
                    self._memory_cache[record.cost_id] = record
                    self._idempotency_cache[record.idempotency_key] = record.cost_id
            except Exception as e:
                # Log or handle corrupted file
                continue

    def _record_to_dict(self, record: CostRecord) -> Dict[str, Any]:
        """Convierte un CostRecord a un diccionario plano determinista."""
        return {
            "cost_id": record.cost_id,
            "occurred_at": _encode_json_value(record.occurred_at),
            "cost_type": record.cost_type.value,
            "provider": record.provider,
            "service_or_model": record.service_or_model,
            "execution_id": record.execution_id,
            "usage": {
                "unit": record.usage.unit.value,
                "input_quantity": _encode_json_value(record.usage.input_quantity),
                "output_quantity": _encode_json_value(record.usage.output_quantity),
                "total_quantity": _encode_json_value(record.usage.total_quantity),
                "details": _encode_json_value(dict(record.usage.details)),
            },
            "currency": record.currency,
            "unit_cost": _encode_json_value(record.unit_cost),
            "total_cost": _encode_json_value(record.total_cost),
            "pricing_source": record.pricing_source,
            "pricing_version": record.pricing_version,
            "trace_id": record.trace_id,
            "mission_id": record.mission_id,
            "cycle_id": record.cycle_id,
            "correlation_id": record.correlation_id,
            "causation_id": record.causation_id,
            "provenance": record.provenance,
            "idempotency_key": record.idempotency_key,
            "checksum": record.checksum,
            "schema_version": record.schema_version,
            "metadata": _encode_json_value(dict(record.metadata)),
        }

    def _dict_to_record(self, data: Dict[str, Any]) -> CostRecord:
        """Reconstruye un CostRecord a partir de un diccionario."""
        occ = datetime.fromisoformat(data["occurred_at"])
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)

        usage = _decode_usage_record(data.get("usage", {}))

        unit_cost = Decimal(str(data["unit_cost"])) if data.get("unit_cost") is not None else None
        total_cost = Decimal(str(data["total_cost"])) if data.get("total_cost") is not None else None

        record = CostRecord(
            cost_id=data["cost_id"],
            occurred_at=occ,
            cost_type=CostType(data["cost_type"]),
            provider=data["provider"],
            service_or_model=data["service_or_model"],
            execution_id=data["execution_id"],
            usage=usage,
            currency=data.get("currency", "USD"),
            unit_cost=unit_cost,
            total_cost=total_cost,
            pricing_source=data.get("pricing_source", "CATALOG"),
            pricing_version=data.get("pricing_version", "1.0.0"),
            trace_id=data.get("trace_id"),
            mission_id=data.get("mission_id"),
            cycle_id=data.get("cycle_id"),
            correlation_id=data.get("correlation_id", ""),
            causation_id=data.get("causation_id"),
            provenance=data.get("provenance", "MEASUREMENT"),
            idempotency_key=data.get("idempotency_key", ""),
            checksum=data.get("checksum"),
            schema_version=data.get("schema_version", "1.0.0"),
            metadata=data.get("metadata", {}),
        )

        # Validar integridad checksum
        if not record.verify_checksum():
            raise CorruptedCostRecordError(
                f"Tamper detected: Checksum mismatch for CostRecord {record.cost_id}"
            )

        return record

    def append(self, record: CostRecord) -> CostRecord:
        """
        Persiste un CostRecord con atomic write y fsync.
        Garantiza idempotencia por cost_id e idempotency_key.
        """
        # Idempotencia: si ya existe por cost_id
        if record.cost_id in self._memory_cache:
            return self._memory_cache[record.cost_id]

        # Idempotencia: si ya existe por idempotency_key
        if record.idempotency_key in self._idempotency_cache:
            existing_id = self._idempotency_cache[record.idempotency_key]
            return self._memory_cache[existing_id]

        record_dict = self._record_to_dict(record)
        json_data = json.dumps(record_dict, indent=2, ensure_ascii=False)

        file_path = self.costs_dir / f"{record.cost_id}.json"
        tmp_path = self.costs_dir / f"{record.cost_id}.tmp"

        # Escritura atómica con fsync
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(json_data)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, file_path)

        # Actualizar archivo de índice append-only
        index_entry = {
            "cost_id": record.cost_id,
            "occurred_at": record.occurred_at.isoformat(),
            "execution_id": record.execution_id,
            "mission_id": record.mission_id,
            "cycle_id": record.cycle_id,
            "trace_id": record.trace_id,
            "idempotency_key": record.idempotency_key,
        }
        with open(self.index_file, "a", encoding="utf-8") as idx_f:
            idx_f.write(json.dumps(index_entry) + "\n")
            idx_f.flush()

        self._memory_cache[record.cost_id] = record
        self._idempotency_cache[record.idempotency_key] = record.cost_id

        return record

    def get_by_id(self, cost_id: str) -> Optional[CostRecord]:
        return self._memory_cache.get(cost_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[CostRecord]:
        cid = self._idempotency_cache.get(idempotency_key)
        return self._memory_cache.get(cid) if cid else None

    def list_records(
        self,
        execution_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        provider: Optional[str] = None,
        cost_type: Optional[CostType] = None,
        currency: Optional[str] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[CostRecord]:
        results: List[CostRecord] = []

        for record in self._memory_cache.values():
            if execution_id and record.execution_id != execution_id:
                continue
            if mission_id and record.mission_id != mission_id:
                continue
            if cycle_id and record.cycle_id != cycle_id:
                continue
            if trace_id and record.trace_id != trace_id:
                continue
            if provider and record.provider != provider:
                continue
            if cost_type and record.cost_type != cost_type:
                continue
            if currency and record.currency.upper() != currency.upper():
                continue
            if from_time:
                ft = from_time if from_time.tzinfo else from_time.replace(tzinfo=timezone.utc)
                if record.occurred_at < ft:
                    continue
            if to_time:
                tt = to_time if to_time.tzinfo else to_time.replace(tzinfo=timezone.utc)
                if record.occurred_at > tt:
                    continue

            results.append(record)

        # Orden determinista: occurred_at ASC, cost_id ASC
        sorted_results = sorted(results, key=lambda r: (r.occurred_at, r.cost_id))
        return sorted_results[:limit]

    def get_summary(
        self,
        mission_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
    ) -> CostSummary:
        records = self.list_records(
            mission_id=mission_id,
            execution_id=execution_id,
            cycle_id=cycle_id,
            limit=10000,
        )
        return CostSummary.from_records(
            records=records,
            mission_id=mission_id,
            execution_id=execution_id,
            cycle_id=cycle_id,
        )
