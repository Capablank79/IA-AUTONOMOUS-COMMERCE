"""
Puertos de dominio para SaaS Authorization y Multi-Tenant RBAC & Scoping (Hito O.4 — SaaS / Platformization).

Define:
- ResourceOwnershipResolverPort: Puerto opcional para resolver de manera confiable el tenant_id y organization_id propietario de un recurso.
- SaaSAuthorizationServicePort: Puerto para el servicio de evaluación y decisión de autorización SaaS.
"""

from abc import ABC, abstractmethod
from typing import Optional, Any, Mapping, Sequence, Tuple

from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationContext,
)


class ResourceOwnershipResolverPort(ABC):
    """
    Puerto para resolver la propiedad (tenant_id y organization_id) de un recurso de manera fidedigna.
    Previene spoofing mediante resource_ids conocidos asociados a tenants falsos.
    """

    @abstractmethod
    def resolve_resource_ownership(
        self,
        resource: Any,
    ) -> Optional[Tuple[str, Optional[str]]]:
        """
        Retorna (tenant_id, organization_id) para el recurso dado, o None si el recurso es desconocido.
        """
        pass


class SaaSAuthorizationServicePort(ABC):
    """
    Puerto de servicio para la evaluación y decisión de autorización SaaS multi-tenant.
    """

    @abstractmethod
    def authorize(
        self,
        request: SaaSAuthorizationRequest,
    ) -> SaaSAuthorizationDecision:
        """
        Evalúa de forma determinista la autorización SaaS según:
        Session (O.3) -> Tenant (O.1) -> Organization/Membership (O.2) -> RBAC (N.4) -> Authorization (N.3).
        """
        pass
