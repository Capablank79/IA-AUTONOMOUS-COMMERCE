"""
Módulo de dominio para SaaS Authentication y Gestión Multi-Tenant de Sesiones (Hito O.3 — SaaS / Platformization).
"""

from src.domain.session.models import (
    SessionStatus,
    SessionValidationReasonCode,
    SessionError,
    SessionNotFoundError,
    SessionValidationError,
    SessionExpiredError,
    SessionRevokedError,
    SessionTenantMismatchError,
    SessionSecurityViolationError,
    generate_secure_session_id,
    compute_session_checksum,
    compute_session_context_checksum,
    SessionReference,
    SessionContext,
    SaaSSession,
    SessionValidationResult,
)
from src.domain.session.ports import (
    SaaSSessionRepositoryPort,
    SaaSSessionServicePort,
)

__all__ = [
    "SessionStatus",
    "SessionValidationReasonCode",
    "SessionError",
    "SessionNotFoundError",
    "SessionValidationError",
    "SessionExpiredError",
    "SessionRevokedError",
    "SessionTenantMismatchError",
    "SessionSecurityViolationError",
    "generate_secure_session_id",
    "compute_session_checksum",
    "compute_session_context_checksum",
    "SessionReference",
    "SessionContext",
    "SaaSSession",
    "SessionValidationResult",
    "SaaSSessionRepositoryPort",
    "SaaSSessionServicePort",
]
