"""
Implementación JSON persistente, atómica y append-only para Agent Trace (Hito K.2).

Garantiza:
- Atomic write (.tmp -> os.replace) con fsync.
- Inmutabilidad y semántica append-only.
- Sanitización recursiva de datos sensibles y exclusión estricta de Chain-of-Thought (CoT).
- Idempotencia estricta por trace_id e idempotency_key determinista.
- Ordenación cronológica determinista por step_number y started_at con desempate por trace_id.
- Resiliencia ante caídas y recarga íntegra tras reinicio de proceso.
- Verificación de checksum para integridad / detección de corrupción (Tamper evidence).
- Reconstrucción de ExecutionTraceTimeline.
"""

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Union, Optional, Any, Dict, List, Tuple
from types import MappingProxyType
import hashlib

from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
    ExecutionTraceTimeline,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort


class JsonAgentTraceRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de trazas."""
    pass


class CorruptedTraceRecordError(JsonAgentTraceRepositoryError):
    """Se lanza cuando los datos de un registro de traza están corruptos o el checksum no coincide."""
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


class JsonAgentTraceRepository(AgentTraceRepositoryPort):
    """
    Repositorio JSON persistente y seguro para Agent Trace.
    Almacena registros individuales inmutables en estructura organizada por ejecuciones.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.traces_dir = self.base_dir / "traces"
        self.index_dir = self.base_dir / "index"
        
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        
        # Archivo de índice maestro append-only para ordenación y búsqueda rápida
        self.index_file = self.index_dir / "traces_index.jsonl"
        if not self.index_file.exists():
            self._init_index_file()

    def _init_index_file(self) -> None:
        temp_path = self.index_file.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            pass
        temp_path.replace(self.index_file)

    def append(self, record: AgentTraceRecord) -> AgentTraceRecord:
        """
        Persiste un AgentTraceRecord de forma atómica e inmutable.
        Garantiza idempotencia estricta ante replays.
        """
        existing = self.get_by_id(record.trace_id)
        if existing:
            return existing

        if record.idempotency_key:
            existing_by_idem = self.get_by_idempotency_key(record.idempotency_key)
            if existing_by_idem:
                return existing_by_idem

        # Archivo por trace_id
        file_path = self.traces_dir / f"{record.trace_id}.json"
        temp_path = file_path.with_suffix(".tmp")

        payload = {
            "trace_id": record.trace_id,
            "component_name": record.component_name,
            "execution_id": record.execution_id,
            "step_number": record.step_number,
            "step_type": record.step_type.value,
            "operation": record.operation,
            "started_at": record.started_at.isoformat(),
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            "status": record.status.value,
            "tool_or_service": record.tool_or_service,
            "input_reference": record.input_reference,
            "output_reference": record.output_reference,
            "correlation_id": record.correlation_id,
            "causation_id": record.causation_id,
            "mission_id": record.mission_id,
            "cycle_id": record.cycle_id,
            "provenance": record.provenance,
            "idempotency_key": record.idempotency_key,
            "checksum": record.checksum,
            "schema_version": record.schema_version,
            "metadata": _encode_json_value(record.metadata),
        }

        # Escritura atómica
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        temp_path.replace(file_path)

        # Entrada de índice append-only
        index_entry = {
            "trace_id": record.trace_id,
            "component_name": record.component_name,
            "execution_id": record.execution_id,
            "step_number": record.step_number,
            "step_type": record.step_type.value,
            "status": record.status.value,
            "started_at": record.started_at.isoformat(),
            "correlation_id": record.correlation_id,
            "mission_id": record.mission_id,
            "cycle_id": record.cycle_id,
            "idempotency_key": record.idempotency_key,
        }
        with open(self.index_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(index_entry, ensure_ascii=False) + "\n")
            f.flush()

        return record

    def get_by_id(self, trace_id: str) -> Optional[AgentTraceRecord]:
        file_path = self.traces_dir / f"{trace_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._dict_to_record(data)
        except Exception as e:
            raise CorruptedTraceRecordError(f"Error loading trace record {trace_id}: {e}") from e

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AgentTraceRecord]:
        if not self.index_file.exists():
            return None

        with open(self.index_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("idempotency_key") == idempotency_key:
                        return self.get_by_id(entry["trace_id"])
                except Exception:
                    continue
        return None

    def list_records(
        self,
        execution_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        component_name: Optional[str] = None,
        step_type: Optional[StepType] = None,
        status: Optional[TraceStatus] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AgentTraceRecord]:
        """
        Consulta registros de trazas ordenados determinísticamente por step_number y started_at.
        """
        matched_trace_ids = []
        if self.index_file.exists():
            with open(self.index_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if execution_id and entry.get("execution_id") != execution_id:
                            continue
                        if mission_id and entry.get("mission_id") != mission_id:
                            continue
                        if cycle_id and entry.get("cycle_id") != cycle_id:
                            continue
                        if correlation_id and entry.get("correlation_id") != correlation_id:
                            continue
                        if component_name and entry.get("component_name") != component_name:
                            continue
                        if step_type and entry.get("step_type") != (step_type.value if hasattr(step_type, "value") else str(step_type)):
                            continue
                        if status and entry.get("status") != (status.value if hasattr(status, "value") else str(status)):
                            continue
                        
                        entry_time = datetime.fromisoformat(entry["started_at"])
                        if entry_time.tzinfo is None:
                            entry_time = entry_time.replace(tzinfo=timezone.utc)
                        if from_time and entry_time < from_time:
                            continue
                        if to_time and entry_time > to_time:
                            continue

                        matched_trace_ids.append(entry["trace_id"])
                    except Exception:
                        continue

        # Evitar duplicados manteniendo orden de inserción
        seen_ids = set()
        unique_ids = []
        for tid in matched_trace_ids:
            if tid not in seen_ids:
                seen_ids.add(tid)
                unique_ids.append(tid)

        records = []
        for tid in unique_ids:
            rec = self.get_by_id(tid)
            if rec:
                records.append(rec)

        # Orden determinista: step_number ASC, started_at ASC, trace_id ASC
        sorted_records = sorted(
            records,
            key=lambda r: (r.step_number, r.started_at, r.trace_id)
        )
        return sorted_records[:limit]

    def get_execution_timeline(self, execution_id: str) -> ExecutionTraceTimeline:
        records = self.list_records(execution_id=execution_id, limit=5000)
        return ExecutionTraceTimeline.build_from_records(execution_id, records)

    def _dict_to_record(self, data: Dict[str, Any]) -> AgentTraceRecord:
        started_at = datetime.fromisoformat(data["started_at"])
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        completed_at = None
        if data.get("completed_at"):
            completed_at = datetime.fromisoformat(data["completed_at"])
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)

        record = AgentTraceRecord(
            trace_id=data["trace_id"],
            component_name=data["component_name"],
            execution_id=data["execution_id"],
            step_number=data["step_number"],
            step_type=StepType(data["step_type"]),
            operation=data["operation"],
            started_at=started_at,
            completed_at=completed_at,
            status=TraceStatus(data["status"]),
            tool_or_service=data.get("tool_or_service"),
            input_reference=data.get("input_reference"),
            output_reference=data.get("output_reference"),
            correlation_id=data.get("correlation_id", ""),
            causation_id=data.get("causation_id"),
            mission_id=data.get("mission_id"),
            cycle_id=data.get("cycle_id"),
            provenance=data.get("provenance", "AGENT"),
            idempotency_key=data.get("idempotency_key", ""),
            checksum=data.get("checksum"),
            schema_version=data.get("schema_version", "1.0.0"),
            metadata=data.get("metadata", {}),
        )

        if not record.verify_checksum():
            raise CorruptedTraceRecordError(f"Checksum mismatch for trace record {record.trace_id}")

        return record
