"""
Repositorio JSON persistente y atómico para Caching de Inferencia (Hito M.4).

Transversal M — Control de Coste e Inferencia.

Garantías:
- Atomic write (.tmp -> fsync -> os.replace).
- Integridad SHA-256 estricta en lectura y detección de corrupción física sin autorreparación silenciosa.
- Thread-safe mediante RLock de concurrencia.
- Recarga completa de índice en memoria tras reinicio.
- Path safety estricto (cache_key como identificador SHA-256 seguro).
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List
import threading

from src.domain.caching.models import (
    CacheEntry,
    CacheIntegrityError,
    compute_cache_entry_checksum,
)
from src.domain.caching.ports import CacheRepositoryPort
from src.domain.model_routing.models import sanitize_routing_data, deep_freeze, SENSITIVE_KEYS

logger = logging.getLogger(__name__)


class JsonCacheRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de caché de inferencia."""
    pass


class CorruptedCacheEntryError(CacheIntegrityError, JsonCacheRepositoryError):
    """Se lanza cuando los datos de una entrada de caché están corruptos o el checksum no coincide."""
    pass


def _encode_json_value(val: Any) -> Any:
    """Serializa estructuras a JSON de forma determinista y sanitiza claves sensibles recursivamente."""
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
    if isinstance(val, (list, tuple, set, frozenset)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonCacheRepository(CacheRepositoryPort):
    """
    Repositorio JSON atómico, persistente y seguro para CacheEntry (Hito M.4).
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.entries_dir = self.base_dir / "entries"
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self._memory_index: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()

        self._load_all_entries()

    def _get_entry_path(self, cache_key: str) -> Path:
        # cache_key es un hash hex sha256 seguro
        clean_key = "".join(c for c in cache_key if c.isalnum() or c in ("-", "_"))
        if not clean_key:
            raise ValueError("Invalid cache_key for filesystem path")
        return self.entries_dir / f"{clean_key}.json"

    def _load_all_entries(self) -> None:
        """Carga y valida todas las entradas existentes en disco al iniciar."""
        with self._lock:
            self._memory_index.clear()
            for json_file in self.entries_dir.glob("*.json"):
                if json_file.name.endswith(".tmp"):
                    continue
                try:
                    entry = self._read_entry_file(json_file)
                    self._memory_index[entry.cache_key] = entry
                except CorruptedCacheEntryError as e:
                    logger.error(f"Corrupted cache file ignored or quarantined: {json_file.name}: {e}")
                    # No autorreparar silenciosamente, no añadir a índice
                except Exception as e:
                    logger.error(f"Failed to read cache file {json_file.name}: {e}")

    def _entry_to_dict(self, entry: CacheEntry) -> Dict[str, Any]:
        """Convierte una CacheEntry a diccionario serializable."""
        return {
            "cache_key": entry.cache_key,
            "route_or_model_id": entry.route_or_model_id,
            "request_fingerprint": entry.request_fingerprint,
            "result_data": _encode_json_value(entry.result_data),
            "created_at": entry.created_at.isoformat(),
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
            "policy_id": entry.policy_id,
            "policy_version": entry.policy_version,
            "model_version": entry.model_version,
            "checksum": entry.checksum,
            "security_context_id": entry.security_context_id,
            "metadata": _encode_json_value(dict(entry.metadata)),
        }

    def _dict_to_entry(self, data: Dict[str, Any]) -> CacheEntry:
        """Reconstruye una CacheEntry y valida su checksum."""
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        expires_at = None
        if data.get("expires_at"):
            expires_at = datetime.fromisoformat(data["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

        entry = CacheEntry(
            cache_key=data["cache_key"],
            route_or_model_id=data["route_or_model_id"],
            request_fingerprint=data["request_fingerprint"],
            result_data=data["result_data"],
            created_at=created_at,
            expires_at=expires_at,
            policy_id=data.get("policy_id", "default_m4_cache_policy"),
            policy_version=data.get("policy_version", "1.0.0"),
            model_version=data.get("model_version"),
            checksum=data.get("checksum", ""),
            security_context_id=data.get("security_context_id"),
            metadata=data.get("metadata", {}),
        )

        if not entry.verify_checksum():
            raise CorruptedCacheEntryError(
                f"Tamper or corruption detected: Checksum mismatch for CacheEntry {entry.cache_key}"
            )

        return entry

    def _read_entry_file(self, path: Path) -> CacheEntry:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise CorruptedCacheEntryError(f"Failed to read cache file {path}: {e}")

        return self._dict_to_entry(data)

    def get_by_key(self, cache_key: str) -> Optional[CacheEntry]:
        with self._lock:
            # Primero intentar desde índice en memoria
            if cache_key in self._memory_index:
                return self._memory_index[cache_key]

            path = self._get_entry_path(cache_key)
            if not path.exists():
                return None

            entry = self._read_entry_file(path)
            self._memory_index[cache_key] = entry
            return entry

    def save(self, entry: CacheEntry) -> None:
        with self._lock:
            path = self._get_entry_path(entry.cache_key)
            tmp_path = path.with_suffix(".tmp")

            entry_dict = self._entry_to_dict(entry)
            json_data = json.dumps(entry_dict, indent=2, ensure_ascii=False)

            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json_data)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, path)
            self._memory_index[entry.cache_key] = entry

    def delete(self, cache_key: str) -> bool:
        with self._lock:
            self._memory_index.pop(cache_key, None)
            path = self._get_entry_path(cache_key)
            if path.exists():
                try:
                    path.unlink()
                    return True
                except Exception as e:
                    logger.error(f"Error deleting cache file {path}: {e}")
                    return False
            return False

    def clear(self) -> None:
        with self._lock:
            self._memory_index.clear()
            for json_file in self.entries_dir.glob("*.json"):
                try:
                    json_file.unlink()
                except Exception as e:
                    logger.error(f"Error clearing cache file {json_file}: {e}")

    def count(self) -> int:
        with self._lock:
            return len(self._memory_index)
