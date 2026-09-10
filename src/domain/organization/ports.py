"""
Puertos de dominio para Organizaciones y Membresías de Usuario (Hito O.2 — Organizations / Users).

Define:
- OrganizationRepositoryPort: Persistencia aislada y tenant-scoped de organizaciones.
- MembershipRepositoryPort: Persistencia aislada y tenant-scoped de membresías de usuario.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Sequence

from src.domain.organization.models import (
    Organization,
    UserMembership,
    OrganizationStatus,
    MembershipStatus,
)
from src.domain.tenant.models import TenantContext


class OrganizationRepositoryPort(ABC):
    """Puerto de repositorio para la gestión persistente de Organizaciones por Tenant."""

    @abstractmethod
    def save(self, context: TenantContext, organization: Organization) -> None:
        """Guarda o actualiza una organización en el contexto exclusivo del tenant."""
        pass

    @abstractmethod
    def get_by_id(self, context: TenantContext, organization_id: str) -> Optional[Organization]:
        """Obtiene una organización por su ID verificando pertenencia al tenant del contexto."""
        pass

    @abstractmethod
    def list_by_tenant(self, context: TenantContext) -> List[Organization]:
        """Lista todas las organizaciones pertenecientes exclusivamente al tenant del contexto."""
        pass

    @abstractmethod
    def delete(self, context: TenantContext, organization_id: str) -> bool:
        """Elimina una organización asegurando aislamiento estricto por tenant."""
        pass


class MembershipRepositoryPort(ABC):
    """Puerto de repositorio para la gestión persistente de Membresías de Usuario por Tenant."""

    @abstractmethod
    def save(self, context: TenantContext, membership: UserMembership) -> None:
        """Guarda o actualiza una membresía en el contexto exclusivo del tenant."""
        pass

    @abstractmethod
    def get_by_id(self, context: TenantContext, membership_id: str) -> Optional[UserMembership]:
        """Obtiene una membresía por su ID verificando pertenencia al tenant del contexto."""
        pass

    @abstractmethod
    def get_by_identity_and_org(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
    ) -> Optional[UserMembership]:
        """Obtiene la membresía de una identidad en una organización concreta del tenant."""
        pass

    @abstractmethod
    def list_by_organization(
        self,
        context: TenantContext,
        organization_id: str,
    ) -> List[UserMembership]:
        """Lista todas las membresías pertenecientes a una organización dentro del tenant."""
        pass

    @abstractmethod
    def list_by_identity(
        self,
        context: TenantContext,
        identity_id: str,
    ) -> List[UserMembership]:
        """Lista todas las membresías de una identidad dentro del tenant del contexto."""
        pass

    @abstractmethod
    def delete(self, context: TenantContext, membership_id: str) -> bool:
        """Elimina una membresía asegurando aislamiento estricto por tenant."""
        pass
