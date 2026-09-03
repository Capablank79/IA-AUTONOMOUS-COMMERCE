"""
Aplicación de Duplicate Detection (Hito L.7 - Transversal Data Quality / Governance).
"""

from .service import (
    DuplicateDetectionService,
    create_default_product_dedup_policy,
    create_default_replay_policy,
)

__all__ = [
    "DuplicateDetectionService",
    "create_default_product_dedup_policy",
    "create_default_replay_policy",
]
