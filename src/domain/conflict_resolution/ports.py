"""
Puertos de dominio para Conflict Resolution (Hito L.8 - Transversal Data Quality / Governance).
"""

from typing import Optional, Protocol, Sequence
from .models import (
    ConflictResolutionPolicy,
    ConflictResolutionResult,
    ConflictCandidate,
)


class ConflictResolutionPolicyRepositoryPort(Protocol):
    """Contrato de persistencia y consulta de políticas de resolución de conflictos versionadas."""

    def save_policy(self, policy: ConflictResolutionPolicy) -> ConflictResolutionPolicy:
        """Guarda una política de resolución de conflictos de forma inmutable e idempotente."""
        ...

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[ConflictResolutionPolicy]:
        """Obtiene una política por ID y versión opcional (o la última si no se especifica)."""
        ...

    def list_policies(self) -> Sequence[ConflictResolutionPolicy]:
        """Lista las políticas registradas."""
        ...


class ConflictResolutionRepositoryPort(Protocol):
    """Contrato de persistencia y consulta de resultados de resolución de conflictos."""

    def save_result(self, result: ConflictResolutionResult) -> ConflictResolutionResult:
        """Persiste un resultado de resolución de forma inmutable y atómica."""
        ...

    def get_result(self, conflict_id: str) -> Optional[ConflictResolutionResult]:
        """Recupera un resultado de resolución por su conflict_id."""
        ...

    def find_results_by_entity(self, canonical_entity_id: str) -> Sequence[ConflictResolutionResult]:
        """Encuentra todos los resultados asociados a un canonical_entity_id."""
        ...

    def find_results_by_correlation(self, correlation_id: str) -> Sequence[ConflictResolutionResult]:
        """Encuentra resultados asociados a un correlation_id."""
        ...
