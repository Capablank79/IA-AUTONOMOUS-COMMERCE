"""
Módulo de dominio para Autorización (Hito N.3).
"""

from .models import (
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
    ActionReference,
    AuthorizationRequest,
    AuthorizationDecision,
    compute_authorization_checksum,
)

__all__ = [
    "AuthorizationStatus",
    "AuthorizationReasonCode",
    "ResourceReference",
    "ActionReference",
    "AuthorizationRequest",
    "AuthorizationDecision",
    "compute_authorization_checksum",
]
