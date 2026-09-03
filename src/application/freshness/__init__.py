"""
Módulo de aplicación para Freshness / TTL (Hito L.3).
"""

from .service import (
    FreshnessService,
    FreshnessServiceError,
    PolicyNotFoundError,
)

__all__ = [
    "FreshnessService",
    "FreshnessServiceError",
    "PolicyNotFoundError",
]
