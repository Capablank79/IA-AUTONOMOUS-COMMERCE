"""
Tests Unitarios Exhaustivos para O.3 — SaaS Authentication & Multi-Tenant Session Management (Hito O — SaaS / Platformization).

Verificaciones obligatorias (mínimo 16 requeridas por la especificación):
1. session created from valid N.2 auth
2. unauthenticated cannot create
3. tenant binding required
4. cross-tenant binding rejected
5. organization membership validation
6. expired session invalid
7. revoked session invalid
8. tampered session invalid
9. session id != token
10. no credentials in session
11. multiple tenant sessions isolated
12. deterministic tenant/org context
13. logout revokes
14. restart-safe semantics
15. session != authorization
16. no O.4+ implementation
"""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import shutil
import json

from src.domain.session.models import (
    SaaSSession,
    SessionStatus,
    SessionReference,
    SessionContext,
    SessionValidationResult,
    SessionValidationReasonCode,
    SessionError,
    SessionNotFoundError,
    SessionValidationError,
    SessionExpiredError,
    SessionRevokedError,
    SessionTenantMismatchError,
    SessionSecurityViolationError,
    generate_secure_session_id,
    compute_session_checksum,
    compute_session_context_checksum,
)
from src.domain.session.ports import SaaSSessionRepositoryPort, SaaSSessionServicePort
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.application.session.saas_session_service import SaaSSessionService

# Integración N.1, N.2, N.4, O.1, O.2, K.7
from src.domain.identity.models import IdentityReference, IdentityType, PrincipalIdentity
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationRequest,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.application.tenant.tenant_context_service import TenantContextService
from src.domain.organization.models import (
    Organization,
    OrganizationStatus,
    UserMembership,
    MembershipStatus,
    MembershipRole,
)
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonOrganizationRepository,
    JsonMembershipRepository,
)
from src.application.organization.organization_service import (
    OrganizationService,
    OrganizationMembershipService,
)
from src.domain.reliability.ports import ClockPort


class DeterministicClock(ClockPort):
    """Implementación de ClockPort para pruebas deterministas sin time.sleep."""
    def __init__(self, initial_time: datetime):
        if initial_time.tzinfo is None:
            initial_time = initial_time.replace(tzinfo=timezone.utc)
        self._current = initial_time

    def now(self) -> datetime:
        return self._current

    def sleep(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="o3_unit_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def clock():
    return DeterministicClock(datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def session_repo(temp_dir):
    return JsonSaaSSessionRepository(temp_dir)


@pytest.fixture
def tenant_service():
    svc = TenantContextService(
        registered_tenants={"tenant_alpha", "tenant_beta"},
        identity_to_tenant={"usr_alice_01": "tenant_alpha", "usr_bob_02": "tenant_beta"},
    )
    return svc


@pytest.fixture
def org_repo(temp_dir):
    return JsonOrganizationRepository(temp_dir)


@pytest.fixture
def membership_repo(temp_dir):
    return JsonMembershipRepository(temp_dir)


@pytest.fixture
def org_service(org_repo):
    return OrganizationService(organization_repo=org_repo)


@pytest.fixture
def membership_service(membership_repo, org_repo, tenant_service):
    return OrganizationMembershipService(
        membership_repo=membership_repo,
        organization_repo=org_repo,
        tenant_mapping=tenant_service,
    )


@pytest.fixture
def saas_session_service(session_repo, tenant_service, org_repo, membership_repo, clock):
    return SaaSSessionService(
        session_repository=session_repo,
        tenant_resolver=tenant_service,
        tenant_mapping=tenant_service,
        organization_repo=org_repo,
        membership_repo=membership_repo,
        clock=clock,
    )


@pytest.fixture
def valid_auth_result():
    principal = IdentityReference(
        identity_id="usr_alice_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:google:alice@example.com",
        display_name="Alice Explorer",
    )
    return AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google_oauth",
        principal=principal,
        authenticated_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 7, 13, 0, 0, tzinfo=timezone.utc),
        reason_codes=("TOKEN_VALID",),
        correlation_id="corr_auth_01",
    )


