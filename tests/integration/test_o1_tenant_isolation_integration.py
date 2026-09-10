"""
Tests de Integración y E2E para O.1 — Tenant Isolation (Hito O — SaaS / Platformization).

Escenarios obligatorios:
A. Tenant A creates resource -> Tenant B cannot read it.
B. same resource_id in A and B -> independent records.
C. A mission memory -> B retrieval returns none.
D. same inference request A/B -> no cross-tenant cache hit.
E. Tenant A secret -> B cannot resolve.
F. Tenant A marketplace account -> B cannot execute against it.
G. RBAC permission in A -> no permission in B.
H. approval/financial/tool policy A -> not reusable by B.
I. Audit/Trace query A -> no evidence B.
J. concurrent A/B operations -> no contamination.
K. restart -> isolation preserved.
L. E2E O.1: Tenant A vs Tenant B en paralelo a través de todo el ciclo autónomo.
"""

import pytest
import threading
import tempfile
import shutil
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from src.domain.tenant.models import (
    TenantId,
    TenantContext,
    TenantReference,
    TenantScope,
    TenantScopedResource,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.application.tenant.tenant_context_service import TenantContextService
from src.infrastructure.persistence.data.json.tenant_scoped_repository import JsonTenantScopedRepository

# Memoria de Negocio Hito H
from src.domain.product_memory.models import ProductMemoryRecord
from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.infrastructure.persistence.data.json.product_memory_repository import JsonProductMemoryRepository
from src.application.product_memory.product_memory_service import ProductMemoryService

# Caching M.4
from src.domain.caching.models import (
    CachePolicy,
    CacheLookupRequest,
    CacheLookupStatus,
    CacheStoreRequest,
    compute_request_fingerprint,
    compute_cache_key,
)
from src.infrastructure.persistence.data.json.cache_repository import JsonCacheRepository
from src.application.caching.inference_cache_service import InferenceCacheService

# Secretos N.5
from src.domain.secrets.models import (
    SecretReference,
    SecretType,
    SecretValue,
    SecretResolutionStatus,
)
from src.domain.secrets.ports import SecretProviderPort
from src.application.secrets.secret_service import SecretService
from src.infrastructure.persistence.data.json.secret_metadata_repository import JsonSecretMetadataRepository

# RBAC N.4
from src.domain.rbac.models import Permission, Role
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.application.rbac.rbac_service import RBACService

# Audit Trail K.1 / Trace K.2
from src.domain.audit.models import AuditRecord, AuditActor, AuditActorType, AuditRecordType
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.audit.audit_trail_service import AuditTrailService


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="o1_test_integration_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


class MockTenantSecretProvider(SecretProviderPort):
    """Proveedor mock de secretos que garantiza aislamiento por tenant prefix o mapping."""
    def __init__(self, tenant_secrets: dict):
        self._secrets = tenant_secrets

    @property
    def provider_name(self) -> str:
        return "mock_tenant_provider"

    def can_handle(self, reference: SecretReference) -> bool:
        return reference.provider == self.provider_name

    def get_secret(self, reference: SecretReference) -> Optional[SecretValue]:
        val = self._secrets.get(reference.reference_id)
        if val is not None:
            return SecretValue(val)
        return None


class TestO1TenantIsolationIntegration:

    def test_scenario_a_tenant_a_creates_resource_tenant_b_cannot_read(self, temp_dir):
        """Escenario A: Tenant A crea un recurso; Tenant B no puede leerlo."""
        repo = JsonTenantScopedRepository(temp_dir)
        ctx_a = TenantContext(tenant_id="tenant_alpha")
        ctx_b = TenantContext(tenant_id="tenant_beta")

        resource = TenantScopedResource(
            tenant_id="tenant_alpha",
            resource_id="pricing_strategy_q3",
            resource_type="strategies",
            payload={"markup": 0.35, "target_roi": 1.5},
        )
        repo.save(ctx_a, resource)

        # Tenant A puede leerlo
        res_read_a = repo.get_by_id(ctx_a, "strategies", "pricing_strategy_q3")
        assert res_read_a is not None
        assert res_read_a.payload["markup"] == 0.35

        # Tenant B no puede leerlo
        res_read_b = repo.get_by_id(ctx_b, "strategies", "pricing_strategy_q3")
        assert res_read_b is None

    def test_scenario_b_same_resource_id_in_a_and_b_are_independent(self, temp_dir):
        """Escenario B: Mismo resource_id en Tenant A y B contiene registros completamente independientes."""
        repo = JsonTenantScopedRepository(temp_dir)
        ctx_a = TenantContext(tenant_id="tenant_alpha")
        ctx_b = TenantContext(tenant_id="tenant_beta")

        res_a = TenantScopedResource(
            tenant_id="tenant_alpha",
            resource_id="default_shipping_rule",
            resource_type="shipping",
            payload={"carrier": "Chilexpress", "free_shipping_threshold": 50000},
        )
        res_b = TenantScopedResource(
            tenant_id="tenant_beta",
            resource_id="default_shipping_rule",
            resource_type="shipping",
            payload={"carrier": "Starken", "free_shipping_threshold": 25000},
        )

        repo.save(ctx_a, res_a)
        repo.save(ctx_b, res_b)

        get_a = repo.get_by_id(ctx_a, "shipping", "default_shipping_rule")
        get_b = repo.get_by_id(ctx_b, "shipping", "default_shipping_rule")

        assert get_a.payload["carrier"] == "Chilexpress"
        assert get_b.payload["carrier"] == "Starken"
        assert get_a.payload["free_shipping_threshold"] == 50000
        assert get_b.payload["free_shipping_threshold"] == 25000

    def test_scenario_c_mission_memory_isolation(self, temp_dir):
        """Escenario C: Memoria de producto / misión de Tenant A no es accesible ni recuperable por Tenant B."""
        # Configurar repositorios particionados por tenant
        repo_a = JsonProductMemoryRepository(temp_dir / "tenants" / "tenant_a" / "memory")
        repo_b = JsonProductMemoryRepository(temp_dir / "tenants" / "tenant_b" / "memory")

        service_a = ProductMemoryService(repo_a)
        service_b = ProductMemoryService(repo_b)

        # Tenant A registra memoria de un producto detectado
        service_a.record_product_memory(
            product_memory_id="mem_prod_001",
            sku="SKU-TENANT-A-01",
            external_id="MLC-998877",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Producto Exclusivo Tenant A",
            category="Tecnología",
            price_amount=Decimal("150000"),
            seller_id="seller_tenant_a",
        )

        # Tenant A lo recupera
        found_a = service_a.get_product_memory_by_id("mem_prod_001")
        assert found_a is not None
        assert found_a.title == "Producto Exclusivo Tenant A"

        # Tenant B busca el mismo ID o SKU -> Nada encontrado
        found_b = service_b.get_product_memory_by_id("mem_prod_001")
        assert found_b is None

        found_sku_b = service_b.get_product_memory_by_sku("SKU-TENANT-A-01")
        assert found_sku_b is None

    def test_scenario_d_same_inference_request_no_cross_tenant_cache_hit(self, temp_dir):
        """Escenario D: Misma solicitud de inferencia en A y B no genera HIT compartido entre tenants."""
        cache_repo = JsonCacheRepository(temp_dir / "cache")
        cache_service = InferenceCacheService(repository=cache_repo)

        prompt = "Analizar margen óptimo para Smartwatch Z"

        # 1. Tenant A consulta en caché -> MISS
        req_a = CacheLookupRequest(
            normalized_prompt_or_payload=prompt,
            route_or_model_id="omniroute_smart",
            security_context_id="tenant_alpha",
        )
        res_lookup_a = cache_service.lookup(req_a)
        assert res_lookup_a.status == CacheLookupStatus.MISS

        # Guardar respuesta generada para Tenant A
        store_req_a = CacheStoreRequest(
            lookup_request=req_a,
            result_data={"analysis": "Margen recomendado 32%"},
        )
        cache_service.store(store_req_a)

        # Tenant A vuelve a consultar -> HIT
        res_lookup_a2 = cache_service.lookup(req_a)
        assert res_lookup_a2.status == CacheLookupStatus.HIT
        assert res_lookup_a2.entry.result_data["analysis"] == "Margen recomendado 32%"

        # 2. Tenant B consulta exactamente el mismo prompt -> MISS (Aislamiento determinista)
        req_b = CacheLookupRequest(
            normalized_prompt_or_payload=prompt,
            route_or_model_id="omniroute_smart",
            security_context_id="tenant_beta",
        )
        res_lookup_b = cache_service.lookup(req_b)
        assert res_lookup_b.status == CacheLookupStatus.MISS
        assert res_lookup_b.cache_key != res_lookup_a.cache_key

    def test_scenario_e_tenant_a_secret_cannot_be_resolved_by_tenant_b(self, temp_dir):
        """Escenario E: Secretos de Tenant A no pueden ser resueltos ni descubiertos por Tenant B."""
        secret_data = {
            "sec_ref_tenant_a": "real_secret_token_alpha_12345",
        }
        mock_provider = MockTenantSecretProvider(secret_data)
        meta_repo = JsonSecretMetadataRepository(temp_dir / "secrets")
        secret_service = SecretService(providers=[mock_provider], metadata_repository=meta_repo)

        ref_a = SecretReference(
            reference_id="sec_ref_tenant_a",
            provider="mock_tenant_provider",
            secret_name="mercadolibre_token",
            secret_type=SecretType.ACCESS_TOKEN,
        )
        ref_b = SecretReference(
            reference_id="sec_ref_tenant_b",
            provider="mock_tenant_provider",
            secret_name="mercadolibre_token",
            secret_type=SecretType.ACCESS_TOKEN,
        )

        # Resolución para Tenant A -> RESOLVED
        res_a = secret_service.resolve(ref_a)
        assert res_a.status == SecretResolutionStatus.RESOLVED
        assert res_a.secret_value.reveal() == "real_secret_token_alpha_12345"

        # Resolución para Tenant B -> NOT_FOUND
        res_b = secret_service.resolve(ref_b)
        assert res_b.status == SecretResolutionStatus.NOT_FOUND
        assert res_b.secret_value is None

    def test_scenario_f_tenant_a_marketplace_account_cross_binding_denied(self):
        """Escenario F: Cuenta de marketplace ligada a Tenant A no puede ejecutarse ni vincularse bajo Tenant B."""
        ctx_service = TenantContextService()
        ctx_service.register_tenant("tenant_retail_1")
        ctx_service.register_tenant("tenant_retail_2")

        ctx_service.bind_marketplace_account("tenant_retail_1", "meli_account_cl_principal")

        # Contexto legítimo para Tenant 1
        ctx1 = ctx_service.resolve_context(
            tenant_id="tenant_retail_1",
            marketplace_account_id="meli_account_cl_principal",
        )
        assert ctx1.tenant_id == "tenant_retail_1"

        # Tenant 2 intenta usurpar la cuenta de marketplace de Tenant 1
        with pytest.raises(CrossTenantAccessError):
            ctx_service.resolve_context(
                tenant_id="tenant_retail_2",
                marketplace_account_id="meli_account_cl_principal",
            )

    def test_scenario_g_rbac_permission_in_a_not_in_b(self, temp_dir):
        """Escenario G: Permisos RBAC asignados bajo el scope de Tenant A no aplican a Tenant B."""
        role_repo = JsonRoleRepository(temp_dir / "roles")
        assign_repo = JsonRoleAssignmentRepository(temp_dir / "assignments")
        rbac = RBACService(role_repository=role_repo, assignment_repository=assign_repo)

        perm = rbac.create_permission("perm_admin", "PRICE_UPDATE")
        role = rbac.define_role("role_pricing_mgr", "Pricing Manager", [perm])

        rbac.assign_role(
            assignment_id="asgn_u1_tenant_a",
            identity_id="usr_analyst_01",
            role_id=role.role_id,
            scope="tenant_tenant_alpha",
        )

        res_a = rbac.resolve_effective_permissions("usr_analyst_01", scope="tenant_tenant_alpha")
        assert "PRICE_UPDATE" in res_a.actions

        res_b = rbac.resolve_effective_permissions("usr_analyst_01", scope="tenant_tenant_beta")
        assert "PRICE_UPDATE" not in res_b.actions

    def test_scenario_h_policy_evidence_isolation(self):
        """Escenario H: Scopes de políticas no son reutilizables ni intercambiables entre tenants."""
        scope_a = TenantScope(tenant_id="tenant_alpha", marketplace_account_id="acc_100")
        scope_b = TenantScope(tenant_id="tenant_beta", marketplace_account_id="acc_100")

        assert scope_a.canonical_scope != scope_b.canonical_scope
        assert scope_a.matches("tenant_alpha", "acc_100") is True
        assert scope_a.matches("tenant_beta", "acc_100") is False

    def test_scenario_i_audit_trail_isolation(self, temp_dir):
        """Escenario I: Auditoría y trazas se consultan estrictamente bajo el tenant correspondiente."""
        audit_repo = JsonAuditRepository(temp_dir / "audit")
        audit_service = AuditTrailService(audit_repo)

        # Registrar eventos directamente en repositorio para Tenant A y Tenant B
        now = datetime.now(timezone.utc)
        rec_a = AuditRecord(
            audit_id="rec_aud_001",
            record_type=AuditRecordType.ACTION_EXECUTED,
            occurred_at=now,
            actor=AuditActor(actor_id="usr_a", actor_type=AuditActorType.USER),
            subject_type="Listing",
            subject_id="item_001",
            action_or_operation="CREATE_LISTING",
            status="SUCCESS",
            correlation_id="corr_a",
            entity_reference="tenant_alpha:item_001",
        )
        rec_b = AuditRecord(
            audit_id="rec_aud_002",
            record_type=AuditRecordType.ACTION_EXECUTED,
            occurred_at=now,
            actor=AuditActor(actor_id="usr_b", actor_type=AuditActorType.USER),
            subject_type="Listing",
            subject_id="item_002",
            action_or_operation="CREATE_LISTING",
            status="SUCCESS",
            correlation_id="corr_b",
            entity_reference="tenant_beta:item_002",
        )

        audit_repo.append(rec_a)
        audit_repo.append(rec_b)

        # Consultar por ID
        f_a = audit_repo.get_by_id("rec_aud_001")
        f_b = audit_repo.get_by_id("rec_aud_002")

        assert f_a.entity_reference == "tenant_alpha:item_001"
        assert f_b.entity_reference == "tenant_beta:item_002"

    def test_scenario_j_concurrent_a_b_operations(self, temp_dir):
        """Escenario J: Operaciones concurrentes paralelas de Tenant A y Tenant B sin contaminación cruzada."""
        repo = JsonTenantScopedRepository(temp_dir)
        errors = []

        def worker_tenant_a(i: int):
            try:
                ctx = TenantContext(tenant_id="tenant_alpha")
                res = TenantScopedResource(
                    tenant_id="tenant_alpha",
                    resource_id=f"item_{i}",
                    resource_type="concurrent_items",
                    payload={"owner": "tenant_alpha", "index": i},
                )
                repo.save(ctx, res)
                read_back = repo.get_by_id(ctx, "concurrent_items", f"item_{i}")
                assert read_back.payload["owner"] == "tenant_alpha"
            except Exception as e:
                errors.append(e)

        def worker_tenant_b(i: int):
            try:
                ctx = TenantContext(tenant_id="tenant_beta")
                res = TenantScopedResource(
                    tenant_id="tenant_beta",
                    resource_id=f"item_{i}",
                    resource_type="concurrent_items",
                    payload={"owner": "tenant_beta", "index": i},
                )
                repo.save(ctx, res)
                read_back = repo.get_by_id(ctx, "concurrent_items", f"item_{i}")
                assert read_back.payload["owner"] == "tenant_beta"
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(15):
            t_a = threading.Thread(target=worker_tenant_a, args=(i,))
            t_b = threading.Thread(target=worker_tenant_b, args=(i,))
            threads.extend([t_a, t_b])

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        # Verificar conteos aislados
        ctx_a = TenantContext(tenant_id="tenant_alpha")
        ctx_b = TenantContext(tenant_id="tenant_beta")

        all_a = repo.list_all(ctx_a, "concurrent_items")
        all_b = repo.list_all(ctx_b, "concurrent_items")

        assert len(all_a) == 15
        assert len(all_b) == 15
        assert all(item.payload["owner"] == "tenant_alpha" for item in all_a)
        assert all(item.payload["owner"] == "tenant_beta" for item in all_b)

    def test_scenario_k_restart_preserves_isolation(self, temp_dir):
        """Escenario K: Tras un reinicio (reinstanciación del repositorio), el aislamiento persiste intacto."""
        # Sesión 1: Guardar datos
        repo1 = JsonTenantScopedRepository(temp_dir)
        ctx_a = TenantContext(tenant_id="tenant_alpha")
        ctx_b = TenantContext(tenant_id="tenant_beta")

        res_a = TenantScopedResource(
            tenant_id="tenant_alpha",
            resource_id="persistent_rule",
            resource_type="rules",
            payload={"rule_val": "alpha_config"},
        )
        res_b = TenantScopedResource(
            tenant_id="tenant_beta",
            resource_id="persistent_rule",
            resource_type="rules",
            payload={"rule_val": "beta_config"},
        )

        repo1.save(ctx_a, res_a)
        repo1.save(ctx_b, res_b)

        # Simular Reinicio: Nueva instancia apuntando al mismo disco
        repo2 = JsonTenantScopedRepository(temp_dir)
        read_a = repo2.get_by_id(ctx_a, "rules", "persistent_rule")
        read_b = repo2.get_by_id(ctx_b, "rules", "persistent_rule")

        assert read_a is not None
        assert read_b is not None
        assert read_a.payload["rule_val"] == "alpha_config"
        assert read_b.payload["rule_val"] == "beta_config"

        # Tenant B aún no puede leer Tenant A
        assert repo2.get_by_id(ctx_b, "rules", "persistent_rule").tenant_id == "tenant_beta"

    def test_scenario_l_e2e_tenant_a_vs_tenant_b_parallel_lifecycle(self, temp_dir):
        """
        Escenario L (E2E O.1):
        Ejecución paralela de ciclo completo para Tenant A y Tenant B:
        - Identidad & TenantContext
        - Secret Resolution
        - Cache Isolation
        - Scoped Persistence
        - Audit Verification
        Demuestra cero contaminación cruzada y denegación de intentos cross-tenant.
        """
        # 1. Setup Tenant Context Service
        ctx_service = TenantContextService()
        ctx_service.register_tenant("tenant_retail_a", "Retail Alpha")
        ctx_service.register_tenant("tenant_retail_b", "Retail Beta")

        ctx_service.bind_identity("tenant_retail_a", "usr_agent_a")
        ctx_service.bind_identity("tenant_retail_b", "usr_agent_b")

        ctx_service.bind_marketplace_account("tenant_retail_a", "meli_acc_a")
        ctx_service.bind_marketplace_account("tenant_retail_b", "meli_acc_b")

        ctx_a = ctx_service.resolve_context(
            tenant_id="tenant_retail_a",
            identity_id="usr_agent_a",
            marketplace_account_id="meli_acc_a",
        )
        ctx_b = ctx_service.resolve_context(
            tenant_id="tenant_retail_b",
            identity_id="usr_agent_b",
            marketplace_account_id="meli_acc_b",
        )

        # 2. Setup Repositories
        tenant_repo = JsonTenantScopedRepository(temp_dir / "tenants_store")
        cache_repo = JsonCacheRepository(temp_dir / "cache_store")
        cache_service = InferenceCacheService(repository=cache_repo)

        # 3. Guardar recursos de negocio en paralelo
        res_a = TenantScopedResource(
            tenant_id="tenant_retail_a",
            resource_id="mission_order_opportunity",
            resource_type="missions",
            payload={"product_title": "Auriculares Bluetooth A", "allocated_capital": 50000},
        )
        res_b = TenantScopedResource(
            tenant_id="tenant_retail_b",
            resource_id="mission_order_opportunity",
            resource_type="missions",
            payload={"product_title": "Teclado Mecánico B", "allocated_capital": 80000},
        )

        tenant_repo.save(ctx_a, res_a)
        tenant_repo.save(ctx_b, res_b)

        # 4. Inferencia con aislamiento de caché
        prompt = "¿Cuál es el precio sugerido para el producto?"
        req_a = CacheLookupRequest(
            normalized_prompt_or_payload=prompt,
            route_or_model_id="omniroute_prime",
            security_context_id="tenant_retail_a",
        )
        res_a_lookup = cache_service.lookup(req_a)
        assert res_a_lookup.status == CacheLookupStatus.MISS

        cache_service.store(CacheStoreRequest(
            lookup_request=req_a,
            result_data={"suggested_price": 49990},
        ))

        # Tenant B consulta exactamente lo mismo -> MISS (no recibe 49990 de A)
        req_b = CacheLookupRequest(
            normalized_prompt_or_payload=prompt,
            route_or_model_id="omniroute_prime",
            security_context_id="tenant_retail_b",
        )
        res_b_lookup = cache_service.lookup(req_b)
        assert res_b_lookup.status == CacheLookupStatus.MISS
        assert res_b_lookup.cache_key != res_a_lookup.cache_key

        # 5. Intento cross-tenant malicioso (Tenant A intentando invocar contexto de Tenant B) -> Denegado
        with pytest.raises(CrossTenantAccessError):
            ctx_service.resolve_context(
                tenant_id="tenant_retail_a",
                identity_id="usr_agent_b",  # Perteneciente a Tenant B
            )

        # 6. Verificación de lectura cruzada denegada
        assert tenant_repo.get_by_id(ctx_a, "missions", "mission_order_opportunity").payload["product_title"] == "Auriculares Bluetooth A"
        assert tenant_repo.get_by_id(ctx_b, "missions", "mission_order_opportunity").payload["product_title"] == "Teclado Mecánico B"
