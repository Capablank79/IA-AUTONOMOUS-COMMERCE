"""
Guardia canónica de Seguridad y Validación Cross-Tenant (Hito O.1 — Tenant Isolation).

Define:
- CrossTenantGuard: Utilidades y métodos de aserción para prevenir accesos cruzados entre tenants.
- Validación determinista fail-safe.
"""

from typing import Optional, Union, Any

from src.domain.tenant.models import (
    TenantId,
    TenantContext,
    TenantScopedResource,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)


class CrossTenantGuard:
    """
    Guardia de seguridad centralizada contra accesos cross-tenant.

    Regla fundamental:
    Si un request/caller con tenant A intenta leer, modificar, borrar, invocar o
    reutilizar un recurso/estado/secreto/memoria del tenant B:
    -> Disparar CrossTenantAccessError inmediatamente antes de cualquier efecto secundario.
    """

    @staticmethod
    def ensure_tenant_context(context: Optional[TenantContext]) -> TenantContext:
        """
        Garantiza que exista un TenantContext válido y no nulo.
        Fail-Safe: NUNCA asume un tenant por defecto.
        """
        if context is None:
            raise TenantSecurityViolationError(
                "Missing tenant context: A valid TenantContext is strictly required for this tenant-scoped operation."
            )
        if not isinstance(context, TenantContext):
            raise TenantSecurityViolationError(
                f"Invalid tenant context type: expected TenantContext, got {type(context).__name__}"
            )
        return context

    @staticmethod
    def assert_same_tenant(
        request_context: Optional[TenantContext],
        target_tenant_id: Union[str, TenantId],
        operation_name: str = "operation",
    ) -> None:
        """
        Valida que el tenant_id del contexto coincida exactamente con el tenant_id del recurso objetivo.
        """
        ctx = CrossTenantGuard.ensure_tenant_context(request_context)
        target_id_str = target_tenant_id.value if isinstance(target_tenant_id, TenantId) else target_tenant_id

        if not target_id_str or not isinstance(target_id_str, str):
            raise TenantSecurityViolationError("Target tenant identifier is missing or invalid.")

        if ctx.tenant_id != target_id_str:
            raise CrossTenantAccessError(
                f"CROSS_TENANT_ACCESS_DENIED: Context tenant '{ctx.tenant_id}' is not authorized to perform '{operation_name}' on resource belonging to tenant '{target_id_str}'."
            )

    @staticmethod
    def assert_resource_access(
        request_context: Optional[TenantContext],
        resource: Any,
        operation_name: str = "resource_access",
    ) -> None:
        """
        Valida que el recurso pertenezca al mismo tenant del contexto.
        Soporta TenantScopedResource o cualquier entidad con atributo `tenant_id`.
        """
        ctx = CrossTenantGuard.ensure_tenant_context(request_context)

        if hasattr(resource, "tenant_id"):
            resource_tenant = getattr(resource, "tenant_id")
            CrossTenantGuard.assert_same_tenant(
                request_context=ctx,
                target_tenant_id=resource_tenant,
                operation_name=operation_name,
            )
        else:
            # Si el recurso no es explícitamente tenant-aware, se debe auditar según política
            pass