# ==============================================================================
# 1. session created from valid N.2 auth
# ==============================================================================
def test_1_session_created_from_valid_n2_auth(saas_session_service, valid_auth_result):
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
        ttl_seconds=1800,
        correlation_id="corr_create_01",
    )

    assert session.session_id.startswith("sess_")
    assert session.identity_id == "usr_alice_01"
    assert session.tenant_id == "tenant_alpha"
    assert session.organization_id is None
    assert session.status == SessionStatus.ACTIVE
    assert session.authentication_method == "OAUTH2"
    assert session.authentication_provider == "google_oauth"
    assert session.is_active is True
    assert session.checksum != ""

    val_res = saas_session_service.validate_session(session.session_id, expected_tenant_id="tenant_alpha")
    assert val_res.is_valid is True
    assert val_res.status == SessionStatus.ACTIVE
    assert val_res.session_context is not None
    assert val_res.session_context.tenant_id == "tenant_alpha"


# ==============================================================================
# 2. unauthenticated cannot create
# ==============================================================================
def test_2_unauthenticated_cannot_create_session(saas_session_service):
    unauth = AuthenticationResult(
        status=AuthenticationStatus.UNAUTHENTICATED,
        method=AuthenticationMethod.BEARER_TOKEN,
        provider="custom_jwt",
        principal=None,
        reason_codes=("INVALID_SIGNATURE",),
    )

    with pytest.raises(SessionValidationError) as exc:
        saas_session_service.create_session(
            auth_result=unauth,
            tenant_id="tenant_alpha",
        )
    assert "Cannot create SaaS session from unauthenticated result" in str(exc.value)


# ==============================================================================
# 3. tenant binding required
# ==============================================================================
def test_3_tenant_binding_required(saas_session_service, valid_auth_result):
    with pytest.raises((ValueError, Exception)):
        saas_session_service.create_session(
            auth_result=valid_auth_result,
            tenant_id="",  # Invalido
        )

    with pytest.raises((ValueError, Exception)):
        saas_session_service.create_session(
            auth_result=valid_auth_result,
            tenant_id="../invalid/path",
        )


# ==============================================================================
# 4. cross-tenant binding rejected
# ==============================================================================
def test_4_cross_tenant_binding_rejected(saas_session_service, valid_auth_result):
    # usr_alice_01 está vinculada a tenant_alpha en el mapping
    with pytest.raises(CrossTenantAccessError) as exc:
        saas_session_service.create_session(
            auth_result=valid_auth_result,
            tenant_id="tenant_beta",  # Diferente a tenant_alpha
        )
    assert "CROSS_TENANT_SESSION_DENIED" in str(exc.value)


# ==============================================================================
# 5. organization membership validation
# ==============================================================================
def test_5_organization_membership_validation(
    saas_session_service, org_service, membership_service, valid_auth_result
):
    ctx_a = TenantContext(tenant_id="tenant_alpha", identity_id="usr_alice_01")
    org_service.create_organization(ctx_a, organization_id="org_alpha_core", name="Alpha Core")

    # Intentar crear sesión con org antes de tener membresía
    with pytest.raises(SessionValidationError) as exc:
        saas_session_service.create_session(
            auth_result=valid_auth_result,
            tenant_id="tenant_alpha",
            organization_id="org_alpha_core",
        )
    assert "has no membership in organization" in str(exc.value)

    # Agregar membresía
    membership_service.add_membership(
        ctx_a, organization_id="org_alpha_core", identity_id="usr_alice_01", role=MembershipRole.ADMIN
    )

    # Crear sesión con org válida
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
        organization_id="org_alpha_core",
    )
    assert session.organization_id == "org_alpha_core"

    # Suspender membresía y validar que session validation falle
    membership_service.update_membership_status(
        ctx_a, membership_id="mem_tenant_alpha_org_alpha_core_usr_alice_01", new_status=MembershipStatus.SUSPENDED
    )
    val_res = saas_session_service.validate_session(session.session_id, expected_tenant_id="tenant_alpha")
    assert val_res.is_valid is False
    assert SessionValidationReasonCode.MEMBERSHIP_INACTIVE.value in val_res.reason_codes


# ==============================================================================
# 6. expired session invalid
# ==============================================================================
def test_6_expired_session_invalid(saas_session_service, valid_auth_result, clock):
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
        ttl_seconds=300,  # 5 minutos
    )

    # Validar que está activa inicialmente
    res1 = saas_session_service.validate_session(session.session_id, expected_tenant_id="tenant_alpha")
    assert res1.is_valid is True

    # Avanzar reloj 301 segundos
    clock.advance(301)

    res2 = saas_session_service.validate_session(session.session_id, expected_tenant_id="tenant_alpha")
    assert res2.is_valid is False
    assert res2.status == SessionStatus.EXPIRED
    assert SessionValidationReasonCode.SESSION_EXPIRED.value in res2.reason_codes

    # get_session_context lanza SessionExpiredError
    with pytest.raises(SessionExpiredError):
        saas_session_service.get_session_context(session.session_id, expected_tenant_id="tenant_alpha")


