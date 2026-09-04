"""
Módulo de Dominio para Caching de Inferencia (Hito M.4).

Transversal M — Control de Coste e Inferencia.
"""

from .models import (
    CacheLookupStatus,
    CacheEvictionReason,
    CachePolicy,
    CacheEntry,
    CacheLookupRequest,
    CacheLookupResult,
    CacheStoreRequest,
    compute_request_fingerprint,
    compute_cache_key,
    compute_cache_entry_checksum,
)
from .ports import (
    CacheRepositoryPort,
    InferenceCacheServicePort,
)

__all__ = [
    "CacheLookupStatus",
    "CacheEvictionReason",
    "CachePolicy",
    "CacheEntry",
    "CacheLookupRequest",
    "CacheLookupResult",
    "CacheStoreRequest",
    "compute_request_fingerprint",
    "compute_cache_key",
    "compute_cache_entry_checksum",
    "CacheRepositoryPort",
    "InferenceCacheServicePort",
]
