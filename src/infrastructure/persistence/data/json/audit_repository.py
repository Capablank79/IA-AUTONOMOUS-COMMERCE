"""
Implementación JSON persistente, atómica y append-only para el Audit Trail (Hito K.1).

Garantiza:
- Atomic write (.tmp -> os.replace).
- Inmutabilidad y semántica append-only.
- Sanitización recursiva de datos sensibles (OAuth, API keys, passwords, tokens, CVV, PAN, secrets).
- Idempotencia estricta por audit_id e idempotency_key determinista.
- Ordenación cronológica determinista por occurred_at con desempate por audit_id.
- Resiliencia ante caídas y recarga íntegra tras reinicio de proceso.
- Verificación de checksum para integridad / detección de corrupción (Tamper evidence).
- Reconstrucción causal y de timeline para misiones.
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List, Tuple
from types import MappingProxyType
import hashlib

from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
    MissionAuditTimeline,
)
from src.domain.audit.ports import AuditRepositoryPort


class JsonAuditRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de auditoría."""
    pass


class CorruptedAuditRecordError(JsonAuditRepositoryError):
    """Se lanza cuando los datos de un registro de auditoría están corruptos o el checksum no coincide."""
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


def _decode_audit_record(data: Dict[str, Any]) -> AuditRecord:
    """Reconstruye una instancia de AuditRecord a partir de un dict JSON."""
    try:
        occurred_at_dt = datetime.fromisoformat(data["occurred_at"])
        if occurred_at_dt.tzinfo is None:
            occurred_at_dt = occurred_at_dt.replace(tzinfo=timezone.utc)

        actor_data = data.get("actor", {})
        actor = AuditActor(
            actor_type=AuditActorType(actor_data.get("actor_type", "SYSTEM")),
            actor_id=actor_data.get("actor_id", "system"),
            details=actor_data.get("details", {}),
        )

        record = AuditRecord(
            audit_id=data["audit_id"],
            record_type=AuditRecordType(data["record_type"]),
            occurred_at=occurred_at_dt,
            actor=actor,
            subject_type=data["subject_type"],
            subject_id=data["subject_id"],
            action_or_operation=data["action_or_operation"],
            status=data["status"],
            correlation_id=data["correlation_id"],
            causation_id=data.get("causation_id"),
            mission_id=data.get("mission_id"),
            entity_reference=data.get("entity_reference"),
            evidence_reference=data.get("evidence_reference"),
            provenance=data.get("provenance", "SYSTEM"),
            idempotency_key=data.get("idempotency_key", ""),
            checksum=data.get("checksum"),
            schema_version=data.get("schema_version", "1.0.0"),
            metadata=data.get("metadata", {}),
        )

        # Validar integridad del checksum persistido si está presente
        if data.get("checksum"):
            payload_for_hash = {
                "audit_id": record.audit_id,
                "record_type": record.record_type.value,
                "occurred_at": record.occurred_at.isoformat(),
                "actor_type": record.actor.actor_type.value,
                "actor_id": record.actor.actor_id,
                "subject_type": record.subject_type,
                "subject_id": record.subject_id,
                "action_or_operation": record.action_or_operation,
                "status": record.status,
                "correlation_id": record.correlation_id,
                "causation_id": record.causation_id,
                "mission_id": record.mission_id,
                "entity_reference": record.entity_reference,
                "evidence_reference": record.evidence_reference,
                "provenance": record.provenance,
                "idempotency_key": record.idempotency_key,
                "schema_version": record.schema_version,
            }
            computed = hashlib.sha256(json.dumps(payload_for_hash, sort_keys=True).encode("utf-8")).hexdigest()
            if computed != data.get("checksum"):
                raise CorruptedAuditRecordError(f"Checksum mismatch for audit record {record.audit_id}")

        return record
    except CorruptedAuditRecordError:
        raise
    except Exception as e:
        raise CorruptedAuditRecordError(f"Corrupted or invalid audit record JSON: {e}") from e


def _encode_audit_record(record: AuditRecord) -> Dict[str, Any]:
    """Serializa un AuditRecord a dict con sanitización recursiva."""
    return {
        "audit_id": record.audit_id,
        "record_type": record.record_type.value,
        "occurred_at": record.occurred_at.isoformat(),
        "actor": {
            "actor_type": record.actor.actor_type.value,
            "actor_id": record.actor.actor_id,
            "details": _encode_json_value(record.actor.details),
        },
        "subject_type": record.subject_type,
        "subject_id": record.subject_id,
        "action_or_operation": record.action_or_operation,
        "status": record.status,
        "correlation_id": record.correlation_id,
        "causation_id": record.causation_id,
        "mission_id": record.mission_id,
        "entity_reference": record.entity_reference,
        "evidence_reference": record.evidence_reference,
        "provenance": record.provenance,
        "idempotency_key": record.idempotency_key,
        "checksum": record.checksum,
        "schema_version": record.schema_version,
        "metadata": _encode_json_value(record.metadata),
    }