# ==============================================================================
# 7. revoked session invalid
# ==============================================================================
def test_7_revoked_session_invalid(saas_session_service, valid_auth_result):
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
    )

    revoked = saas_session_service.revoke_session(
        session_id=session.session_id,
        tenant_id="tenant_alpha",
        reason="Security compliance rotation",
    )
    assert revoked.status == SessionStatus.REVOKED
    assert revoked.metadata.get("revocation_reason") == "Security compliance rotation"

    val_res = saas_session_service.validate_session(session.session_id, expected_tenant_id="tenant_alpha")
    assert val_res.is_valid is False
    assert val_res.status == SessionStatus.REVOKED
    assert SessionValidationReasonCode.SESSION_REVOKED.value in val_res.reason_codes

    with pytest.raises(SessionRevokedError):
        saas_session_service.get_session_context(session.session_id, expected_tenant_id="tenant_alpha")


# ==============================================================================
# 8. tampered session invalid
# ==============================================================================
def test_8_tampered_session_invalid(temp_dir, saas_session_service, valid_auth_result):
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
    )

    # Manipular físicamente el archivo JSON en disco
    file_path = temp_dir / "tenants" / "tenant_alpha" / "sessions" / f"{session.session_id}.json"
    assert file_path.exists()

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["identity_id"] = "usr_hacker_evil"  # Modificación maliciosa sin actualizar checksum

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Intentar validar la sesión manipulada -> checksum mismatch detectado al deserializar o None retornado
    val_res = saas_session_service.validate_session(session.session_id, expected_tenant_id="tenant_alpha")
    assert val_res.is_valid is False
    assert val_res.status == SessionStatus.INVALID


# ==============================================================================
# 9. session id != token
# ==============================================================================
def test_9_session_id_distinct_from_token(saas_session_service):
    secret_token = "oauth2_bearer_secret_1234567890abcdef_never_use_as_session_id"
    principal = IdentityReference(
        identity_id="usr_alice_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:google:alice@example.com",
    )
    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=principal,
        authenticated_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc),
    )

    session = saas_session_service.create_session(
        auth_result=auth_result,
        tenant_id="tenant_alpha",
    )

    assert session.session_id != secret_token
    assert secret_token not in session.session_id
    assert session.identity_id not in session.session_id
    assert session.tenant_id not in session.session_id


# ==============================================================================
# 10. no credentials in session
# ==============================================================================
def test_10_no_credentials_in_session(saas_session_service, valid_auth_result):
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
        metadata={"token": "should_be_sanitized", "api_key": "redact_me", "user_agent": "Mozilla/5.0"},
    )

    d = session.to_dict()
    assert "password" not in d
    assert "access_token" not in d
    assert "refresh_token" not in d
    assert "api_key" not in d or d["metadata"].get("api_key") == "[REDACTED]"
    assert d["metadata"].get("token") == "[REDACTED]"
    assert d["metadata"].get("user_agent") == "Mozilla/5.0"


# ==============================================================================
# 11. multiple tenant sessions isolated
# ==============================================================================
def test_11_multiple_tenant_sessions_isolated(
    session_repo, tenant_service, org_repo, membership_repo, clock
):
    # Permitir a usr_multitenant estar en tenant_alpha y tenant_gamma
    tenant_svc = TenantContextService(
        registered_tenants={"tenant_alpha", "tenant_gamma"},
        identity_to_tenant={},  # Sin bloqueo exclusivo
    )
    svc = SaaSSessionService(
        session_repository=session_repo,
        tenant_resolver=tenant_svc,
        tenant_mapping=tenant_svc,
        organization_repo=org_repo,
        membership_repo=membership_repo,
        clock=clock,
    )

    principal = IdentityReference(
        identity_id="usr_multitenant",
        identity_type=IdentityType.USER,
        canonical_identifier="user:jwt:usr_multitenant",
    )
    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.BEARER_TOKEN,
        provider="jwt",
        principal=principal,
    )

    sess_alpha = svc.create_session(auth, tenant_id="tenant_alpha")
    sess_gamma = svc.create_session(auth, tenant_id="tenant_gamma")

    assert sess_alpha.session_id != sess_gamma.session_id
    assert sess_alpha.tenant_id == "tenant_alpha"
    assert sess_gamma.tenant_id == "tenant_gamma"

    # Validar que buscar sess_alpha bajo tenant_gamma falle (Cross-tenant guard)
    res_mismatch = svc.validate_session(sess_alpha.session_id, expected_tenant_id="tenant_gamma")
    assert res_mismatch.is_valid is False
    assert SessionValidationReasonCode.TENANT_MISMATCH.value in res_mismatch.reason_codes


