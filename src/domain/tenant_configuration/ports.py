"""Puertos para configuración por tenant O.11."""

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from .models import ConfigurationScope, TenantConfiguration


class TenantConfigurationRepositoryPort(ABC):
    @abstractmethod
    def save(self, configuration: TenantConfiguration) -> TenantConfiguration:
        pass

    @abstractmethod
    def get_latest(
        self, tenant_id: str, key: str, scope: ConfigurationScope = ConfigurationScope.TENANT,
        organization_id: Optional[str] = None,
    ) -> Optional[TenantConfiguration]:
        pass

    @abstractmethod
    def list_latest(
        self, tenant_id: str, scope: ConfigurationScope = ConfigurationScope.TENANT,
        organization_id: Optional[str] = None,
    ) -> Sequence[TenantConfiguration]:
        pass

    @abstractmethod
    def list_history(
        self, tenant_id: str, key: str, scope: ConfigurationScope = ConfigurationScope.TENANT,
        organization_id: Optional[str] = None,
    ) -> Sequence[TenantConfiguration]:
        pass
