"""
Módulo del dominio de Identidad Canónica (Hito N.1 - Transversal N Security, Governance y Safety).
"""

from src.domain.identity.models import (
    Identity,
    IdentityType,
    IdentityStatus,
    IdentityReference,
    PrincipalIdentity,
    build_canonical_identifier,
    compute_identity_checksum,
    audit_actor_to_identity_reference,
    identity_to_audit_actor,
    agent_trace_to_identity_reference,
    create_oauth_user_identity,
)
from src.domain.identity.ports import IdentityRepositoryPort

__all__ = [
    "Identity",
    "IdentityType",
    "IdentityStatus",
    "IdentityReference",
    "PrincipalIdentity",
    "IdentityRepositoryPort",
    "build_canonical_identifier",
    "compute_identity_checksum",
    "audit_actor_to_identity_reference",
    "identity_to_audit_actor",
    "agent_trace_to_identity_reference",
    "create_oauth_user_identity",
]
