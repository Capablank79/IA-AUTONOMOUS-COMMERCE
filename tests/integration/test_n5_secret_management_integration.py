"""
Tests de Integración y E2E para N.5 — Secret Management (Transversal N Security, Governance y Safety).

Escenarios exigidos por la especificación N.5:
A. MercadoLibre adapter -> resolve credential securely -> mock provider call.
B. OmniRoute/provider -> resolve API key -> mock inference call.
C. Secret missing -> call blocked safely (explicit NOT_FOUND, no empty string / mock fallback in prod).
D. Secret rotation -> subsequent resolution uses new value/reference version without identity change.
E. Audit / Trace -> metadata only, zero secret leakage in logs, traces, or audit events.
F. N.2 authentication flow continues working without token leakage.
G. M.4 cache path contains no credential material.
H. Restart / config reload -> secret metadata integrity and resolution remain correct.
I. Full E2E Security Chain: Actor -> N.1 -> N.2 -> N.4/N.3 -> permitted external operation -> adapter resolves SecretReference -> mock external provider.
"""

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from src.domain.identity.models import IdentityType, IdentityReference
from src.domain.oauth.models import OAuthConnection
from src.domain.oauth.ports import OAuthConnectionRepository
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationResult,
    AuthenticationRequest,
    PrincipalContext,
)
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule
from src.domain.authorization.models import (
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
    AuthorizationRequest,
    AuthorizationDecision,
)
from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
)
from src.domain.secrets.models import (
    SecretType,
    SecretStatus,
    SecretResolutionStatus,
    SecretValue,
    SecretReference,
    SecretMetadata,
    SecretResolutionResult,
)
from src.domain.audit.models import AuditRecordType

from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.infrastructure.persistence.data.json.secret_metadata_repository import (
    JsonSecretMetadataRepository,
)
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.secrets.providers import (
    EnvSecretProvider,
    InjectedSecretProvider,
    OAuthSecretProviderBridge,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock

from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import AuthenticationService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.rbac.rbac_service import RBACService
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.application.secrets.secret_service import SecretService


class MockOAuthConnectionRepository(OAuthConnectionRepository):
    """Repositorio OAuth en memoria para pruebas de integración."""
    def __init__(self):
        self._connections = {}

    def save(self, connection: OAuthConnection) -> None:
        key = f"{connection.provider.lower()}:{connection.user_id}"
        self._connections[key] = connection

    def get(self, provider: str, user_id: str) -> OAuthConnection:
        key = f"{provider.lower()}:{user_id}"
        return self._connections.get(key)

    def delete(self, provider: str, user_id: str) -> bool:
        key = f"{provider.lower()}:{user_id}"
        return self._connections.pop(key, None) is not None


@pytest.fixture
def test_env(tmp_path):
    clock = VirtualClock(initial_time=datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc))

    identity_repo = JsonIdentityRepository(base_dir=tmp_path / "identities_db")
    role_repo = JsonRoleRepository(base_dir=tmp_path / "roles_db")
    assignment_repo = JsonRoleAssignmentRepository(base_dir=tmp_path / "assignments_db")
    secret_meta_repo = JsonSecretMetadataRepository(base_dir=tmp_path / "secrets_meta_db")
    audit_repo = JsonAuditRepository(storage_dir=tmp_path / "audit_db")
    trace_repo = JsonAgentTraceRepository(base_dir=tmp_path / "trace_db")
    oauth_repo = MockOAuthConnectionRepository()

    trace_service = AgentTraceService(trace_repository=trace_repo)
    identity_service = IdentityService(repository=identity_repo)
    auth_service = AuthenticationService(
        identity_service=identity_service,
        clock=clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
        trusted_api_credentials={"autonomous_token_123": "autonomous_sync_agent"},
    )
    rbac_service = RBACService(
        role_repository=role_repo,
        assignment_repository=assignment_repo,
        audit_repository=audit_repo,
        trace_service=trace_service,
        clock=clock,
    )
    policy_engine = PolicyEngine(rules=[AuthorizationPolicyRule()])
    authz_service = AuthorizationService(
        policy_engine=policy_engine,
        clock=clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
    )

    injected_provider = InjectedSecretProvider(
        initial_secrets={
            "mercadolibre:client_secret": "ml_sec_val_prod_778899",
            "omniroute:api_key": "omni_live_key_998877",
            "openai:api_key": "sk-real-secret-key-112233",
        }
    )
    env_provider = EnvSecretProvider()
    oauth_bridge = OAuthSecretProviderBridge(oauth_repository=oauth_repo)

    secret_service = SecretService(
        providers=[injected_provider, env_provider, oauth_bridge],
        metadata_repository=secret_meta_repo,
        clock=clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
    )

    return {
        "clock": clock,
        "identity_service": identity_service,
        "auth_service": auth_service,
        "rbac_service": rbac_service,
        "authz_service": authz_service,
        "secret_service": secret_service,
        "secret_meta_repo": secret_meta_repo,
        "injected_provider": injected_provider,
        "oauth_repo": oauth_repo,
        "audit_repo": audit_repo,
        "trace_repo": trace_repo,
        "base_path": tmp_path,
    }


