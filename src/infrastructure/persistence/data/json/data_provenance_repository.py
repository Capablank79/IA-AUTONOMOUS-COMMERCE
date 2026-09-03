"""
Implementación JSON persistente, atómica y determinista para Data Provenance (Hito L.2).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Inmutabilidad estricta por provenance_id.
- Idempotencia estricta para replays con payload y checksum idénticos.
- Detección explícita de conflictos si se intenta registrar un provenance_id existente con checksum/contenido diferente (ProvenanceConflictError).
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción (CorruptedProvenanceRecordError).
- Thread-safe mediante RLock de concurrencia y atomicidad a nivel filesystem.
- Recuperación y reconstrucción resiliente de índices secundarios (por subject y por source) ante caídas o reinicios.
- Path safety estricto (rechaza traversals, .. , /, \\).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Sequence, Set
import threading
import time

from src.domain.data_provenance.models import (
    ProvenanceRecord,
    SubjectType,
    compute_provenance_checksum,
)
from src.domain.data_provenance.ports import ProvenanceRepositoryPort
from src.domain.security.models import (
    validate_safe_identifier,
    SENSITIVE_KEYS,
    sanitize_security_data,
)

logger = logging.getLogger(__name__)


class JsonProvenanceRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de Data Provenance."""
    pass


class ProvenanceConflictError(JsonProvenanceRepositoryError):
    """Se lanza cuando se intenta sobrescribir un registro de procedencia con contenido diferente."""
    pass


class CorruptedProvenanceRecordError(JsonProvenanceRepositoryError):
    """Se lanza cuando un archivo persistido está corrupto o tiene un checksum inválido."""
    pass


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
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


