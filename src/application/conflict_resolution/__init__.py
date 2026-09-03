"""
Application package for Conflict Resolution (Hito L.8 - Transversal Data Quality / Governance).
"""

from .service import (
    ConflictResolutionService,
    create_default_source_priority_policy,
    create_default_freshness_policy,
    create_default_confidence_policy,
    create_default_consensus_policy,
)

__all__ = [
    "ConflictResolutionService",
    "create_default_source_priority_policy",
    "create_default_freshness_policy",
    "create_default_confidence_policy",
    "create_default_consensus_policy",
]
