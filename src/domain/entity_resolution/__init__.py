"""
Módulo de dominio para Entity Resolution (Hito L.6).
"""

from .models import (
    EntityType,
    IdentifierType,
    MatchStatus,
    ResolutionReasonCode,
    EntityIdentifier,
    EntityReference,
    EntityResolutionPolicy,
    EntityResolutionResult,
    ResolvedEntity,
    normalize_text,
    normalize_identifier_value,
    build_deterministic_canonical_entity_id,
    compute_entity_reference_checksum,
    compute_resolution_policy_checksum,
    compute_resolution_result_checksum,
    compute_resolution_input_fingerprint,
    compute_resolved_entity_checksum,
)
from .ports import (
    EntityResolutionPolicyRepositoryPort,
    EntityResolutionRepositoryPort,
)

__all__ = [
    "EntityType",
    "IdentifierType",
    "MatchStatus",
    "ResolutionReasonCode",
    "EntityIdentifier",
    "EntityReference",
    "EntityResolutionPolicy",
    "EntityResolutionResult",
    "ResolvedEntity",
    "normalize_text",
    "normalize_identifier_value",
    "build_deterministic_canonical_entity_id",
    "compute_entity_reference_checksum",
    "compute_resolution_policy_checksum",
    "compute_resolution_result_checksum",
    "compute_resolution_input_fingerprint",
    "compute_resolved_entity_checksum",
    "EntityResolutionPolicyRepositoryPort",
    "EntityResolutionRepositoryPort",
]