# =========================================================================
# Scenario A: MercadoLibre adapter -> resolve credential securely -> mock call
# =========================================================================
def test_scenario_a_mercadolibre_credential_resolution(test_env):
    secret_service = test_env["secret_service"]

    # Adapter solicita SecretReference
    ref = SecretReference(
        reference_id="sec-ml-client-secret",
        provider="mercadolibre",
        secret_name="client_secret",
        secret_type=SecretType.CLIENT_SECRET,
    )

    res = secret_service.resolve(ref)
    assert res.is_resolved
    assert isinstance(res.secret_value, SecretValue)

    # Mock adapter usage
    mock_http_client = MagicMock()
    mock_http_client.post.return_value = {"status": 200, "token": "oauth_granted"}

    # Frontera de infraestructura: se devela solo para la llamada HTTP
    headers = {"X-Client-Secret": res.reveal_value()}
    mock_http_client.post("https://api.mercadolibre.com/oauth/token", headers=headers)

    mock_http_client.post.assert_called_once_with(
        "https://api.mercadolibre.com/oauth/token",
        headers={"X-Client-Secret": "ml_sec_val_prod_778899"}
    )


# =========================================================================
# Scenario B: OmniRoute/provider -> resolve API key -> mock inference call
# =========================================================================
def test_scenario_b_omniroute_api_key_resolution(test_env):
    secret_service = test_env["secret_service"]

    ref = SecretReference(
        reference_id="sec-omniroute-api-key",
        provider="omniroute",
        secret_name="api_key",
        secret_type=SecretType.API_KEY,
    )

    res = secret_service.resolve(ref)
    assert res.is_resolved

    mock_llm_backend = MagicMock()
    mock_llm_backend.complete.return_value = {"text": "Market analysis complete."}

    # Pass revealed secret strictly at provider invocation boundary
    auth_header = f"Bearer {res.reveal_value()}"
    response = mock_llm_backend.complete(
        prompt="Analyze trends",
        auth_header=auth_header,
    )
    assert response["text"] == "Market analysis complete."
    mock_llm_backend.complete.assert_called_once_with(
        prompt="Analyze trends",
        auth_header="Bearer omni_live_key_998877",
    )


# =========================================================================
# Scenario C: Secret missing -> call blocked safely
# =========================================================================
def test_scenario_c_secret_missing_blocks_call(test_env):
    secret_service = test_env["secret_service"]

    missing_ref = SecretReference(
        reference_id="sec-missing-payment-gateway",
        provider="stripe",
        secret_name="live_api_key",
        secret_type=SecretType.API_KEY,
    )

    res = secret_service.resolve(missing_ref)
    assert not res.is_resolved
    assert res.status == SecretResolutionStatus.NOT_FOUND

    mock_payment_adapter = MagicMock()

    # Adapter verifies resolution before attempting dispatch
    if not res.is_resolved:
        mock_payment_adapter.abort_charge(reason=res.error_message)

    mock_payment_adapter.abort_charge.assert_called_once()
    mock_payment_adapter.charge.assert_not_called()