# ==============================================================================
# 12. deterministic tenant/org context
# ==============================================================================
def test_12_deterministic_tenant_org_context(saas_session_service, valid_auth_result):
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
        correlation_id="corr_flow_42",
    )

    sess_ctx = saas_session_service.get_session_context(
        session.session_id, expected_tenant_id="tenant_alpha", correlation_id="corr_flow_42"
    )

    assert sess_ctx.session_id == session.session_id
    assert sess_ctx.identity_id == "usr_alice_01"
    assert sess_ctx.tenant_id == "tenant_alpha"
    assert sess_ctx.correlation_id == "corr_flow_42"
    assert sess_ctx.checksum != ""

    tenant_ctx = sess_ctx.to_tenant_context()
    assert isinstance(tenant_ctx, TenantContext)
    assert tenant_ctx.tenant_id == "tenant_alpha"
    assert tenant_ctx.identity_id == "usr_alice_01"
    assert tenant_ctx.correlation_id == "corr_flow_42"
    assert tenant_ctx.metadata.get("session_id") == session.session_id


# ==============================================================================
# 13. logout revokes
# ==============================================================================
def test_13_logout_revokes_session(saas_session_service, valid_auth_result):
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
    )

    logged_out = saas_session_service.logout(session.session_id, tenant_id="tenant_alpha")
    assert logged_out.status == SessionStatus.REVOKED
    assert logged_out.metadata.get("revocation_reason") == "LOGOUT"

    val_res = saas_session_service.validate_session(session.session_id, expected_tenant_id="tenant_alpha")
    assert val_res.is_valid is False
    assert val_res.status == SessionStatus.REVOKED


# ==============================================================================
# 14. restart-safe semantics
# ==============================================================================
def test_14_restart_safe_semantics(temp_dir, valid_auth_result, clock, tenant_service):
    repo1 = JsonSaaSSessionRepository(temp_dir)
    svc1 = SaaSSessionService(session_repository=repo1, tenant_resolver=tenant_service, clock=clock)

    session = svc1.create_session(auth_result=valid_auth_result, tenant_id="tenant_alpha")
    sess_id = session.session_id

    # Simular reinicio creando nuevas instancias de repositorio y servicio sobre el mismo storage_dir
    repo2 = JsonSaaSSessionRepository(temp_dir)
    svc2 = SaaSSessionService(session_repository=repo2, tenant_resolver=tenant_service, clock=clock)

    reloaded = repo2.get_by_id(sess_id, tenant_id="tenant_alpha")
    assert reloaded is not None
    assert reloaded.session_id == sess_id
    assert reloaded.status == SessionStatus.ACTIVE
    assert reloaded.checksum == session.checksum

    val_res = svc2.validate_session(sess_id, expected_tenant_id="tenant_alpha")
    assert val_res.is_valid is True
    assert val_res.status == SessionStatus.ACTIVE


# ==============================================================================
# 15. session != authorization
# ==============================================================================
def test_15_session_distinct_from_authorization(saas_session_service, valid_auth_result):
    session = saas_session_service.create_session(
        auth_result=valid_auth_result,
        tenant_id="tenant_alpha",
    )

    # SaaSSession y SessionContext demuestran autenticación y contexto tenant/org, NO permisos ni roles RBAC
    assert not hasattr(session, "permissions")
    assert not hasattr(session, "roles")
    assert not hasattr(session, "can_execute")

    sess_ctx = session.to_context()
    assert not hasattr(sess_ctx, "permissions")
    assert not hasattr(sess_ctx, "roles")


# ==============================================================================
# 16. no O.4+ implementation
# ==============================================================================
def test_16_no_o4_plus_implementation():
    import src.domain.session.models as session_models
    import src.application.session.saas_session_service as session_app

    # Asegurar que no existan conceptos de O.4 (SaaS RBAC/ABAC SaaS policies), billing, tiers, quotas, usage metering
    for forbidden in ["Billing", "SubscriptionPlan", "QuotaLimit", "UsageMeter", "SaaSAuthorizationPolicy"]:
        assert not hasattr(session_models, forbidden)
        assert not hasattr(session_app, forbidden)