class JsonAuditRepository(AuditRepositoryPort):
    """
    Adaptador persistente en JSON para Audit Trail.
    Almacena registros en `{storage_dir}/audit_records/{audit_id}.json`.
    Mantiene un índice en memoria para consultas eficientes e idempotencia.
    """

    def __init__(self, storage_dir: Union[str, Path]):
        self.storage_dir = Path(storage_dir)
        self._records_dir = self.storage_dir / "audit_records"
        self._records_dir.mkdir(parents=True, exist_ok=True)

        # Índices en memoria: {audit_id: record} e {idempotency_key: audit_id}
        self._records_by_id: Dict[str, AuditRecord] = {}
        self._id_by_idempotency_key: Dict[str, str] = {}
        self._load_existing_records()

    def _load_existing_records(self) -> None:
        """Carga y valida todos los registros existentes desde disco."""
        if not self._records_dir.exists():
            return

        for path in self._records_dir.glob("*.json"):
            if path.name.endswith(".tmp"):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                record = _decode_audit_record(data)
                self._records_by_id[record.audit_id] = record
                if record.idempotency_key:
                    self._id_by_idempotency_key[record.idempotency_key] = record.audit_id
            except Exception as e:
                # Si un archivo está corrupto, propagar o manejar según política
                raise CorruptedAuditRecordError(f"Failed loading audit file {path.name}: {e}") from e

    def append(self, record: AuditRecord) -> AuditRecord:
        """
        Persiste un AuditRecord de forma atómica e inmutable.
        Si ya existe por audit_id o idempotency_key, retorna el registro existente.
        """
        if not isinstance(record, AuditRecord):
            raise ValueError("record must be an instance of AuditRecord")

        # 1. Chequeo por audit_id existente (idempotencia)
        if record.audit_id in self._records_by_id:
            return self._records_by_id[record.audit_id]

        # 2. Chequeo por idempotency_key existente (idempotencia)
        if record.idempotency_key and record.idempotency_key in self._id_by_idempotency_key:
            existing_id = self._id_by_idempotency_key[record.idempotency_key]
            return self._records_by_id[existing_id]

        # 3. Persistir atómicamente a disco
        file_path = self._records_dir / f"{record.audit_id}.json"
        temp_path = self._records_dir / f"{record.audit_id}.tmp"

        payload = _encode_audit_record(record)
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, file_path)

        # 4. Actualizar memoria / índices
        self._records_by_id[record.audit_id] = record
        if record.idempotency_key:
            self._id_by_idempotency_key[record.idempotency_key] = record.audit_id

        return record

    def get_by_id(self, audit_id: str) -> Optional[AuditRecord]:
        return self._records_by_id.get(audit_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AuditRecord]:
        audit_id = self._id_by_idempotency_key.get(idempotency_key)
        if audit_id:
            return self._records_by_id.get(audit_id)
        return None

    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        results: List[AuditRecord] = []

        for record in self._records_by_id.values():
            if mission_id and record.mission_id != mission_id:
                continue
            if correlation_id and record.correlation_id != correlation_id:
                continue
            if subject_type and record.subject_type != subject_type:
                continue
            if subject_id and record.subject_id != subject_id:
                continue
            if record_type and record.record_type != record_type:
                continue
            if from_time and record.occurred_at < from_time:
                continue
            if to_time and record.occurred_at > to_time:
                continue
            results.append(record)

        # Ordenar estrictamente por occurred_at con desempate determinista por audit_id
        results.sort(key=lambda r: (r.occurred_at, r.audit_id))

        if limit > 0:
            return results[:limit]
        return results

    def reconstruct_mission_timeline(self, mission_id: str) -> MissionAuditTimeline:
        """
        Reconstruye cronológica y causalmente todos los registros de una misión.
        """
        records = self.list_records(mission_id=mission_id, limit=10000)
        corr_id = records[0].correlation_id if records else f"mission-{mission_id}"
        return MissionAuditTimeline(
            mission_id=mission_id,
            correlation_id=corr_id,
            records=tuple(records),
            reconstructed_at=datetime.now(timezone.utc),
        )
