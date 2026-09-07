"""
Módulo de dominio para Autenticación (Hito N.2).
"""

from .models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationRequest,
    AuthenticationResult,
    PrincipalContext,
    compute_auth_result_checksum,
)

__all__ = [
    "AuthenticationMethod",
    "AuthenticationStatus",
    "AuthenticationRequest",
    "AuthenticationResult",
    "PrincipalContext",
    "compute_auth_result_checksum",
]
