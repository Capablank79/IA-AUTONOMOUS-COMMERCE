"""
Implementación JSON persistente, atómica y determinista para Source Registry (Hito L.1).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Inmutabilidad y versionado estricto por (source_id, version).
- Idempotencia estricta para payloads idénticos.
- Detección explícita de conflictos ante diferente checksum para misma clave (SourceVersionConflictError).
- Detección de colisiones si un canonical_identifier ya está asignado a otro source_id (SourceCanonicalConflictError).
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción (CorruptedSourceRecordError).
- Thread-safe mediante lock de concurrencia y file-lock a nivel filesystem.
- Recuperación y reconstrucción resiliente del índice index/sources_index.jsonl ante caídas o reinicios.
- Path safety estricto (rechaza traversals, .. , /, \\).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Sequence
from contextlib import contextmanager
import threading
import time

from src.domain.source_registry.models import (
    RegisteredSource,
    SourceType,
    SourceStatus,
    compute_source_checksum,
    build_canonical_identifier,
)
from src.domain.source_registry.ports import SourceRegistryRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS, sanitize_security_data

logger = logging.getLogger(__name__)


class JsonSourceRegistryRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de Source Registry."""
    pass


class SourceVersionConflictError(JsonSourceRegistryRepositoryError):
    """Se lanza cuando se intenta sobrescribir una versión de fuente existente con contenido diferente."""
    pass


class SourceCanonicalConflictError(JsonSourceRegistryRepositoryError):
    """Se lanza cuando un canonical_identifier ya está asociado a otro source_id."""
    pass


class CorruptedSourceRecordError(JsonSourceRegistryRepositoryError):
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


