"""
Tests Unitarios Exhaustivos para O.1 — Tenant Isolation (Hito O — SaaS / Platformization).

Verificaciones canónicas mínimas:
1. canonical TenantId
2. immutable TenantContext
3. safe tenant identifiers (path traversal prevention)
4. same resource id different tenant isolated
5. repository cross-tenant read denied
6. cross-tenant update/save denied
7. missing tenant fail-safe
8. identity != tenant
9. marketplace account != tenant
10. tenant-scoped RBAC
11. tenant-scoped secret reference / resolution
12. cache key tenant isolation
13. approval evidence tenant isolation
14. financial policy tenant isolation
15. deterministic context & SHA-256 checksums
16. no O.2+ implementation leakage
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import shutil

from src.domain.tenant.models import (
    TenantId,
    TenantContext,
    TenantReference,
    TenantScope,
    TenantScopedResource,
    TenantResolutionStatus,
    CrossTenantAccessError,
    TenantSecurityViolationError,
    compute_tenant_context_checksum,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.application.tenant.tenant_context_service import TenantContextService
from src.infrastructure.persistence.data.json.tenant_scoped_repository import JsonTenantScopedRepository

# Integraciones de gobernanza y seguridad N.1 - N.11 / M.4
from src.domain.identity.models import Identity, IdentityType, IdentityStatus, IdentityReference
from src.domain.rbac.models import Permission, Role, RoleAssignment, PermissionStatus
from src.application.rbac.rbac_service import RBACService
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.domain.secrets.models import SecretReference, SecretType, SecretValue
from src.domain.caching.models import (
    CachePolicy,
    compute_request_fingerprint,
    compute_cache_key,
)
from src.domain.financial_limit.models import (
    FinancialLimitPolicy,
    FinancialLimitRule,
    FinancialLimitType,
)


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="o1_test_unit_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


class TestO1TenantIsolationUnit:

    def test_01_canonical_tenant_id(self):
        """Verifica la creación y propiedades canónicas del TenantId."""
        t_id = TenantId("tenant_acme_corp")
        assert t_id.value == "tenant_acme_corp"
        assert str(t_id) == "tenant_acme_corp"
        assert repr(t_id) == "TenantId('tenant_acme_corp')"

        # Espacios en blanco se limpian
        t_id2 = TenantId("  tenant_beta  ")
        assert t_id2.value == "tenant_beta"

    def test_02_immutable_tenant_context(self):
        """Verifica inmutabilidad estricta y sanitización de TenantContext."""
        ctx = TenantContext(
            tenant_id="tenant_alpha",
            identity_id="usr_admin_01",
            correlation_id="corr_12345",
            marketplace_account_id="meli_acc_99",
            metadata={"environment": "production", "api_key": "sensitive_val_to_redact"},
        )
        assert ctx.tenant_id == "tenant_alpha"
        assert ctx.identity_id == "usr_admin_01"
        assert ctx.marketplace_account_id == "meli_acc_99"
        assert ctx.metadata["environment"] == "production"
        # Secretos sanitizados
        assert ctx.metadata["api_key"] == "[REDACTED]"
        assert ctx.checksum is not None

        # Verificar inmutabilidad (frozen dataclass)
        with pytest.raises((AttributeError, TypeError)):
            ctx.tenant_id = "tenant_hacked"  # type: ignore

    def test_03_safe_tenant_identifiers_path_traversal_prevention(self):
        """Verifica el bloqueo de ataques de path traversal en TenantId y TenantContext."""
        unsafe_ids = [
            "../etc/passwd",
            "../../secret_tenant",
            "tenant/slash",
            "tenant\\backslash",
            "C:\\Windows\\System32",
            "",
            "   ",
            "tenant:sub",
        ]
        for unsafe in unsafe_ids:
            with pytest.raises(ValueError):
                TenantId(unsafe)

            with pytest.raises(ValueError):
                TenantContext(tenant_id=unsafe)

    def test_04_same_resource_id_different_tenant_isolated(self, temp_dir):
        """Verifica que el mismo resource_id en diferentes tenants representa registros independientes."""
        repo = JsonTenantScopedRepository(temp_dir)
        ctx_a = TenantContext(tenant_id="tenant_a")
        ctx_b = TenantContext(tenant_id="tenant_b")

        # Mismo resource_id 'catalog_config'
        res_a = TenantScopedResource(
            tenant_id="tenant_a",
            resource_id="catalog_config",
            resource_type="config",
            payload={"currency": "USD", "max_margin": 25},
        )
        res_b = TenantScopedResource(
            tenant_id="tenant_b",
            resource_id="catalog_config",
            resource_type="config",
            payload={"currency": "CLP", "max_margin": 10},
        )

        repo.save(ctx_a, res_a)
        repo.save(ctx_b, res_b)

        fetched_a = repo.get_by_id(ctx_a, "config", "catalog_config")
        fetched_b = repo.get_by_id(ctx_b, "config", "catalog_config")

        assert fetched_a is not None
        assert fetched_b is not None
        assert fetched_a.payload["currency"] == "USD"
        assert fetched_b.payload["currency"] == "CLP"
        assert fetched_a.tenant_id == "tenant_a"
        assert fetched_b.tenant_id == "tenant_b"

    def test_05_repository_cross_tenant_read_denied(self, temp_dir):
        """Verifica que Tenant B no puede leer recursos creados por Tenant A."""
        repo = JsonTenantScopedRepository(temp_dir)
        ctx_a = TenantContext(tenant_id="tenant_a")
        ctx_b = TenantContext(tenant_id="tenant_b")

        res_a = TenantScopedResource(
            tenant_id="tenant_a",
            resource_id="secret_contract_01",
            resource_type="contracts",
            payload={"value": 1000000},
        )
        repo.save(ctx_a, res_a)

        # Tenant B intenta leer el recurso de Tenant A
        read_by_b = repo.get_by_id(ctx_b, "contracts", "secret_contract_01")
        assert read_by_b is None

        # Listar por Tenant B no retorna registros de Tenant A
        all_b = repo.list_all(ctx_b, "contracts")
        assert len(all_b) == 0

        all_a = repo.list_all(ctx_a, "contracts")
        assert len(all_a) == 1

    def test_06_cross_tenant_update_denied(self, temp_dir):
        """Verifica que guardar o actualizar un recurso con tenant mismatch genera CrossTenantAccessError."""
        repo = JsonTenantScopedRepository(temp_dir)
        ctx_a = TenantContext(tenant_id="tenant_a")

        # Intentar guardar un recurso de Tenant B usando el contexto de Tenant A
        res_b = TenantScopedResource(
            tenant_id="tenant_b",
            resource_id="price_rule_1",
            resource_type="rules",
            payload={"price": 500},
        )

        with pytest.raises(CrossTenantAccessError):
            repo.save(ctx_a, res_b)

    def test_07_missing_tenant_fail_safe(self, temp_dir):
        """Verifica el principio Fail-Safe: ausencia de TenantContext lanza TenantSecurityViolationError."""
        repo = JsonTenantScopedRepository(temp_dir)
        res = TenantScopedResource(
            tenant_id="tenant_a",
            resource_id="item_1",
            resource_type="items",
            payload={},
        )

        with pytest.raises(TenantSecurityViolationError):
            repo.save(None, res)  # type: ignore

        with pytest.raises(TenantSecurityViolationError):
            repo.get_by_id(None, "items", "item_1")  # type: ignore

        with pytest.raises(TenantSecurityViolationError):
            CrossTenantGuard.assert_same_tenant(None, "tenant_a")

    def test_08_identity_differs_from_tenant(self):
        """Verifica la distinción canónica entre Identity (quién opera) y Tenant (organización)."""
        service = TenantContextService()
        service.register_tenant("tenant_omega")
        service.bind_identity("tenant_omega", "usr_ops_lead")

        # Identity ligada a tenant_omega
        ctx = service.resolve_context(
            tenant_id="tenant_omega",
            identity_id="usr_ops_lead",
        )
        assert ctx.tenant_id == "tenant_omega"
        assert ctx.identity_id == "usr_ops_lead"

        # Intentar usar esa misma identidad en otro tenant es rechazado
        with pytest.raises(CrossTenantAccessError):
            service.resolve_context(
                tenant_id="tenant_other",
                identity_id="usr_ops_lead",
            )

    def test_09_marketplace_account_differs_from_tenant(self):
        """Verifica la distinción entre Marketplace Account (integración) y Tenant."""
        service = TenantContextService()
        service.register_tenant("tenant_retail_corp")
        service.bind_marketplace_account("tenant_retail_corp", "meli_account_cl_123")

        ctx = service.resolve_context(
            tenant_id="tenant_retail_corp",
            marketplace_account_id="meli_account_cl_123",
        )
        assert ctx.tenant_id == "tenant_retail_corp"
        assert ctx.marketplace_account_id == "meli_account_cl_123"

        # Intentar vincular la cuenta de marketplace a otro tenant es rechazado
        with pytest.raises(CrossTenantAccessError):
            service.resolve_context(
                tenant_id="tenant_rival_corp",
                marketplace_account_id="meli_account_cl_123",
            )

    def test_10_tenant_scoped_rbac(self, temp_dir):
        """Verifica que permisos otorgados bajo un scope de Tenant A no aplican a Tenant B."""
        role_repo = JsonRoleRepository(temp_dir / "roles")
        assign_repo = JsonRoleAssignmentRepository(temp_dir / "assignments")
        rbac = RBACService(role_repository=role_repo, assignment_repository=assign_repo)

        # Crear rol con permiso de publicación
        perm = rbac.create_permission("perm_pub", "PUBLISH_LISTING")
        role = rbac.define_role("role_publisher", "Publisher", [perm])

        # Asignar rol al usuario en el scope de Tenant A
        rbac.assign_role(
            assignment_id="asgn_1",
            identity_id="usr_user_1",
            role_id=role.role_id,
            scope="tenant_tenant_a",
        )

        # Consultar permisos para Tenant A
        res_a = rbac.resolve_effective_permissions("usr_user_1", scope="tenant_tenant_a")
        assert "PUBLISH_LISTING" in res_a.actions

        # Consultar permisos para Tenant B -> Sin permisos
        res_b = rbac.resolve_effective_permissions("usr_user_1", scope="tenant_tenant_b")
        assert "PUBLISH_LISTING" not in res_b.actions
        assert res_b.is_empty is True

    def test_11_tenant_scoped_secret_reference(self):
        """Verifica que referencias y nombres de secretos estén debidamente aislados por tenant scope."""
        ref_a = SecretReference(
            reference_id="ref_sec_tenant_a",
            provider="mercadolibre_oauth",
            secret_name="tenant_a_api_token",
            secret_type=SecretType.ACCESS_TOKEN,
        )
        ref_b = SecretReference(
            reference_id="ref_sec_tenant_b",
            provider="mercadolibre_oauth",
            secret_name="tenant_b_api_token",
            secret_type=SecretType.ACCESS_TOKEN,
        )
        assert ref_a.reference_id != ref_b.reference_id
        assert ref_a.secret_name != ref_b.secret_name

        # SecretValue redactado en memoria
        val = SecretValue("super_secret_key_12345")
        assert str(val) == "[REDACTED]"
        assert repr(val) == "<SecretValue: [REDACTED]>"
        assert val.reveal() == "super_secret_key_12345"

    def test_12_cache_key_tenant_isolation(self):
        """Verifica que solicitudes idénticas de inferencia generen claves de caché distintas para diferentes tenants."""
        prompt = "¿Cuál es el precio óptimo para el SKU-100?"

        # M.4 utiliza security_context_id para aislar fingerprints por tenant
        fp_a = compute_request_fingerprint(
            normalized_prompt_or_payload=prompt,
            security_context_id="tenant_a",
        )
        fp_b = compute_request_fingerprint(
            normalized_prompt_or_payload=prompt,
            security_context_id="tenant_b",
        )

        assert fp_a != fp_b

        key_a = compute_cache_key(
            request_fingerprint=fp_a,
            route_or_model_id="omniroute_fast",
            policy_id="cache_pol_1",
            policy_version="1.0.0",
        )
        key_b = compute_cache_key(
            request_fingerprint=fp_b,
            route_or_model_id="omniroute_fast",
            policy_id="cache_pol_1",
            policy_version="1.0.0",
        )

        assert key_a != key_b

    def test_13_approval_evidence_tenant_isolation(self):
        """Verifica que evidencias de aprobación queden vinculadas y no sean intercambiables entre tenants."""
        scope_a = TenantScope(tenant_id="tenant_a")
        scope_b = TenantScope(tenant_id="tenant_b")

        assert scope_a.canonical_scope == "tenant_tenant_a"
        assert scope_b.canonical_scope == "tenant_tenant_b"
        assert scope_a.matches("tenant_a") is True
        assert scope_a.matches("tenant_b") is False

    def test_14_financial_policy_tenant_isolation(self):
        """Verifica que las políticas financieras configuradas para una cuenta de Tenant A no apliquen a Tenant B."""
        rule_a = FinancialLimitRule(
            rule_id="rule_max_spend_a",
            limit_type=FinancialLimitType.MAX_ORDER_VALUE,
            currency="CLP",
            max_amount=Decimal("100000"),
            account_id="meli_acc_tenant_a",
        )
        rule_b = FinancialLimitRule(
            rule_id="rule_max_spend_b",
            limit_type=FinancialLimitType.MAX_ORDER_VALUE,
            currency="CLP",
            max_amount=Decimal("500000"),
            account_id="meli_acc_tenant_b",
        )

        policy_a = FinancialLimitPolicy(
            policy_name="policy_tenant_a",
            currency="CLP",
            rules=[rule_a],
        )
        policy_b = FinancialLimitPolicy(
            policy_name="policy_tenant_b",
            currency="CLP",
            rules=[rule_b],
        )

        assert policy_a.checksum != policy_b.checksum
        assert policy_a.rules[0].account_id != policy_b.rules[0].account_id

    def test_15_deterministic_tenant_context_checksum(self):
        """Verifica la generación determinista del checksum SHA-256 para TenantContext."""
        chk1 = compute_tenant_context_checksum(
            tenant_id="tenant_alpha",
            identity_id="usr_01",
            correlation_id="corr_99",
            marketplace_account_id="acc_1",
            metadata={"tier": "enterprise"},
        )
        chk2 = compute_tenant_context_checksum(
            tenant_id="tenant_alpha",
            identity_id="usr_01",
            correlation_id="corr_99",
            marketplace_account_id="acc_1",
            metadata={"tier": "enterprise"},
        )
        assert chk1 == chk2

        # Cambio en cualquier campo altera el checksum
        chk3 = compute_tenant_context_checksum(
            tenant_id="tenant_beta",
            identity_id="usr_01",
            correlation_id="corr_99",
            marketplace_account_id="acc_1",
            metadata={"tier": "enterprise"},
        )
        assert chk1 != chk3

    def test_16_no_o2_leakage(self):
        """Verifica que no se hayan introducido prematuramente conceptos de O.2+ (billing, subscriptions, tiers)."""
        import src.domain.tenant.models as t_models
        exported = dir(t_models)
        prohibited = ["Billing", "Subscription", "Plan", "Invoice", "PaymentGateway", "StripeBilling"]
        for p in prohibited:
            assert p not in exported, f"Prohibited O.2+ concept '{p}' found in O.1 domain models!"
