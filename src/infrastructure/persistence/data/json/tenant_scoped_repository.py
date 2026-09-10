"""
Adaptador de Repositorio Aislado por Tenant para Persistencia JSON (Hito O.1 — Tenant Isolation).

Responsabilidades:
- Implementar TenantScopedRepositoryPort sobre un layout físico particionado y protegido contra path traversal.
- Layout conceptual: `base_dir / "tenants" / tenant_safe_id / resource_type / resource_id.json`.
- Garantizar que Tenant A nunca pueda obtener, listar, actualizar ni borrar datos de Tenant B.
- Aplicar CrossTenantGuard antes de cualquier lectura, escritura o eliminación.
"""

import json
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

from src.domain.tenant.models import (
    TenantContext,
    TenantScopedResource,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.ports import TenantScopedRepositoryPort
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier


class JsonTenantScopedRepository(TenantScopedRepositoryPort[TenantScopedResource]):
    """
    Implementación JSON de repositorio particionado y aislado estrictamente por Tenant.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)

    def _get_tenant_path(self, tenant_id: str, resource_type: str) -> Path:
        """Calcula y valida la ruta física para el tenant y tipo de recurso."""
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        validate_safe_identifier(resource_type, field_name="resource_type")

        tenant_path = self.tenants_dir / tenant_id / resource_type
        tenant_path.mkdir(parents=True, exist_ok=True)
        return tenant_path

    def _get_file_path(self, tenant_id: str, resource_type: str, resource_id: str) -> Path:
        """Calcula la ruta completa a un archivo de recurso de tenant."""
        validate_safe_identifier(resource_id, field_name="resource_id")
        folder = self._get_tenant_path(tenant_id, resource_type)
        return folder / f"{resource_id}.json"

    def save(self, context: TenantContext, resource: TenantScopedResource) -> None:
        """
        Guarda un recurso en el almacén físico del tenant verificado en el contexto.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, resource.tenant_id, operation_name="save")

        file_path = self._get_file_path(
            tenant_id=context.tenant_id,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
        )

        data = resource.to_dict()
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_by_id(
        self,
        context: TenantContext,
        resource_type: str,
        resource_id: str,
    ) -> Optional[TenantScopedResource]:
        """
        Obtiene un recurso por su ID asegurando que pertenezca al tenant del contexto.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        file_path = self._get_file_path(
            tenant_id=context.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )

        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            res = TenantScopedResource(
                tenant_id=data["tenant_id"],
                resource_id=data["resource_id"],
                resource_type=data["resource_type"],
                payload=data.get("payload", {}),
                created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
            )
            # Verificación adicional por seguridad
            CrossTenantGuard.assert_same_tenant(context, res.tenant_id, operation_name="get_by_id")
            return res
        except Exception:
            return None

    def list_all(
        self,
        context: TenantContext,
        resource_type: str,
    ) -> List[TenantScopedResource]:
        """
        Lista todos los recursos de un tipo pertenecientes únicamente al tenant del contexto.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        folder = self._get_tenant_path(context.tenant_id, resource_type)
        results: List[TenantScopedResource] = []

        for json_file in folder.glob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                res = TenantScopedResource(
                    tenant_id=data["tenant_id"],
                    resource_id=data["resource_id"],
                    resource_type=data["resource_type"],
                    payload=data.get("payload", {}),
                    created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
                )
                if res.tenant_id == context.tenant_id:
                    results.append(res)
            except Exception:
                continue

        return results

    def delete(
        self,
        context: TenantContext,
        resource_type: str,
        resource_id: str,
    ) -> bool:
        """
        Elimina un recurso asegurando aislamiento por tenant.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        file_path = self._get_file_path(
            tenant_id=context.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )

        if not file_path.exists():
            return False

        file_path.unlink()
        return True
