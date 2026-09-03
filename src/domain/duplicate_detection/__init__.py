"""
Dominio de Duplicate Detection (Hito L.7 - Transversal Data Quality / Governance).
"""

from .models import (
    DuplicateStatus,
    DuplicateReasonCode,
    DuplicateCandidate,
    DuplicateDetectionPolicy,
    DuplicateDetectionResult,
    DuplicateGroup,
    normalize_value,
    compute_semantic_fingerprint,
    compute_duplicate_candidate_checksum,
    compute_duplicate_policy_checksum,
    compute_duplicate_result_checksum,
    compute_duplicate_group_checksum,
)
from .ports import (
    DuplicateDetectionPolicyRepositoryPort,
    DuplicateDetectionRepositoryPort,
)

__all__ = [
    "DuplicateStatus",
    "DuplicateReasonCode",
    "DuplicateCandidate",
    "DuplicateDetectionPolicy",
    "DuplicateDetectionResult",
    "DuplicateGroup",
    "normalize_value",
    "compute_semantic_fingerprint",
    "compute_duplicate_policy_checksum",
    "compute_duplicate_result_checksum",
    "compute_duplicate_group_checksum",
    "DuplicateDetectionPolicyRepositoryPort",
    "DuplicateDetectionRepositoryPort",
]
