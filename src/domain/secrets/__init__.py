"""
Dominio de Gestión de Secretos (Hito N.5 — Secret Management).
"""

from .models import (
    SecretType,
    SecretStatus,
    SecretResolutionStatus,
    SecretValue,
    SecretMetadata,
    SecretReference,
    SecretResolutionResult,
    compute_secret_metadata_checksum,
)
from .ports import (
    SecretProviderPort,
    SecretResolverPort,
    SecretMetadataRepositoryPort,
)

__all__ = [
    "SecretType",
    "SecretStatus",
    "SecretResolutionStatus",
    "SecretValue",
    "SecretMetadata",
    "SecretReference",
    "SecretResolutionResult",
    "compute_secret_metadata_checksum",
    "SecretProviderPort",
    "SecretResolverPort",
    "SecretMetadataRepositoryPort",
]
