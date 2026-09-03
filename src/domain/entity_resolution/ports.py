"""
Puertos de dominio para Entity Resolution (Hito L.6).
"""

from typing import Optional, Protocol, Sequence, Union
from .models import (
    EntityType,
    EntityResolutionPolicy,
    EntityResolutionResult,
    ResolvedEntity,
    EntityReference,
)


class EntityResolutionPolicyRepositoryPort(Protocol):
    """Contrato de persistencia y consulta de políticas de resolución versionadas."""

    def save_policy(self, policy: EntityResolutionPolicy) -> EntityResolutionPolicy:
        """Guarda o actualiza una política de resolución de forma inmutable e idempotente."""
        ...

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[EntityResolutionPolicy]:
        """Obtiene una política por ID y versión opcional (o la última versión si no se especifica)."""
        ...

    def get_latest_policy_for_entity_type(
        self,
        entity_type: Union[EntityType, str],
    ) -> Optional[EntityResolutionPolicy]:
        """Obtiene la última política activa para un tipo de entidad."""
        ...

    def list_policies(
        self,
        entity_type: Optional[Union[EntityType, str]] = None,
    ) -> Sequence[EntityResolutionPolicy]:
        """Lista las políticas registradas."""
        ...


class EntityResolutionRepositoryPort(Protocol):
    """Contrato de persistencia y consulta de resultados de resolución y entidades canónicas."""

    def save_resolution(self, result: EntityResolutionResult) -> EntityResolutionResult:
        """Persiste un resultado de resolución de forma inmutable y atómica."""
        ...

    def get_resolution(self, resolution_id: str) -> Optional[EntityResolutionResult]:
        """Recupera un resultado de resolución por su identificador."""
        ...

    def find_resolutions_by_reference(
        self,
        source_id: str,
        source_entity_id: str,
    ) -> Sequence[EntityResolutionResult]:
        """Encuentra todas las resoluciones que involucran a una referencia dada."""
        ...

    def save_canonical_entity(self, entity: ResolvedEntity) -> ResolvedEntity:
        """Persiste o actualiza una entidad canónica resuelta."""
        ...

    def get_canonical_entity(self, canonical_entity_id: str) -> Optional[ResolvedEntity]:
        """Recupera una entidad canónica por su identificador único."""
        ...

    def find_canonical_by_reference(
        self,
        source_id: str,
        source_entity_id: str,
    ) -> Optional[ResolvedEntity]:
        """Encuentra la entidad canónica que contiene la referencia dada como miembro."""
        ...

    def list_canonical_entities(
        self,
        entity_type: Optional[Union[EntityType, str]] = None,
    ) -> Sequence[ResolvedEntity]:
        """Lista entidades canónicas registradas."""
        ...
