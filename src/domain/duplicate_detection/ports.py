"""
Puertos de dominio para Duplicate Detection (Hito L.7).
"""

from typing import Optional, Protocol, Sequence, Union
from .models import (
    DuplicateDetectionPolicy,
    DuplicateDetectionResult,
    DuplicateGroup,
    DuplicateCandidate,
)


class DuplicateDetectionPolicyRepositoryPort(Protocol):
    """Contrato de persistencia y consulta de políticas de deduplicación versionadas."""

    def save_policy(self, policy: DuplicateDetectionPolicy) -> DuplicateDetectionPolicy:
        """Guarda una política de deduplicación de forma inmutable e idempotente."""
        ...

    def get_policy(self, policy_id: str, version: Optional[str] = None) -> Optional[DuplicateDetectionPolicy]:
        """Obtiene una política por ID y versión opcional (o la última si no se especifica)."""
        ...

    def list_policies(self) -> Sequence[DuplicateDetectionPolicy]:
        """Lista las políticas registradas."""
        ...


class DuplicateDetectionRepositoryPort(Protocol):
    """Contrato de persistencia y consulta de resultados de detección y grupos de duplicados."""

    def save_result(self, result: DuplicateDetectionResult) -> DuplicateDetectionResult:
        """Persiste un resultado de detección de forma inmutable y atómica."""
        ...

    def get_result(self, result_id: str) -> Optional[DuplicateDetectionResult]:
        """Recupera un resultado de detección por su identificador."""
        ...

    def find_results_by_record(self, record_id: str) -> Sequence[DuplicateDetectionResult]:
        """Encuentra todos los resultados asociados a un record_id."""
        ...

    def save_group(self, group: DuplicateGroup) -> DuplicateGroup:
        """Persiste o actualiza un grupo de duplicados."""
        ...

    def get_group(self, group_id: str) -> Optional[DuplicateGroup]:
        """Recupera un grupo de duplicados por ID."""
        ...

    def get_group_by_fingerprint(self, fingerprint: str) -> Optional[DuplicateGroup]:
        """Recupera un grupo de duplicados por su fingerprint canónico."""
        ...

    def list_groups(self) -> Sequence[DuplicateGroup]:
        """Lista grupos de duplicados registrados."""
        ...
