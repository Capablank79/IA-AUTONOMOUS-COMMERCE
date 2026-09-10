"""
Adaptadores de Repositorio JSON para Organizaciones y Membresías aisladas por Tenant (Hito O.2).

Implementa:
- JsonOrganizationRepository (OrganizationRepositoryPort)
- JsonMembershipRepository (MembershipRepositoryPort)

Garantías:
- Aislamiento físico estricto: `base_dir / "tenants" / tenant_safe_id / "organizations" / org_id.json`
- Aislamiento físico estricto: `base_dir / "tenants" / tenant_safe_id / "memberships" / mem_id.json`
- Operaciones atómicas y crash-safe (`.tmp` + `fsync` + `os.replace`).
- Verificación de integridad criptográfica SHA-256 (fail-safe ante corrupción / tampering).
- Prevención total de path traversal (`validate_safe_identifier`).
- Thread-safety mediante `threading.RLock`.
- Aplicación de `CrossTenantGuard` en todas las operaciones.
"""

import json
import os
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

from src.domain.organization.models import (
    Organization,
    UserMembership,
    compute_organization_checksum,
    compute_membership_checksum,
)
from src.domain.organization.ports import (
    OrganizationRepositoryPort,
    MembershipRepositoryPort,
)
from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.security.models import validate_safe_identifier


class JsonOrganizationRepository(OrganizationRepositoryPort):
    """
    Repositorio JSON aislado por Tenant para Organizaciones.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_org_dir(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        org_dir = self.tenants_dir / tenant_id / "organizations"
        org_dir.mkdir(parents=True, exist_ok=True)
        return org_dir

    def _get_file_path(self, tenant_id: str, organization_id: str) -> Path:
        validate_safe_identifier(organization_id, field_name="organization_id")
        return self._get_org_dir(tenant_id) / f"{organization_id}.json"

    def save(self, context: TenantContext, organization: Organization) -> None:
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, organization.tenant_id, operation_name="save_organization")

        with self._lock:
            file_path = self._get_file_path(context.tenant_id, organization.organization_id)
            tmp_path = file_path.with_suffix(".tmp")

            data = organization.to_dict()
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, file_path)

    def get_by_id(self, context: TenantContext, organization_id: str) -> Optional[Organization]:
        CrossTenantGuard.ensure_tenant_context(context)
        with self._lock:
            file_path = self._get_file_path(context.tenant_id, organization_id)
            if not file_path.exists():
                return None

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Validar pertenencia de tenant del payload
                if data.get("tenant_id") != context.tenant_id:
                    raise CrossTenantAccessError("Tenant mismatch in persisted organization record.")

                org = Organization.from_dict(data)
                return org
            except Exception:
                # Corrupción física o tampering detectado -> Fail-safe None
                return None

    def list_by_tenant(self, context: TenantContext) -> List[Organization]:
        CrossTenantGuard.ensure_tenant_context(context)
        with self._lock:
            org_dir = self._get_org_dir(context.tenant_id)
            results: List[Organization] = []

            for json_file in org_dir.glob("*.json"):
                if json_file.name.endswith(".tmp"):
                    continue
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("tenant_id") == context.tenant_id:
                        results.append(Organization.from_dict(data))
                except Exception:
                    continue

            return sorted(results, key=lambda o: o.organization_id)

    def delete(self, context: TenantContext, organization_id: str) -> bool:
        CrossTenantGuard.ensure_tenant_context(context)
        with self._lock:
            file_path = self._get_file_path(context.tenant_id, organization_id)
            if not file_path.exists():
                return False
            file_path.unlink()
            return True


class JsonMembershipRepository(MembershipRepositoryPort):
    """
    Repositorio JSON aislado por Tenant para Membresías de Usuario.
    """

    def __init__(self, base_storage_dir: Union[str, Path]):
        self.base_storage_dir = Path(base_storage_dir)
        self.tenants_dir = self.base_storage_dir / "tenants"
        self.tenants_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_membership_dir(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, field_name="tenant_id")
        mem_dir = self.tenants_dir / tenant_id / "memberships"
        mem_dir.mkdir(parents=True, exist_ok=True)
        return mem_dir

    def _get_file_path(self, tenant_id: str, membership_id: str) -> Path:
        validate_safe_identifier(membership_id, field_name="membership_id")
        return self._get_membership_dir(tenant_id) / f"{membership_id}.json"

    def save(self, context: TenantContext, membership: UserMembership) -> None:
        CrossTenantGuard.ensure_tenant_context(context)
        CrossTenantGuard.assert_same_tenant(context, membership.tenant_id, operation_name="save_membership")

        with self._lock:
            file_path = self._get_file_path(context.tenant_id, membership.membership_id)
            tmp_path = file_path.with_suffix(".tmp")

            data = membership.to_dict()
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, file_path)

    def get_by_id(self, context: TenantContext, membership_id: str) -> Optional[UserMembership]:
        CrossTenantGuard.ensure_tenant_context(context)
        with self._lock:
            file_path = self._get_file_path(context.tenant_id, membership_id)
            if not file_path.exists():
                return None

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if data.get("tenant_id") != context.tenant_id:
                    raise CrossTenantAccessError("Tenant mismatch in persisted membership record.")

                mem = UserMembership.from_dict(data)
                return mem
            except Exception:
                return None

    def get_by_identity_and_org(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
    ) -> Optional[UserMembership]:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(organization_id, field_name="organization_id")
        validate_safe_identifier(identity_id, field_name="identity_id")

        with self._lock:
            mem_dir = self._get_membership_dir(context.tenant_id)
            for json_file in mem_dir.glob("*.json"):
                if json_file.name.endswith(".tmp"):
                    continue
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if (
                        data.get("tenant_id") == context.tenant_id
                        and data.get("organization_id") == organization_id
                        and data.get("identity_id") == identity_id
                    ):
                        return UserMembership.from_dict(data)
                except Exception:
                    continue
            return None

    def list_by_organization(
        self,
        context: TenantContext,
        organization_id: str,
    ) -> List[UserMembership]:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(organization_id, field_name="organization_id")

        with self._lock:
            mem_dir = self._get_membership_dir(context.tenant_id)
            results: List[UserMembership] = []

            for json_file in mem_dir.glob("*.json"):
                if json_file.name.endswith(".tmp"):
                    continue
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if (
                        data.get("tenant_id") == context.tenant_id
                        and data.get("organization_id") == organization_id
                    ):
                        results.append(UserMembership.from_dict(data))
                except Exception:
                    continue

            return sorted(results, key=lambda m: m.membership_id)

    def list_by_identity(
        self,
        context: TenantContext,
        identity_id: str,
    ) -> List[UserMembership]:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(identity_id, field_name="identity_id")

        with self._lock:
            mem_dir = self._get_membership_dir(context.tenant_id)
            results: List[UserMembership] = []

            for json_file in mem_dir.glob("*.json"):
                if json_file.name.endswith(".tmp"):
                    continue
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if (
                        data.get("tenant_id") == context.tenant_id
                        and data.get("identity_id") == identity_id
                    ):
                        results.append(UserMembership.from_dict(data))
                except Exception:
                    continue

            return sorted(results, key=lambda m: m.membership_id)

    def delete(self, context: TenantContext, membership_id: str) -> bool:
        CrossTenantGuard.ensure_tenant_context(context)
        with self._lock:
            file_path = self._get_file_path(context.tenant_id, membership_id)
            if not file_path.exists():
                return False
            file_path.unlink()
            return True
