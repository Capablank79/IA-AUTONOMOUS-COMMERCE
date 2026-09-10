"""
Exportaciones de dominio para SaaS Authorization y Multi-Tenant RBAC & Scoping (Hito O.4).
"""

from src.domain.saas_authorization.models import (
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
    SaaSAuthorizationError,
    SaaSAuthorizationDeniedError,
    SaaSAuthorizationSecurityViolationError,
    SaaSAuthorizationRequest,
    SaaSAuthorizationContext,
    SaaSAuthorizationDecision,
    compute_saas_authorization_context_checksum,
    compute_saas_authorization_decision_checksum,
)
from src.domain.saas_authorization.ports import (
    ResourceOwnershipResolverPort,
    SaaSAuthorizationServicePort,
)

__all__ = [
    "SaaSAuthorizationStatus",
    "SaaSAuthorizationReasonCode",
    "SaaSAuthorizationError",
    "SaaSAuthorizationDeniedError",
    "SaaSAuthorizationSecurityViolationError",
    "SaaSAuthorizationRequest",
    "SaaSAuthorizationContext",
    "SaaSAuthorizationDecision",
    "compute_saas_authorization_context_checksum",
    "compute_saas_authorization_decision_checksum",
    "ResourceOwnershipResolverPort",
    "SaaSAuthorizationServicePort",
]
