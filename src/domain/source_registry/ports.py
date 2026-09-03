"""
Puertos de dominio para Source Registry (Hito L.1).

Define:
- SourceRegistryRepositoryPort: Contrato para almacenamiento y consulta de fuentes registradas.
"""

from typing import Protocol, Optional, Sequence, List
from .models import RegisteredSource, SourceType, SourceStatus


class SourceRegistryRepositoryPort(Protocol):
    """
    Puerto de repositorio para la persistencia e indexación de fuentes registradas.
    """

    def save_source(self, source: RegisteredSource) -> RegisteredSource:
        """
        Persiste una fuente registrada de forma atómica e idempotente.
        Lanza excepción ante colisión/conflicto de contenido bajo la misma identidad/versión.
        """
        ...

    def get_source(self, source_id: str, version: Optional[str] = None) -> Optional[RegisteredSource]:
        """
        Obtiene una fuente por su source_id y versión específica.
        Si version es None, obtiene la versión más reciente (por semver o fecha).
        """
        ...

    def find_by_canonical_identifier(self, canonical_identifier: str) -> Optional[RegisteredSource]:
        """
        Busca una fuente registrada mediante su canonical_identifier exacto.
        """
        ...

    def list_sources(
        self,
        source_type: Optional[SourceType] = None,
        provider: Optional[str] = None,
        status: Optional[SourceStatus] = None,
        limit: int = 100,
    ) -> Sequence[RegisteredSource]:
        """
        Lista fuentes registradas aplicando filtros opcionales.
        """
        ...

    def exists(self, source_id: str, version: Optional[str] = None) -> bool:
        """
        Verifica si existe una fuente registrada con el source_id (y versión opcional).
        """
        ...
