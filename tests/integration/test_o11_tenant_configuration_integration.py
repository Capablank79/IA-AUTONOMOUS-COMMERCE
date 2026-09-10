import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from src.application.admin_console.admin_console_service import AdminConsoleService
from src.application.tenant_configuration.tenant_configuration_service import TenantConfigurationService
from src.domain.admin_console.models import (
    AdminAction,
    AdminAuthenticationError,
    AdminAuthorizationError,
)
from src.domain.model_gateway.models import TenantModelConfig
from src.domain.rbac.models import Permission, Role, RoleAssignment, PermissionSet
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.tenant_configuration.models import (
    ConfigurationIntegrityError,
    ConfigurationScope,
    ConfigurationVersionConflictError,
    ConfigurationValidationError,
)
from src.infrastructure.persistence.data.json.tenant_configuration_repository import JsonTenantConfigurationRepository


class FakeSessionRepo:
    def __init__(self, sessions):
        self.sessions = {s.session_id: s for s in sessions}

    def get_by_id(self, sid):
        return self.sessions.get(sid)


class FakeRBACService:
    def __init__(self, permissions_map):
        self.permissions_map = permissions_map

    def evaluate_effective_permissions(self, identity_id, **kwargs):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class MockEval:
            effective_permissions: PermissionSet

        perms = self.permissions_map.get(identity_id, set())
        perm_objs = tuple(Permission(permission_id=p, action=p) for p in perms)
        return MockEval(effective_permissions=PermissionSet(permissions=perm_objs))


def test_filesystem_repository_survives_restart_and_partitions_tenants_and_orgs(tmp_path):
    repo = JsonTenantConfigurationRepository(tmp_path)
    svc = TenantConfigurationService(repo)
    tenant = svc.set_configuration("tenant-a", "branding.display_name", "Alpha", "admin")
    org = svc.set_configuration(
        "tenant-a", "branding.theme", "dark", "admin",
        scope=ConfigurationScope.ORGANIZATION, organization_id="org-a",
    )
    restarted = TenantConfigurationService(JsonTenantConfigurationRepository(tmp_path))
    assert restarted.get_configuration("tenant-a", "branding.display_name") == tenant
    assert restarted.get_configuration(
        "tenant-a", "branding.theme", ConfigurationScope.ORGANIZATION, "org-a"
    ) == org
    assert restarted.get_configuration("tenant-b", "branding.display_name") is None
    assert (tmp_path / "tenants" / "tenant-a" / "configuration" / "tenant").is_dir()
    assert (tmp_path / "tenants" / "tenant-a" / "configuration" / "organizations" / "org-a").is_dir()
    assert not list(tmp_path.rglob("*.tmp"))


def test_checksum_corruption_is_fail_safe(tmp_path):
    svc = TenantConfigurationService(JsonTenantConfigurationRepository(tmp_path))
    svc.set_configuration("tenant-a", "branding.display_name", "Alpha", "admin")
    path = next((tmp_path / "tenants" / "tenant-a").rglob("*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["value"] = "Tampered"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationIntegrityError):
        JsonTenantConfigurationRepository(tmp_path).get_latest("tenant-a", "branding.display_name")


def test_safe_ids_block_path_traversal(tmp_path):
    svc = TenantConfigurationService(JsonTenantConfigurationRepository(tmp_path))
    with pytest.raises(ValueError):
        svc.set_configuration("../escape", "branding.theme", "dark", "admin")
    with pytest.raises(ValueError):
        svc.set_configuration(
            "tenant-a", "branding.theme", "dark", "admin",
            scope=ConfigurationScope.ORGANIZATION, organization_id="../escape",
        )


def test_concurrent_writers_allow_only_one_version(tmp_path):
    svc = TenantConfigurationService(JsonTenantConfigurationRepository(tmp_path))
    svc.set_configuration("tenant-a", "branding.theme", "system", "admin", expected_version=0)

    def update(value):
        try:
            return svc.set_configuration(
                "tenant-a", "branding.theme", value, "admin", expected_version=1
            ).version.value
        except ConfigurationVersionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ("dark", "light")))
    assert sorted(results, key=str) == [2, "conflict"]
    assert len(svc.get_history("tenant-a", "branding.theme")) == 2


class FakeAuthService:
    def __init__(self, allowed_actions_map):
        self.allowed_actions_map = allowed_actions_map

    def authorize(self, request):
        from src.domain.saas_authorization.models import (
            SaaSAuthorizationDecision,
            SaaSAuthorizationStatus,
            SaaSAuthorizationReasonCode,
            SaaSAuthorizationContext,
        )
        allowed = self.allowed_actions_map.get(request.identity_id, set())
        is_allowed = (request.action in allowed or "ALL" in allowed)
        ctx = SaaSAuthorizationContext(
            session_id=request.session_id,
            identity_id=request.identity_id,
            tenant_id=request.tenant_id,
            organization_id=request.organization_id,
            action=request.action,
            resolved_permissions=tuple(sorted(allowed)),
        )
        if is_allowed:
            return SaaSAuthorizationDecision(
                decision_id="dec-1",
                status=SaaSAuthorizationStatus.ALLOW,
                reason_code=SaaSAuthorizationReasonCode.AUTHORIZED,
                context=ctx,
            )
        return SaaSAuthorizationDecision(
            decision_id="dec-2",
            status=SaaSAuthorizationStatus.DENY,
            reason_code=SaaSAuthorizationReasonCode.INSUFFICIENT_PERMISSIONS,
            context=ctx,
        )


def test_admin_console_integration_and_rbac_enforcement(tmp_path):
    repo = JsonTenantConfigurationRepository(tmp_path)
    cfg_svc = TenantConfigurationService(repo)

    now = datetime.now(timezone.utc)
    from datetime import timedelta
    session_admin = SaaSSession(
        session_id="sess-admin",
        identity_id="user-admin",
        tenant_id="tenant-a",
        status=SessionStatus.ACTIVE,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    session_readonly = SaaSSession(
        session_id="sess-ro",
        identity_id="user-ro",
        tenant_id="tenant-a",
        status=SessionStatus.ACTIVE,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    session_repo = FakeSessionRepo([session_admin, session_readonly])
    auth_svc = FakeAuthService({
        "user-admin": {"TENANT_CONFIG_READ", "TENANT_CONFIG_MANAGE"},
        "user-ro": {"TENANT_CONFIG_READ"},
    })

    admin_console = AdminConsoleService(
        session_repository=session_repo,
        authorization_service=auth_svc,
        tenant_configuration_service=cfg_svc,
    )

    # 1. Update via Admin Console
    updated = admin_console.update_tenant_configuration(
        session_id="sess-admin",
        target_tenant_id="tenant-a",
        key="branding.display_name",
        value="Enterprise Brand",
    )
    assert updated.value.value == "Enterprise Brand"

    # 2. Read via Admin Console (Read-only user)
    effective = admin_console.get_tenant_configuration(
        session_id="sess-ro",
        target_tenant_id="tenant-a",
    )
    assert effective.effective_values["branding.display_name"] == "Enterprise Brand"

    # 3. Read-only user tries to update -> Unauthorized
    with pytest.raises(AdminAuthorizationError):
        admin_console.update_tenant_configuration(
            session_id="sess-ro",
            target_tenant_id="tenant-a",
            key="branding.display_name",
            value="Hacked Brand",
        )

    # 4. Cross-tenant attempt -> Unauthorized
    with pytest.raises(AdminAuthorizationError):
        admin_console.get_tenant_configuration(
            session_id="sess-admin",
            target_tenant_id="tenant-b",
        )
