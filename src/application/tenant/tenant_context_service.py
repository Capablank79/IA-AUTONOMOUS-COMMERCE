"""
Servicio de Aplicación para Gestión y Resolución de Contexto de Tenant (Hito O.1 — Tenant Isolation).

Responsabilidades:
- Implementar TenantResolverPort y TenantMappingPort.
- Validar identificadores y prevenir path traversal o identificadores inseguros.
- Mantener mappings explícitos entre Identidades, Cuentas de Marketplace y Tenants.
- Crear contextos inmutables y verificados (TenantContext).
- Fail-Safe: Rechazar contextos vacíos o tenant_ids desconocidos/mismatches.
"""

from datetime import datetime, timezone
import logging
from typing import Optional, Dict, Any, Mapping, Set

from src.domain.tenant.models import (
    TenantId,
    TenantContext,
    TenantReference,
    TenantResolutionStatus,
    TenantSecurityViolationError,
    CrossTenantAccessError,
)
from src.domain.tenant.ports import (
    TenantResolverPort,
    TenantMappingPort,
)
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


class TenantContextService(TenantResolverPort, TenantMappingPort):
    """
    Servicio central de resolución y mapeo seguro de Tenants.
    """

    def __init__(
        self,
        registered_tenants: Optional[Set[str]] = None,
        identity_to_tenant: Optional[Dict[str, str]] = None,
        marketplace_account_to_tenant: Optional[Dict[str, str]] = None,
    ):
        self._tenants: Set[str] = set(registered_tenants) if registered_tenants else set()
        self._identity_map: Dict[str, str] = dict(identity_to_tenant) if identity_to_tenant else {}
        self._marketplace_map: Dict[str, str] = dict(marketplace_account_to_tenant) if marketplace_account_to_tenant else {}

    def register_tenant(self, tenant_id: str, tenant_name: Optional[str] = None) -> TenantReference:
        """Registra un nuevo tenant en la plataforma."""
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        self._tenants.add(tenant_id)
        return TenantReference(
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            created_at=datetime.now(timezone.utc),
        )

    def validate_tenant_exists(self, tenant_id: str) -> bool:
        """Verifica si un tenant está registrado."""
        if not tenant_id or not isinstance(tenant_id, str):
            return False
        return tenant_id in self._tenants

    def bind_identity(self, tenant_id: str, identity_id: str) -> None:
        """Asocia una identidad de actor a un tenant."""
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        validate_safe_identifier(identity_id, field_name="identity_id")
        if self._tenants and tenant_id not in self._tenants:
            self._tenants.add(tenant_id)
        self._identity_map[identity_id] = tenant_id

    def get_tenant_for_identity(self, identity_id: str) -> Optional[str]:
        """Obtiene el tenant_id vinculado a una identidad."""
        if not identity_id:
            return None
        return self._identity_map.get(identity_id)

    def bind_marketplace_account(self, tenant_id: str, marketplace_account_id: str) -> None:
        """Asocia una cuenta de marketplace (MercadoLibre, etc.) a un tenant."""
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        validate_safe_identifier(marketplace_account_id, field_name="marketplace_account_id")
        if self._tenants and tenant_id not in self._tenants:
            self._tenants.add(tenant_id)
        self._marketplace_map[marketplace_account_id] = tenant_id

    def get_tenant_for_marketplace_account(self, marketplace_account_id: str) -> Optional[str]:
        """Obtiene el tenant_id propietario de una cuenta de marketplace."""
        if not marketplace_account_id:
            return None
        return self._marketplace_map.get(marketplace_account_id)

    def resolve_context(
        self,
        tenant_id: str,
        identity_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        marketplace_account_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TenantContext:
        """
        Resuelve y valida un TenantContext explícito.

        Validaciones:
        - Si identity_id está provisto y registrado en otro tenant -> CrossTenantAccessError.
        - Si marketplace_account_id está provisto y registrado en otro tenant -> CrossTenantAccessError.
        """
        validate_safe_identifier(tenant_id, field_name="tenant_id")

        if identity_id:
            bound_tenant = self.get_tenant_for_identity(identity_id)
            if bound_tenant and bound_tenant != tenant_id:
                raise CrossTenantAccessError(
                    f"Identity '{identity_id}' belongs to tenant '{bound_tenant}', cannot create context for tenant '{tenant_id}'."
                )

        if marketplace_account_id:
            bound_tenant = self.get_tenant_for_marketplace_account(marketplace_account_id)
            if bound_tenant and bound_tenant != tenant_id:
                raise CrossTenantAccessError(
                    f"Marketplace account '{marketplace_account_id}' belongs to tenant '{bound_tenant}', cannot bind to tenant '{tenant_id}'."
                )

        if tenant_id not in self._tenants:
            self._tenants.add(tenant_id)

        return TenantContext(
            tenant_id=tenant_id,
            identity_id=identity_id,
            correlation_id=correlation_id,
            marketplace_account_id=marketplace_account_id,
            metadata=metadata or {},
        )
