"""
Domain package for Plans & Pricing Tiers (Hito O.8).
"""

from .models import (
    PlanManagementError,
    PlanNotFoundError,
    PlanVersionNotFoundError,
    PlanAssignmentNotFoundError,
    PlanIntegrityError,
    PlanTier,
    PlanStatus,
    PlanFeature,
    PlanLimits,
    PlanQuotaTemplate,
    Plan,
    PlanAssignmentStatus,
    PlanAssignment,
    PlanEntitlementStatus,
    PlanEntitlementRequest,
    PlanEntitlementDecision,
)
from .ports import (
    PlanCatalogRepositoryPort,
    PlanAssignmentRepositoryPort,
    PlanAuditPort,
    PlanEntitlementServicePort,
)

__all__ = [
    "PlanManagementError",
    "PlanNotFoundError",
    "PlanVersionNotFoundError",
    "PlanAssignmentNotFoundError",
    "PlanIntegrityError",
    "PlanTier",
    "PlanStatus",
    "PlanFeature",
    "PlanLimits",
    "PlanQuotaTemplate",
    "Plan",
    "PlanAssignmentStatus",
    "PlanAssignment",
    "PlanEntitlementStatus",
    "PlanEntitlementRequest",
    "PlanEntitlementDecision",
    "PlanCatalogRepositoryPort",
    "PlanAssignmentRepositoryPort",
    "PlanAuditPort",
    "PlanEntitlementServicePort",
]
