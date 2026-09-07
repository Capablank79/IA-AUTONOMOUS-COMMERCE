"""
Implementación JSON persistente, atómica y determinista para SecretMetadata (Hito N.5 — Secret Management).

Garantiza:
- Atomic write (.tmp -> fsync -> os.replace).
- Almacenamiento exclusivo de METADATOS (referencias, tipos, estados, checksums). Cero material confidencial.
- Idempotencia estricta para metadatos idénticos.
- Detección explícita de conflictos ante diferente checksum para el mismo reference_id (SecretMetadataConflictError).
- Verificación estricta de integridad SHA-256 en lectura y detección de corrupción (CorruptedSecretMetadataRecordError).
- Thread-safe mediante lock de concurrencia.
- Recuperación y reconstrucción resiliente del índice index/secrets_index.jsonl ante caídas o reinicios.
- Path safety estricto (rechaza traversals, .., /, \\, :).
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

from src.domain.secrets.models import (
    SecretMetadata,
    SecretType,
    SecretStatus,
    compute_secret_metadata_checksum,
)
from src.domain.secrets.ports import SecretMetadataRepositoryPort
from src.domain.security.models import validate_safe_identifier, SENSITIVE_KEYS, sanitize_security_data

logger = logging.getLogger(__name__)


class JsonSecretMetadataRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de SecretMetadata."""
    pass


class SecretMetadataConflictError(JsonSecretMetadataRepositoryError):
    """Se lanza cuando se intenta registrar metadatos con el mismo ID pero contenido incompatible."""
    pass