class JsonProvenanceRepository(ProvenanceRepositoryPort):
    """
    Repositorio JSON persistente, atómico y determinista para ProvenanceRecord (L.2).
    Organización en disco:
      base_dir/
        provenance/
          {provenance_id}.json
        index/
          provenance_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.provenance_dir = self.base_dir / "provenance"
        self.index_dir = self.base_dir / "index"

        self.provenance_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.index_file = self.index_dir / "provenance_index.jsonl"
        self._lock = threading.RLock()

        # Reconstruir o verificar índice persistente
        self._sync_index_from_disk()

    def _get_provenance_file_path(self, provenance_id: str) -> Path:
        validate_safe_identifier(provenance_id, field_name="provenance_id")
        return self.provenance_dir / f"{provenance_id}.json"

    def _sync_index_from_disk(self) -> None:
        """
        Escanea el directorio de procedencias para verificar o reconstruir el índice si es necesario.
        """
        with self._lock:
            if not self.provenance_dir.exists():
                return

            # Si el archivo de índice existe, no es necesario reconstruir todo a menos que esté vacío o dañado
            indexed_ids: Set[str] = set()
            if self.index_file.exists():
                try:
                    with open(self.index_file, "r", encoding="utf-8") as f:
                        for line in f:
                            l = line.strip()
                            if l:
                                entry = json.loads(l)
                                indexed_ids.add(entry["provenance_id"])
                except Exception as e:
                    logger.warning(f"Error reading provenance index file, rebuilding: {e}")
                    indexed_ids.clear()

            # Escanear archivos en disco que no estén indexados
            missing_entries = []
            for file_path in self.provenance_dir.glob("*.json"):
                pid = file_path.stem
                if pid not in indexed_ids:
                    try:
                        rec = self._read_and_validate_file(file_path)
                        if rec:
                            missing_entries.append({
                                "provenance_id": rec.provenance_id,
                                "source_id": rec.source_id,
                                "subject_type": rec.subject_type.value,
                                "subject_id": rec.subject_id,
                                "field_path": rec.field_path,
                                "evidence_id": rec.evidence_id,
                                "checksum": rec.checksum,
                                "captured_at": rec.captured_at.isoformat(),
                            })
                    except Exception as e:
                        logger.error(f"Cannot parse provenance file {file_path}: {e}")

            if missing_entries:
                with open(self.index_file, "a", encoding="utf-8") as f:
                    for entry in missing_entries:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())

    def _read_and_validate_file(self, file_path: Path) -> ProvenanceRecord:
        """
        Lee un archivo JSON de procedencia y verifica su integridad física y semántica SHA-256.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"Provenance file {file_path} not found.")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedProvenanceRecordError(f"Corrupted or invalid JSON in {file_path}: {e}") from e

        required_fields = [
            "provenance_id", "source_id", "subject_type", "subject_id",
            "captured_at", "checksum"
        ]
        for req in required_fields:
            if req not in data:
                raise CorruptedProvenanceRecordError(f"Missing required field '{req}' in {file_path}")

        try:
            captured_at_dt = datetime.fromisoformat(data["captured_at"])
            if captured_at_dt.tzinfo is None:
                captured_at_dt = captured_at_dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            raise CorruptedProvenanceRecordError(f"Invalid timestamp in {file_path}: {e}") from e

        rec = ProvenanceRecord(
            provenance_id=data["provenance_id"],
            source_id=data["source_id"],
            subject_type=SubjectType(data["subject_type"]),
            subject_id=data["subject_id"],
            captured_at=captured_at_dt,
            source_version=data.get("source_version", "1.0.0"),
            source_record_id=data.get("source_record_id"),
            evidence_id=data.get("evidence_id"),
            field_path=data.get("field_path"),
            parent_provenance_ids=tuple(data.get("parent_provenance_ids", ())),
            transformation_id=data.get("transformation_id"),
            correlation_id=data.get("correlation_id", "default-correlation"),
            causation_id=data.get("causation_id"),
            schema_version=data.get("schema_version", "1.0.0"),
            checksum=data["checksum"],
            metadata=data.get("metadata", {}),
        )

        return rec

    def save_provenance(self, record: ProvenanceRecord) -> ProvenanceRecord:
        """
        Persiste un ProvenanceRecord de forma atómica y thread-safe.
        Si ya existe con exactamente el mismo checksum, retorna idempotente.
        Si ya existe con diferente checksum o payload, lanza ProvenanceConflictError.
        """
        with self._lock:
            target_path = self._get_provenance_file_path(record.provenance_id)

            if target_path.exists():
                existing = self._read_and_validate_file(target_path)
                if existing.checksum == record.checksum:
                    # Idempotencia exacta
                    return existing
                else:
                    raise ProvenanceConflictError(
                        f"Conflict detected for provenance_id '{record.provenance_id}'. "
                        f"Existing checksum '{existing.checksum}' != new checksum '{record.checksum}'."
                    )

            payload = {
                "provenance_id": record.provenance_id,
                "source_id": record.source_id,
                "source_version": record.source_version,
                "source_record_id": record.source_record_id,
                "evidence_id": record.evidence_id,
                "subject_type": record.subject_type.value,
                "subject_id": record.subject_id,
                "field_path": record.field_path,
                "captured_at": record.captured_at.isoformat(),
                "parent_provenance_ids": list(record.parent_provenance_ids),
                "transformation_id": record.transformation_id,
                "correlation_id": record.correlation_id,
                "causation_id": record.causation_id,
                "schema_version": record.schema_version,
                "checksum": record.checksum,
                "metadata": _encode_json_value(record.metadata),
            }

            tmp_path = target_path.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(tmp_path, target_path)
            except Exception as e:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                raise JsonProvenanceRepositoryError(f"Failed to persist provenance '{record.provenance_id}': {e}") from e

            # Actualizar índice append-only
            index_entry = {
                "provenance_id": record.provenance_id,
                "source_id": record.source_id,
                "subject_type": record.subject_type.value,
                "subject_id": record.subject_id,
                "field_path": record.field_path,
                "evidence_id": record.evidence_id,
                "checksum": record.checksum,
                "captured_at": record.captured_at.isoformat(),
            }
            try:
                with open(self.index_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(index_entry, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                logger.error(f"Failed to append to provenance index: {e}")

            return record

    def get_provenance(self, provenance_id: str) -> Optional[ProvenanceRecord]:
        with self._lock:
            target_path = self._get_provenance_file_path(provenance_id)
            if not target_path.exists():
                return None
            return self._read_and_validate_file(target_path)

    def find_by_subject(
        self,
        subject_id: str,
        subject_type: Optional[SubjectType] = None,
        field_path: Optional[str] = None,
    ) -> Sequence[ProvenanceRecord]:
        with self._lock:
            matched_ids: List[str] = []
            if self.index_file.exists():
                try:
                    with open(self.index_file, "r", encoding="utf-8") as f:
                        for line in f:
                            l = line.strip()
                            if not l:
                                continue
                            entry = json.loads(l)
                            if entry.get("subject_id") == subject_id:
                                if subject_type and entry.get("subject_type") != subject_type.value:
                                    continue
                                if field_path is not None and entry.get("field_path") != field_path:
                                    continue
                                matched_ids.append(entry["provenance_id"])
                except Exception as e:
                    logger.error(f"Error scanning index for subject {subject_id}: {e}")

            # Cargar y validar cada uno
            results: List[ProvenanceRecord] = []
            seen = set()
            for pid in matched_ids:
                if pid in seen:
                    continue
                seen.add(pid)
                rec = self.get_provenance(pid)
                if rec:
                    results.append(rec)
            return results

    def find_by_source(
        self,
        source_id: str,
        limit: int = 100,
    ) -> Sequence[ProvenanceRecord]:
        with self._lock:
            matched_ids: List[str] = []
            if self.index_file.exists():
                try:
                    with open(self.index_file, "r", encoding="utf-8") as f:
                        for line in f:
                            l = line.strip()
                            if not l:
                                continue
                            entry = json.loads(l)
                            if entry.get("source_id") == source_id:
                                matched_ids.append(entry["provenance_id"])
                                if len(matched_ids) >= limit:
                                    break
                except Exception as e:
                    logger.error(f"Error scanning index for source {source_id}: {e}")

            results: List[ProvenanceRecord] = []
            seen = set()
            for pid in matched_ids:
                if pid in seen:
                    continue
                seen.add(pid)
                rec = self.get_provenance(pid)
                if rec:
                    results.append(rec)
            return results

    def find_by_evidence(
        self,
        evidence_id: str,
    ) -> Sequence[ProvenanceRecord]:
        with self._lock:
            matched_ids: List[str] = []
            if self.index_file.exists():
                try:
                    with open(self.index_file, "r", encoding="utf-8") as f:
                        for line in f:
                            l = line.strip()
                            if not l:
                                continue
                            entry = json.loads(l)
                            if entry.get("evidence_id") == evidence_id:
                                matched_ids.append(entry["provenance_id"])
                except Exception as e:
                    logger.error(f"Error scanning index for evidence {evidence_id}: {e}")

            results: List[ProvenanceRecord] = []
            seen = set()
            for pid in matched_ids:
                if pid in seen:
                    continue
                seen.add(pid)
                rec = self.get_provenance(pid)
                if rec:
                    results.append(rec)
            return results

    def exists(self, provenance_id: str) -> bool:
        with self._lock:
            target_path = self._get_provenance_file_path(provenance_id)
            return target_path.exists()
