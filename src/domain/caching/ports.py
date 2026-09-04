"""
Puertos de dominio para Caching de Inferencia (Hito M.4).

Transversal M — Control de Coste e Inferencia.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence, List

from src.domain.caching.models import (
    CacheEntry,
    CacheLookupRequest,
    CacheLookupResult,
    CachePolicy,
    CacheStoreRequest,
)


class CacheRepositoryPort(ABC):
    """
    Puerto de repositorio para persistencia y recuperación de entradas de caché de inferencia.
    """

    @abstractmethod
    def get_by_key(self, cache_key: str) -> Optional[CacheEntry]:
        """Recupera una entrada de caché por su clave canónica."""
        pass

    @abstractmethod
    def save(self, entry: CacheEntry) -> None:
        """Guarda o actualiza atómicamente una entrada en caché."""
        pass

    @abstractmethod
    def delete(self, cache_key: str) -> bool:
        """Elimina una entrada de caché por su clave canónica."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Limpia todo el almacenamiento de caché."""
        pass


class InferenceCacheServicePort(ABC):
    """
    Puerto primario del servicio de Caching de Inferencia (Hito M.4).
    """

    @abstractmethod
    def lookup(
        self,
        request: CacheLookupRequest,
        policy: Optional[CachePolicy] = None,
    ) -> CacheLookupResult:
        """
        Realiza una búsqueda determinista y segura en la caché de inferencia.
        """
        pass

    @abstractmethod
    def store(
        self,
        request: CacheStoreRequest,
        policy: Optional[CachePolicy] = None,
    ) -> Optional[CacheEntry]:
        """
        Evalúa la cacheabilidad y almacena de forma segura el resultado de inferencia.
        Retorna la CacheEntry almacenada o None si la política/estado impidió el guardado.
        """
        pass

    @abstractmethod
    def invalidate(self, cache_key: str) -> bool:
        """Invalida explícitamente una entrada de caché."""
        pass