class CorruptedSecretMetadataRecordError(JsonSecretMetadataRepositoryError):
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
            # No redactar campos de primer nivel de SecretMetadata (ej. secret_name, secret_type)
            if str(k) in ("secret_name", "secret_type", "reference_id", "provider", "version", "status", "created_at", "updated_at", "expires_at", "checksum"):
                cleaned[str(k)] = _encode_json_value(v)
                continue
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonSecretMetadataRepository(SecretMetadataRepositoryPort):
    """
    Repositorio JSON persistente, atómico y determinista para SecretMetadata (N.5).
    Organización en disco:
      base_dir/
        secrets_metadata/
          {reference_id}.json
        index/
          secrets_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.secrets_dir = self.base_dir / "secrets_metadata"
        self.index_dir = self.base_dir / "index"

        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.secrets_index_file = self.index_dir / "secrets_index.jsonl"
        self._thread_lock = threading.RLock()
        with self._exclusive_lock():
            self._recover_index_if_needed()

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            yield

    def _recover_index_if_needed(self) -> None:
        valid_index = self.secrets_index_file.exists()
        if valid_index:
            try:
                with open(self.secrets_index_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            json.loads(line)
            except (OSError, json.JSONDecodeError):
                valid_index = False
        if valid_index:
            return

        entries = []
        for secret_file in sorted(self.secrets_dir.glob("*.json")):
            try:
                meta = self._load_metadata_file(secret_file)
                entries.append(meta.to_dict())
            except Exception as e:
                logger.warning(f"Skipping unrecoverable secret metadata file {secret_file}: {e}")

        tmp_path = self.secrets_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.secrets_index_file)

    def _rebuild_index_unlocked(self) -> None:
        entries = []
        for secret_file in sorted(self.secrets_dir.glob("*.json")):
            try:
                meta = self._load_metadata_file(secret_file)
                entries.append(meta.to_dict())
            except Exception as e:
                logger.warning(f"Skipping unrecoverable secret metadata file {secret_file}: {e}")

        tmp_path = self.secrets_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.secrets_index_file)

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        payload = json.dumps(_encode_json_value(data), indent=2, sort_keys=True, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    def _append_to_index(self, index_file: Path, entry: Dict[str, Any]) -> None:
        tmp_line = json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n"
        with open(index_file, "a", encoding="utf-8") as f:
            f.write(tmp_line)
            f.flush()
            os.fsync(f.fileno())

    def _load_metadata_file(self, file_path: Path) -> SecretMetadata:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            meta = SecretMetadata.from_dict(data)

            recomputed_checksum = compute_secret_metadata_checksum(
                reference_id=meta.reference_id,
                secret_name=meta.secret_name,
                provider=meta.provider,
                secret_type=meta.secret_type,
                version=meta.version,
                status=meta.status,
                created_at=meta.created_at,
                expires_at=meta.expires_at,
                metadata=meta.metadata,
            )

            if meta.checksum != recomputed_checksum:
                raise CorruptedSecretMetadataRecordError(
                    f"Checksum mismatch in {file_path.name}: file has '{meta.checksum}', recomputed '{recomputed_checksum}'"
                )

            return meta
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            raise CorruptedSecretMetadataRecordError(f"Corrupted secret metadata record in {file_path}: {e}") from e

    def save_metadata(self, metadata: SecretMetadata) -> SecretMetadata:
        validate_safe_identifier(metadata.reference_id, field_name="reference_id")

        with self._exclusive_lock():
            meta_file = self.secrets_dir / f"{metadata.reference_id}.json"

            if meta_file.exists():
                try:
                    existing = self._load_metadata_file(meta_file)
                    if existing.checksum == metadata.checksum:
                        return existing
                    # If version or status changed (e.g. rotation), allow update
                    if existing.version == metadata.version and existing.checksum != metadata.checksum:
                        raise SecretMetadataConflictError(
                            f"SecretMetadata '{metadata.reference_id}' already exists with different checksum for same version ({existing.checksum} vs {metadata.checksum})."
                        )
                except SecretMetadataConflictError:
                    raise
                except Exception as e:
                    raise CorruptedSecretMetadataRecordError(
                        f"Cannot verify existing metadata '{metadata.reference_id}': {e}"
                    ) from e

            self._atomic_write_json(meta_file, metadata.to_dict())
            self._rebuild_index_unlocked()
            return metadata

    def get_metadata(self, reference_id: str) -> Optional[SecretMetadata]:
        validate_safe_identifier(reference_id, field_name="reference_id")

        with self._exclusive_lock():
            meta_file = self.secrets_dir / f"{reference_id}.json"
            if not meta_file.exists():
                return None
            return self._load_metadata_file(meta_file)

    def find_by_name(self, provider: str, secret_name: str) -> Optional[SecretMetadata]:
        norm_prov = provider.strip().lower()
        norm_name = secret_name.strip()

        with self._exclusive_lock():
            if not self.secrets_index_file.exists():
                return None

            matched_id = None
            with open(self.secrets_index_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if (
                        entry.get("provider", "").lower() == norm_prov
                        and entry.get("secret_name", "") == norm_name
                    ):
                        matched_id = entry.get("reference_id")

            if matched_id:
                meta_file = self.secrets_dir / f"{matched_id}.json"
                if meta_file.exists():
                    return self._load_metadata_file(meta_file)

            return None

    def list_metadata(
        self,
        provider: Optional[str] = None,
        secret_type: Optional[SecretType] = None,
        status: Optional[SecretStatus] = None,
        limit: int = 100,
    ) -> Sequence[SecretMetadata]:
        with self._exclusive_lock():
            metadata_map: Dict[str, SecretMetadata] = {}
            for fpath in sorted(self.secrets_dir.glob("*.json")):
                m = self.get_metadata(fpath.stem)
                if m:
                    metadata_map[m.reference_id] = m

            results = []
            for m in metadata_map.values():
                if provider and m.provider.lower() != provider.lower():
                    continue
                if secret_type and m.secret_type != secret_type:
                    continue
                if status and m.status != status:
                    continue
                results.append(m)
                if len(results) >= limit:
                    break

            return tuple(results)

    def delete_metadata(self, reference_id: str) -> bool:
        validate_safe_identifier(reference_id, field_name="reference_id")
        with self._exclusive_lock():
            meta_file = self.secrets_dir / f"{reference_id}.json"
            if not meta_file.exists():
                return False
            meta_file.unlink()
            self._rebuild_index_unlocked()
            return True
