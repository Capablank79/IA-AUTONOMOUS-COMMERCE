"""
Módulo de dominio para Tenant Isolation (Hito O.1).
"""

from .models import (
    TenantId,
    TenantContext,
    TenantReference,
    TenantScope,
    TenantScopedResource,
    TenantResolutionStatus,
    CrossTenantAccessError,
    TenantSecurityViolationError,
    compute_tenant_context_checksum,
)
from .guard import CrossTenantGuard
from .ports import (
    TenantResolverPort,
    TenantMappingPort,
    TenantScopedRepositoryPort,
)

__all__ = [
    "TenantId",
    "TenantContext",
    "TenantReference",
    "TenantScope",
    "TenantScopedResource",
    "TenantResolutionStatus",
    "CrossTenantAccessError",
    "TenantSecurityViolationError",
    "compute_tenant_context_checksum",
    "CrossTenantGuard",
    "TenantResolverPort",
    "TenantMappingPort",
    "TenantScopedRepositoryPort",
]
