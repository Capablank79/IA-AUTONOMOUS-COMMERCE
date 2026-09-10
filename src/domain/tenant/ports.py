"""
Puertos de dominio para Aislamiento y Resolución de Tenants (Hito O.1 — Tenant Isolation).

Define:
- TenantResolverPort: Puerto para resolver y validar contextos de tenant a partir de peticiones o identificadores.
- TenantScopedRepositoryPort: Puerto genérico para repositorios que requieren estricto aislamiento por tenant.
- TenantMappingPort: Puerto para resolver el mapeo seguro entre identidades, marketplace accounts y tenants.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, TypeVar, Generic, Sequence

from src.domain.tenant.models import (
    TenantId,
    TenantContext,
    TenantReference,
    TenantScopedResource,
    TenantResolutionStatus,
)

T = TypeVar("T")


class TenantResolverPort(ABC):
    """
    Puerto para la validación y resolución segura de contextos de Tenant.
    """

    @abstractmethod
    def resolve_context(
        self,
        tenant_id: str,
        identity_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        marketplace_account_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TenantContext:
        """Resuelve y valida un TenantContext explícito."""
        pass

    @abstractmethod
    def validate_tenant_exists(self, tenant_id: str) -> bool:
        """Comprueba si un tenant_id está registrado o es válido."""
        pass


class TenantMappingPort(ABC):
    """
    Puerto para la gestión y consulta de relaciones explícitas:
    - Identity -> Tenant
    - Marketplace Account -> Tenant
    """

    @abstractmethod
    def get_tenant_for_marketplace_account(self, marketplace_account_id: str) -> Optional[str]:
        """Obtiene el tenant_id propietario de una cuenta de marketplace."""
        pass

    @abstractmethod
    def get_tenant_for_identity(self, identity_id: str) -> Optional[str]:
        """Obtiene el tenant_id asociado a una identidad canónica."""
        pass

    @abstractmethod
    def bind_marketplace_account(self, tenant_id: str, marketplace_account_id: str) -> None:
        """Asocia una cuenta de marketplace a un tenant."""
        pass

    @abstractmethod
    def bind_identity(self, tenant_id: str, identity_id: str) -> None:
        """Asocia una identidad a un tenant."""
        pass


class TenantScopedRepositoryPort(Generic[T], ABC):
    """
    Puerto de repositorio genérico aislado por Tenant.
    Todas las operaciones requieren obligatoriamente TenantContext.
    """

    @abstractmethod
    def save(self, context: TenantContext, resource: T) -> None:
        """Guarda o actualiza un recurso en el contexto exclusivo del tenant."""
        pass

    @abstractmethod
    def get_by_id(self, context: TenantContext, resource_id: str) -> Optional[T]:
        """Obtiene un recurso por su ID asegurando que pertenezca al tenant del contexto."""
        pass

    @abstractmethod
    def list_all(self, context: TenantContext) -> List[T]:
        """Lista todos los recursos pertenecientes al tenant del contexto."""
        pass

    @abstractmethod
    def delete(self, context: TenantContext, resource_id: str) -> bool:
        """Elimina un recurso asegurando aislamiento por tenant."""
        pass
