"""
Repositorio en memoria para Caching de Inferencia (Hito M.4).

Transversal M — Control de Coste e Inferencia.
"""

from typing import Optional, Dict
import threading

from src.domain.caching.models import CacheEntry
from src.domain.caching.ports import CacheRepositoryPort


class InMemoryCacheRepository(CacheRepositoryPort):
    """
    Implementación en memoria thread-safe de CacheRepositoryPort.
    Útil para tests rápidos, ejecuciones efímeras y fallback local.
    """

    def __init__(self):
        self._entries: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()

    def get_by_key(self, cache_key: str) -> Optional[CacheEntry]:
        with self._lock:
            return self._entries.get(cache_key)

    def save(self, entry: CacheEntry) -> None:
        with self._lock:
            self._entries[entry.cache_key] = entry

    def delete(self, cache_key: str) -> bool:
        with self._lock:
            return self._entries.pop(cache_key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._entries)
