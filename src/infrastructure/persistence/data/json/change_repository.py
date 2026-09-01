"""
Implementación JSON atómica y persistente del repositorio de ChangeRecord (Hito J.4).

Garantiza:
- Atomic write (.tmp -> os.replace)
- Sanitización recursiva de datos sensibles
- Idempotencia estricta por clave de idempotencia
- Manejo de corrupción y resiliencia ante reinicio
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List, Tuple
from types import MappingProxyType

from src.domain.change_detection.models import (
    ChangeRecord,
    ChangeSubjectType,
    ChangeType,
    ChangeSignificance,
    ObservedChangeField,
    DerivedChangeDelta,
)
from src.domain.change_detection.ports import ChangeRecordRepositoryPort
from src.domain.market_intelligence.models import Confidence


class JsonChangeRecordRepositoryError(Exception):
    """Excepción base para errores en el repositorio de registros de cambio."""
    pass


class CorruptedChangeRecordDataError(JsonChangeRecordRepositoryError):
    """Se lanza cuando los datos de un registro de cambio están corruptos."""
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
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


def _decode_change_record(data: Dict[str, Any]) -> ChangeRecord:
    """Reconstruye una instancia de ChangeRecord a partir de un dict JSON."""
    try:
        change_id = data["change_id"]
        subject_type = ChangeSubjectType(data["subject_type"])
        subject_id = data["subject_id"]
        previous_reference = data.get("previous_reference")
        current_reference = data["current_reference"]
        change_type = ChangeType(data["change_type"])

        detected_at = datetime.fromisoformat(data["detected_at"])
        if detected_at.tzinfo is None:
            detected_at = detected_at.replace(tzinfo=timezone.utc)

        observed_to = datetime.fromisoformat(data["observed_to"])
        if observed_to.tzinfo is None:
            observed_to = observed_to.replace(tzinfo=timezone.utc)

        observed_from = None
        if data.get("observed_from"):
            observed_from = datetime.fromisoformat(data["observed_from"])
            if observed_from.tzinfo is None:
                observed_from = observed_from.replace(tzinfo=timezone.utc)

        changed_fields = tuple(data.get("changed_fields", []))

        observed_changes = []
        for oc in data.get("observed_changes", []):
            observed_changes.append(
                ObservedChangeField(
                    field_name=oc["field_name"],
                    previous_value=oc.get("previous_value"),
                    current_value=oc.get("current_value"),
                    is_previous_unknown=oc.get("is_previous_unknown", False),
                    is_current_unknown=oc.get("is_current_unknown", False),
                )
            )

        derived_deltas = []
        for dd in data.get("derived_deltas", []):
            num_d = Decimal(str(dd["numeric_delta"])) if dd.get("numeric_delta") is not None else None
            pct_d = Decimal(str(dd["percentage_delta"])) if dd.get("percentage_delta") is not None else None
            derived_deltas.append(
                DerivedChangeDelta(
                    field_name=dd["field_name"],
                    numeric_delta=num_d,
                    percentage_delta=pct_d,
                    delta_description=dd.get("delta_description"),
                    is_valid_delta=dd.get("is_valid_delta", True),
                )
            )

        significance = ChangeSignificance(data.get("significance", ChangeSignificance.NONE.value))
        confidence = Confidence(data.get("confidence", Confidence.HIGH.value))
        provenance = data.get("provenance", "DERIVED")
        correlation_id = data.get("correlation_id", "default-correlation")
        idempotency_key = data.get("idempotency_key", "")
        evidence_references = tuple(data.get("evidence_references", []))
        unknown_fields = tuple(data.get("unknown_fields", []))
        metadata = data.get("metadata", {})

        return ChangeRecord(
            change_id=change_id,
            subject_type=subject_type,
            subject_id=subject_id,
            previous_reference=previous_reference,
            current_reference=current_reference,
            change_type=change_type,
            detected_at=detected_at,
            observed_from=observed_from,
            observed_to=observed_to,
            changed_fields=changed_fields,
            observed_changes=tuple(observed_changes),
            derived_deltas=tuple(derived_deltas),
            significance=significance,
            confidence=confidence,
            provenance=provenance,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            evidence_references=evidence_references,
            unknown_fields=unknown_fields,
            metadata=metadata,
        )
    except Exception as e:
        raise CorruptedChangeRecordDataError(f"Failed to decode ChangeRecord: {str(e)}") from e


class JsonChangeRecordRepository(ChangeRecordRepositoryPort):
    """
    Adaptador de persistencia JSON para ChangeRecord.
    Almacena cada cambio en un archivo individual nombrado `{change_id}.json`
    dentro del directorio base indicado.
    """

    def __init__(self, storage_dir: Union[str, Path]):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def save(self, change_record: ChangeRecord) -> None:
        """Guarda un registro de cambio de forma atómica e idempotente."""
        # Verificar si ya existe por idempotency_key
        existing = self.get_by_idempotency_key(change_record.idempotency_key)
        if existing is not None:
            # Idempotencia: ya guardado
            return

        file_path = self.storage_dir / f"{change_record.change_id}.json"
        temp_path = self.storage_dir / f"{change_record.change_id}.tmp"

        payload = {
            "change_id": change_record.change_id,
            "subject_type": change_record.subject_type.value,
            "subject_id": change_record.subject_id,
            "previous_reference": change_record.previous_reference,
            "current_reference": change_record.current_reference,
            "change_type": change_record.change_type.value,
            "detected_at": _encode_json_value(change_record.detected_at),
            "observed_from": _encode_json_value(change_record.observed_from) if change_record.observed_from else None,
            "observed_to": _encode_json_value(change_record.observed_to),
            "changed_fields": list(change_record.changed_fields),
            "observed_changes": [
                {
                    "field_name": oc.field_name,
                    "previous_value": _encode_json_value(oc.previous_value),
                    "current_value": _encode_json_value(oc.current_value),
                    "is_previous_unknown": oc.is_previous_unknown,
                    "is_current_unknown": oc.is_current_unknown,
                }
                for oc in change_record.observed_changes
            ],
            "derived_deltas": [
                {
                    "field_name": dd.field_name,
                    "numeric_delta": _encode_json_value(dd.numeric_delta),
                    "percentage_delta": _encode_json_value(dd.percentage_delta),
                    "delta_description": dd.delta_description,
                    "is_valid_delta": dd.is_valid_delta,
                }
                for dd in change_record.derived_deltas
            ],
            "significance": change_record.significance.value,
            "confidence": change_record.confidence.value,
            "provenance": change_record.provenance,
            "correlation_id": change_record.correlation_id,
            "idempotency_key": change_record.idempotency_key,
            "evidence_references": list(change_record.evidence_references),
            "unknown_fields": list(change_record.unknown_fields),
            "metadata": _encode_json_value(change_record.metadata),
        }

        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, file_path)
        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise JsonChangeRecordRepositoryError(f"Failed to save ChangeRecord to {file_path}: {str(e)}") from e

    def save_all(self, change_records: List[ChangeRecord]) -> int:
        """Guarda múltiples registros y retorna la cantidad de nuevos registros almacenados."""
        saved_count = 0
        for cr in change_records:
            existing = self.get_by_idempotency_key(cr.idempotency_key)
            if existing is None:
                self.save(cr)
                saved_count += 1
        return saved_count

    def get_by_id(self, change_id: str) -> Optional[ChangeRecord]:
        """Obtiene un ChangeRecord por su ID."""
        file_path = self.storage_dir / f"{change_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _decode_change_record(data)
        except CorruptedChangeRecordDataError:
            raise
        except Exception as e:
            raise CorruptedChangeRecordDataError(f"Error reading {file_path}: {str(e)}") from e

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[ChangeRecord]:
        """Obtiene un ChangeRecord buscando por su idempotency_key."""
        for file_path in self.storage_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("idempotency_key") == idempotency_key:
                    return _decode_change_record(data)
            except Exception:
                continue
        return None

    def list_by_subject(
        self,
        subject_type: ChangeSubjectType,
        subject_id: str,
        limit: int = 100,
    ) -> List[ChangeRecord]:
        """Lista registros de cambio para un sujeto, ordenados cronológicamente."""
        records = []
        for file_path in self.storage_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if (
                    data.get("subject_type") == subject_type.value
                    and data.get("subject_id") == subject_id
                ):
                    records.append(_decode_change_record(data))
            except Exception:
                continue

        records.sort(key=lambda r: r.observed_to)
        return records[:limit]

    def list_all(self, limit: int = 1000) -> List[ChangeRecord]:
        """Lista todos los registros de cambio persistidos."""
        records = []
        for file_path in self.storage_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                records.append(_decode_change_record(data))
            except Exception:
                continue

        records.sort(key=lambda r: r.observed_to)
        return records[:limit]