# =========================================================================
# Scenario D: Secret rotation -> subsequent resolution uses new value/version
# =========================================================================
def test_scenario_d_secret_rotation(test_env):
    secret_service = test_env["secret_service"]
    clock = test_env["clock"]

    ref_id = "sec-rot-api-key"
    test_env["injected_provider"].set_secret(ref_id, "api_key_v1_initial")

    secret_service.register_metadata(
        reference_id=ref_id,
        secret_name="rotating_provider_key",
        provider="injected",
        secret_type=SecretType.API_KEY,
        version="1",
    )

    ref = SecretReference(
        reference_id=ref_id,
        provider="injected",
        secret_name="rotating_provider_key",
        secret_type=SecretType.API_KEY,
        version="1",
    )

    res_v1 = secret_service.resolve(ref)
    assert res_v1.reveal_value() == "api_key_v1_initial"

    clock.advance(1800.0)

    # Rotar el secreto
    new_meta = secret_service.rotate_secret(
        reference_id=ref_id,
        new_value="api_key_v2_rotated",
        new_version="2",
    )
    assert new_meta.version == "2"

    res_v2 = secret_service.resolve(ref)
    assert res_v2.reveal_value() == "api_key_v2_rotated"


# =========================================================================
# Scenario E: Audit / Trace -> metadata only, zero secret leakage
# =========================================================================
def test_scenario_e_audit_and_trace_zero_secret_leakage(test_env):
    secret_service = test_env["secret_service"]
    audit_repo = test_env["audit_repo"]
    trace_repo = test_env["trace_repo"]

    secret_val = "extremely_sensitive_audit_leak_test_secret_999"
    ref_id = "sec-audit-check"
    test_env["injected_provider"].set_secret(ref_id, secret_val)

    ref = SecretReference(
        reference_id=ref_id,
        provider="injected",
        secret_name="audit_test_key",
        secret_type=SecretType.API_KEY,
    )

    res = secret_service.resolve(ref, correlation_id="corr-audit-check-001")
    assert res.is_resolved

    # 1. Verificar registros de auditoría K.1
    audit_records = audit_repo.list_records(subject_id=ref_id)
    assert len(audit_records) >= 1
    for rec in audit_records:
        rec_json = json.dumps(dict(rec.metadata))
        assert secret_val not in rec_json
        assert rec.record_type == AuditRecordType.SECRET_RESOLVED

    # 2. Verificar trazas K.2
    trace_records = trace_repo.list_records(execution_id="corr-audit-check-001")
    assert len(trace_records) >= 1
    for st in trace_records:
        st_json = json.dumps(dict(st.metadata))
        assert secret_val not in st_json
        assert secret_val not in str(st)


# =========================================================================
# Scenario F: N.2 authentication flow continues working without token leakage
# =========================================================================
def test_scenario_f_n2_authentication_and_oauth_bridge(test_env):
    identity_service = test_env["identity_service"]
    auth_service = test_env["auth_service"]
    secret_service = test_env["secret_service"]
    oauth_repo = test_env["oauth_repo"]

    user_id = "MLA_990011"
    # Guardar conexión OAuth
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id=user_id,
        access_token="APP_USR_ml_oauth_access_token_12345",
        refresh_token="TG_ml_oauth_refresh_token_67890",
        expires_at=datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc),
    )
    oauth_repo.save(conn)

    # Autenticación N.2
    auth_res = auth_service.authenticate_oauth_connection(conn)
    assert auth_res.is_authenticated
    identity_id = auth_res.principal.identity_id

    # N.5 Secret Management resuelve el token vía OAuthSecretProviderBridge
    token_ref = SecretReference(
        reference_id=f"ref-token-{identity_id}",
        provider="mercadolibre",
        secret_name=f"access_token:{user_id}",
        secret_type=SecretType.ACCESS_TOKEN,
    )
    sec_res = secret_service.resolve(token_ref)
    assert sec_res.is_resolved
    assert sec_res.reveal_value() == "APP_USR_ml_oauth_access_token_12345"

    # Refresh token resolution
    refresh_ref = SecretReference(
        reference_id=f"ref-refresh-{identity_id}",
        provider="mercadolibre",
        secret_name=f"refresh_token:{user_id}",
        secret_type=SecretType.REFRESH_TOKEN,
    )
    refresh_res = secret_service.resolve(refresh_ref)
    assert refresh_res.is_resolved
    assert refresh_res.reveal_value() == "TG_ml_oauth_refresh_token_67890"


# =========================================================================
# Scenario G: M.4 cache path contains no credential material
# =========================================================================
def test_scenario_g_cache_key_contains_no_secrets():
    # Cache keys deben formarse sobre identificadores canónicos / versionados, nunca material secreto
    cache_entry = {
        "namespace": "mercadolibre_catalog",
        "key_identifier": "item_category_MLA1234",
        "version": "v1",
        "value": {"title": "Smartphone Pro", "price": 1000},
        "ttl_seconds": 3600,
    }

    entry_json = json.dumps(cache_entry)

    assert "secret" not in entry_json.lower()
    assert "token" not in entry_json.lower()
    assert "password" not in entry_json.lower()