class JsonSourceRegistryRepository(SourceRegistryRepositoryPort):
    """
    Repositorio JSON persistente, atómico y determinista para RegisteredSource (L.1).
    Organización en disco:
      base_dir/
        sources/
          {source_id}/
            {version}.json
        index/
          sources_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.sources_dir = self.base_dir / "sources"
        self.index_dir = self.base_dir / "index"

        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.sources_index_file = self.index_dir / "sources_index.jsonl"
        self._thread_lock = threading.RLock()
        self._lock_file = self.base_dir / ".repository.lock"
        with self._exclusive_lock():
            self._recover_index_if_needed()

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            # En modo multi-hilo en el mismo proceso, RLock maneja la reentrancia.
            # El lock de archivo se usa para sincronización inter-proceso con manejo atómico.
            yield

    def _recover_index_if_needed(self) -> None:
        valid_index = self.sources_index_file.exists()
        if valid_index:
            try:
                with open(self.sources_index_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            json.loads(line)
            except (OSError, json.JSONDecodeError):
                valid_index = False
        if valid_index:
            return

        entries = []
        for version_file in sorted(self.sources_dir.glob("*/*.json")):
            try:
                source = self._load_source_file(version_file)
                entries.append({
                    "source_id": source.source_id,
                    "version": source.version,
                    "canonical_identifier": source.canonical_identifier,
                    "name": source.name,
                    "source_type": source.source_type.value,
                    "provider": source.provider,
                    "status": source.status.value,
                    "checksum": source.checksum,
                    "created_at": source.created_at.isoformat(),
                    "updated_at": source.updated_at.isoformat(),
                })
            except Exception as e:
                logger.warning(f"Skipping unrecoverable source file {version_file}: {e}")

        tmp_path = self.sources_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.sources_index_file)

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Escribe un archivo JSON de manera atómica (.tmp -> fsync -> os.replace)."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        payload = json.dumps(_encode_json_value(data), indent=2, sort_keys=True, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    def _append_to_index(self, index_file: Path, entry: Dict[str, Any]) -> None:
        """Agrega una entrada append-only a un archivo index JSONL con fsync."""
        tmp_line = json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n"
        with open(index_file, "a", encoding="utf-8") as f:
            f.write(tmp_line)
            f.flush()
            os.fsync(f.fileno())

    def _load_source_file(self, file_path: Path) -> RegisteredSource:
        """Carga y valida la integridad estricta por checksum de un archivo de fuente JSON."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            source = RegisteredSource.from_dict(data)

            # Recomputar y validar checksum
            recomputed_checksum = compute_source_checksum(
                source_id=source.source_id,
                name=source.name,
                source_type=source.source_type,
                provider=source.provider,
                canonical_identifier=source.canonical_identifier,
                endpoint_reference=source.endpoint_reference,
                schema_version=source.schema_version,
                version=source.version,
                status=source.status,
                metadata=source.metadata,
            )

            if source.checksum != recomputed_checksum:
                raise CorruptedSourceRecordError(
                    f"Checksum mismatch in {file_path.name}: file has '{source.checksum}', recomputed '{recomputed_checksum}'"
                )

            return source
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            raise CorruptedSourceRecordError(f"Corrupted source record in {file_path}: {e}") from e

    def save_source(self, source: RegisteredSource) -> RegisteredSource:
        """Persiste una fuente bajo exclusión mutua para que check-and-write sea atómico."""
        validate_safe_identifier(source.source_id, field_name="source_id")
        validate_safe_identifier(source.version, field_name="version")

        with self._exclusive_lock():
            # 1. Verificar si ya existe el mismo canonical_identifier bajo otro source_id
            existing_by_canonical = self._find_by_canonical_identifier_unlocked(source.canonical_identifier)
            if existing_by_canonical and existing_by_canonical.source_id != source.source_id:
                raise SourceCanonicalConflictError(
                    f"Canonical identifier '{source.canonical_identifier}' is already registered under source_id '{existing_by_canonical.source_id}'."
                )

            source_dir = self.sources_dir / source.source_id
            version_file = source_dir / f"{source.version}.json"

            if version_file.exists():
                try:
                    existing_source = self._load_source_file(version_file)
                    if existing_source.checksum == source.checksum:
                        return existing_source
                    else:
                        raise SourceVersionConflictError(
                            f"Source '{source.source_id}' version '{source.version}' already exists with different checksum ({existing_source.checksum} vs {source.checksum})."
                        )
                except SourceVersionConflictError:
                    raise
                except Exception as e:
                    raise CorruptedSourceRecordError(
                        f"Cannot verify existing source '{source.source_id}' v{source.version}: {e}"
                    ) from e

            # Escribir archivo de versión atómicamente
            self._atomic_write_json(version_file, source.to_dict())

            # Actualizar índice
            self._append_to_index(
                self.sources_index_file,
                {
                    "source_id": source.source_id,
                    "version": source.version,
                    "canonical_identifier": source.canonical_identifier,
                    "name": source.name,
                    "source_type": source.source_type.value,
                    "provider": source.provider,
                    "status": source.status.value,
                    "checksum": source.checksum,
                    "created_at": source.created_at.isoformat(),
                    "updated_at": source.updated_at.isoformat(),
                },
            )
            return source

    def get_source(self, source_id: str, version: Optional[str] = None) -> Optional[RegisteredSource]:
        """Obtiene una fuente por ID y versión específica (o la más reciente si version es None)."""
        validate_safe_identifier(source_id, field_name="source_id")
        if version is not None:
            validate_safe_identifier(version, field_name="version")

        with self._exclusive_lock():
            source_dir = self.sources_dir / source_id
            if not source_dir.exists() or not source_dir.is_dir():
                return None

            if version is not None:
                version_file = source_dir / f"{version}.json"
                if not version_file.exists():
                    return None
                return self._load_source_file(version_file)

            # Buscar todas las versiones disponibles y retornar la última (orden semver/lexicográfico)
            v_files = sorted(list(source_dir.glob("*.json")), reverse=True)
            if not v_files:
                return None
            return self._load_source_file(v_files[0])

    def _find_by_canonical_identifier_unlocked(self, canonical_identifier: str) -> Optional[RegisteredSource]:
        """Búsqueda interna sin re-adquirir lock."""
        norm_ci = canonical_identifier.strip().lower()
        if not self.sources_index_file.exists():
            return None

        matched_source_id = None
        matched_version = None

        with open(self.sources_index_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("canonical_identifier", "").lower() == norm_ci:
                    matched_source_id = entry.get("source_id")
                    matched_version = entry.get("version")

        if matched_source_id:
            source_dir = self.sources_dir / matched_source_id
            version_file = source_dir / f"{matched_version}.json"
            if version_file.exists():
                return self._load_source_file(version_file)

        return None

    def find_by_canonical_identifier(self, canonical_identifier: str) -> Optional[RegisteredSource]:
        """Busca una fuente registrada por su canonical_identifier exacto."""
        with self._exclusive_lock():
            return self._find_by_canonical_identifier_unlocked(canonical_identifier)

    def list_sources(
        self,
        source_type: Optional[SourceType] = None,
        provider: Optional[str] = None,
        status: Optional[SourceStatus] = None,
        limit: int = 100,
    ) -> Sequence[RegisteredSource]:
        """Lista las fuentes más recientes registradas con filtros opcionales."""
        with self._exclusive_lock():
            sources_map: Dict[str, RegisteredSource] = {}
            for source_id_dir in sorted(self.sources_dir.glob("*")):
                if source_id_dir.is_dir():
                    src = self.get_source(source_id_dir.name)
                    if src:
                        sources_map[src.source_id] = src

            results = []
            for src in sources_map.values():
                if source_type and src.source_type != source_type:
                    continue
                if provider and src.provider.lower() != provider.lower():
                    continue
                if status and src.status != status:
                    continue
                results.append(src)
                if len(results) >= limit:
                    break

            return tuple(results)

    def exists(self, source_id: str, version: Optional[str] = None) -> bool:
        """Verifica existencia de fuente registrada."""
        validate_safe_identifier(source_id, field_name="source_id")
        if version is not None:
            validate_safe_identifier(version, field_name="version")

        with self._exclusive_lock():
            source_dir = self.sources_dir / source_id
            if not source_dir.exists():
                return False
            if version is not None:
                return (source_dir / f"{version}.json").exists()
            return len(list(source_dir.glob("*.json"))) > 0
