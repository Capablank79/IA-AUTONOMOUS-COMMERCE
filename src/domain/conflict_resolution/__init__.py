"""
Domain package for Conflict Resolution (Hito L.8 - Transversal Data Quality / Governance).
"""

from .models import (
    ConflictStatus,
    ConflictReasonCode,
    ResolutionStrategy,
    ConflictCandidate,
    ConflictResolutionPolicy,
    ConflictResolutionResult,
    compute_candidate_checksum,
    compute_conflict_policy_checksum,
    compute_conflict_result_checksum,
    normalize_conflict_value,
)
from .ports import (
    ConflictResolutionPolicyRepositoryPort,
    ConflictResolutionRepositoryPort,
)

__all__ = [
    "ConflictStatus",
    "ConflictReasonCode",
    "ResolutionStrategy",
    "ConflictCandidate",
    "ConflictResolutionPolicy",
    "ConflictResolutionResult",
    "compute_candidate_checksum",
    "compute_conflict_policy_checksum",
    "compute_conflict_result_checksum",
    "normalize_conflict_value",
    "ConflictResolutionPolicyRepositoryPort",
    "ConflictResolutionRepositoryPort",
]