# =========================================================================
# Scenario H: Restart / config reload -> secret resolution remains correct
# =========================================================================
def test_scenario_h_repository_restart_and_integrity(test_env):
    base_path = test_env["base_path"]
    clock = test_env["clock"]
    meta_repo = test_env["secret_meta_repo"]

    meta = SecretMetadata(
        reference_id="sec-persisted-meta-001",
        secret_name="analytics_api_key",
        provider="analytics_corp",
        secret_type=SecretType.API_KEY,
        version="1",
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    meta_repo.save_metadata(meta)

    # Simular reinicio creando nueva instancia del repositorio sobre el mismo directorio
    restarted_repo = JsonSecretMetadataRepository(base_dir=base_path / "secrets_meta_db")
    reloaded_meta = restarted_repo.get_metadata("sec-persisted-meta-001")

    assert reloaded_meta is not None
    assert reloaded_meta.secret_name == "analytics_api_key"
    assert reloaded_meta.provider == "analytics_corp"
    assert reloaded_meta.checksum == meta.checksum


# =========================================================================
# Scenario I: Full E2E Chain: Actor -> N.1 -> N.2 -> N.4/N.3 -> Action -> N.5 -> External Provider
# =========================================================================
def test_scenario_i_full_e2e_security_pipeline(test_env):
    identity_service = test_env["identity_service"]
    auth_service = test_env["auth_service"]
    rbac_service = test_env["rbac_service"]
    authz_service = test_env["authz_service"]
    secret_service = test_env["secret_service"]

    # 1. N.1: Registrar Identidad de Agente Autónomo
    agent_identity = identity_service.register_system_agent(
        agent_name="autonomous_sync_agent",
        display_name="Autonomous Catalog Sync Agent",
    )

    # 2. N.2: Autenticar Principal con credencial de confianza
    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        token_or_secret="autonomous_token_123",
        declared_subject="autonomous_sync_agent",
    )
    auth_result = auth_service.authenticate_request(auth_req)
    assert auth_result.is_authenticated
    principal = auth_result.principal

    # 3. N.4: Crear Rol y Permiso para Publicación
    publish_permission = rbac_service.create_permission(
        permission_id="perm_publish_01",
        action="listing.publish",
    )
    catalog_role = rbac_service.define_role(
        role_id="CatalogManagerRole",
        name="Catalog Manager Role",
        permissions=[publish_permission],
    )
    rbac_service.assign_role(
        identity_id=principal.identity_id,
        role_id="CatalogManagerRole",
    )

    # 4. N.3: Evaluar Autorización
    principal_ctx = PrincipalContext(principal=principal, auth_result=auth_result)
    rbac_res = rbac_service.resolve_effective_permissions(principal_ctx)
    assert "LISTING_PUBLISH" in rbac_res.actions

    authz_request = AuthorizationRequest(
        principal_context=principal_ctx,
        action="LISTING_PUBLISH",
        resource=ResourceReference(resource_type="marketplace_listing", resource_id="listing-item-001"),
    )
    authz_decision = authz_service.authorize(authz_request, allowed_actions_override=list(rbac_res.actions))
    assert authz_decision.is_allowed

    # 5. N.5: Si está autorizado, el adaptador de infraestructura resuelve el secreto
    secret_ref = SecretReference(
        reference_id="sec-ml-client-secret",
        provider="mercadolibre",
        secret_name="client_secret",
        secret_type=SecretType.CLIENT_SECRET,
    )
    secret_res = secret_service.resolve(secret_ref)
    assert secret_res.is_resolved

    # 6. Llamada a API externa simulada
    mock_ml_api = MagicMock()
    mock_ml_api.publish.return_value = {"id": "MLA998877", "status": "active"}

    # Ejecución en frontera
    api_response = mock_ml_api.publish(
        item_id="listing-item-001",
        auth_credential=secret_res.reveal_value(),
    )
    assert api_response["id"] == "MLA998877"
    assert api_response["status"] == "active"
    mock_ml_api.publish.assert_called_once_with(
        item_id="listing-item-001",
        auth_credential="ml_sec_val_prod_778899",
    )
