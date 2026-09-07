"""
Tests unitarios para N.2 Authentication (Transversal N Security, Governance y Safety).

Cubre los 14 requerimientos mínimos exigidos por el Hito N.2:
1. Valid authentication (OAuth & Internal assertion)
2. Invalid credentials/token
3. Expired auth
4. UNKNOWN preserved (no fallback permisivo)
5. Same subject -> same identity (estabilidad de identidad N.1)
6. Token is not identity (identidad desacoplada de tokens efímeros)
7. Provider mismatch / Validation
8. Method validation (enums canónicos y protección de tipos)
9. Secret sanitization (recursivo en metadata de requests y results)
10. No token in result (cero secretos expuestos en modelos o representaciones)
11. Identity != authenticated (una identidad conocida no implica estar autenticada)
12. Authentication != authorization (autenticación exitosa no otorga permisos N.3)
13. Replay/state safety (idempotencia y detección de estados inconsistentes)
14. Deterministic result semantics (checksum SHA-256 e inmutabilidad frozen)
"""

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
import pytest

from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationRequest,
    AuthenticationResult,
    PrincipalContext,
    compute_auth_result_checksum,
)
from src.domain.identity.models import (
    Identity,
    IdentityType,
    IdentityStatus,
    IdentityReference,
    create_oauth_user_identity,
)
from src.domain.oauth.models import OAuthConnection
from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import AuthenticationService
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.domain.security.models import SENSITIVE_KEYS


class InMemoryIdentityRepo:
    def __init__(self):
        self._identities = {}
        self._by_canonical = {}
        self._by_external = {}

    def save_identity(self, identity: Identity) -> Identity:
        self._identities[identity.identity_id] = identity
        self._by_canonical[identity.canonical_identifier] = identity
        if identity.provider and identity.external_subject_id:
            key = f"{identity.provider.lower()}:{identity.external_subject_id.lower()}"
            self._by_external[key] = identity
        return identity

    def get_identity(self, identity_id: str):
        return self._identities.get(identity_id)

    def find_by_canonical_identifier(self, canonical_identifier: str):
        return self._by_canonical.get(canonical_identifier)

    def find_by_external_subject(self, provider: str, external_subject_id: str):
        key = f"{provider.lower()}:{external_subject_id.lower()}"
        return self._by_external.get(key)

    def list_identities(self, identity_type=None, provider=None, status=None, limit=100):
        res = list(self._identities.values())
        if identity_type:
            res = [i for i in res if i.identity_type == identity_type]
        if provider:
            res = [i for i in res if i.provider == provider]
        if status:
            res = [i for i in res if i.status == status]
        return res[:limit]

    def exists(self, identity_id: str) -> bool:
        return identity_id in self._identities


@pytest.fixture
def identity_service():
    repo = InMemoryIdentityRepo()
    return IdentityService(repository=repo)


@pytest.fixture
def virtual_clock():
    start_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def auth_service(identity_service, virtual_clock):
    return AuthenticationService(
        identity_service=identity_service,
        clock=virtual_clock,
        trusted_internal_tokens={"internal-secret-token-xyz": "autonomous_loop"},
        trusted_api_credentials={"api-key-12345": "partner_service"},
    )


# ---------------------------------------------------------------------------
# 1. Valid Authentication
# ---------------------------------------------------------------------------
def test_valid_oauth_authentication(auth_service, virtual_clock):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="123456789",
        access_token="APP_USR_VALID_TOKEN",
        refresh_token="REFRESH_TOKEN",
        expires_at=virtual_clock.now() + timedelta(hours=6),
    )

    result = auth_service.authenticate_oauth_connection(conn, correlation_id="corr-001")

    assert result.is_authenticated is True
    assert result.status == AuthenticationStatus.AUTHENTICATED
    assert result.method == AuthenticationMethod.OAUTH2
    assert result.provider == "mercadolibre"
    assert result.principal is not None
    assert result.principal.identity_id == "usr_mercadolibre_123456789"
    assert result.principal.identity_type == IdentityType.USER
    assert result.correlation_id == "corr-001"
    assert "AUTHENTICATION_SUCCESS" in result.reason_codes
    assert result.checksum is not None


def test_valid_internal_service_authentication(auth_service):
    request = AuthenticationRequest(
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal",
        token_or_secret="internal-secret-token-xyz",
        declared_subject="autonomous_loop",
        correlation_id="corr-internal-01",
    )

    result = auth_service.authenticate_request(request)

    assert result.is_authenticated is True
    assert result.status == AuthenticationStatus.AUTHENTICATED
    assert result.method == AuthenticationMethod.INTERNAL_SERVICE
    assert result.principal is not None
    assert result.principal.identity_id == "agent_autonomous_loop"
    assert result.principal.identity_type == IdentityType.AGENT


# ---------------------------------------------------------------------------
# 2. Invalid Credentials / Token
# ---------------------------------------------------------------------------
def test_invalid_internal_credentials(auth_service):
    request = AuthenticationRequest(
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal",
        token_or_secret="fake-untrusted-token",
        declared_subject="autonomous_loop",
    )

    result = auth_service.authenticate_request(request)

    assert result.is_authenticated is False
    assert result.status == AuthenticationStatus.INVALID
    assert result.principal is None
    assert "INVALID_INTERNAL_CREDENTIAL" in result.reason_codes


def test_missing_access_token_in_oauth(auth_service, virtual_clock):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="123456789",
        access_token="",  # vacío
        refresh_token="REFRESH_TOKEN",
        expires_at=virtual_clock.now() + timedelta(hours=1),
    )

    result = auth_service.authenticate_oauth_connection(conn)

    assert result.is_authenticated is False
    assert result.status == AuthenticationStatus.INVALID
    assert "MISSING_ACCESS_TOKEN" in result.reason_codes


# ---------------------------------------------------------------------------
# 3. Expired Auth
# ---------------------------------------------------------------------------
def test_expired_oauth_token(auth_service, virtual_clock):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="123456789",
        access_token="APP_USR_EXPIRED",
        refresh_token="REFRESH_TOKEN",
        expires_at=virtual_clock.now() - timedelta(minutes=5),  # en el pasado
    )

    result = auth_service.authenticate_oauth_connection(conn)

    assert result.is_authenticated is False
    assert result.status == AuthenticationStatus.EXPIRED
    assert result.principal is None
    assert "TOKEN_EXPIRED" in result.reason_codes


# ---------------------------------------------------------------------------
# 4. UNKNOWN Preserved (No Permissive Fallback)
# ---------------------------------------------------------------------------
def test_unknown_method_preserves_unknown_status(auth_service):
    request = AuthenticationRequest(
        method=AuthenticationMethod.UNKNOWN,
        provider="unrecognized_provider",
        token_or_secret="some_token",
    )

    result = auth_service.authenticate_request(request)

    assert result.is_authenticated is False
    assert result.status == AuthenticationStatus.UNKNOWN
    assert result.principal is None
    assert "UNKNOWN_AUTHENTICATION_METHOD" in result.reason_codes


# ---------------------------------------------------------------------------
# 5. Same Subject -> Same Identity (N.1 Stability)
# ---------------------------------------------------------------------------
def test_same_subject_different_tokens_yield_same_identity(auth_service, virtual_clock):
    # Primer token
    conn1 = OAuthConnection(
        provider="mercadolibre",
        user_id="998877",
        access_token="TOKEN_ALPHA",
        refresh_token="REFRESH_ALPHA",
        expires_at=virtual_clock.now() + timedelta(hours=2),
    )
    result1 = auth_service.authenticate_oauth_connection(conn1)

    # Segundo token (rotado) para el mismo user_id
    conn2 = OAuthConnection(
        provider="mercadolibre",
        user_id="998877",
        access_token="TOKEN_BETA",
        refresh_token="REFRESH_BETA",
        expires_at=virtual_clock.now() + timedelta(hours=4),
    )
    result2 = auth_service.authenticate_oauth_connection(conn2)

    assert result1.is_authenticated is True
    assert result2.is_authenticated is True
    assert result1.principal.identity_id == result2.principal.identity_id == "usr_mercadolibre_998877"
    assert result1.principal.canonical_identifier == result2.principal.canonical_identifier


# ---------------------------------------------------------------------------
# 6. Token Is Not Identity
# ---------------------------------------------------------------------------
def test_token_is_not_identity(auth_service, virtual_clock):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="12345",
        access_token="SUPER_SECRET_TOKEN_STRING",
        refresh_token="SUPER_SECRET_REFRESH",
        expires_at=virtual_clock.now() + timedelta(hours=1),
    )
    result = auth_service.authenticate_oauth_connection(conn)

    # El identity_id debe basarse en el provider y subject, NUNCA en el token string
    assert "SUPER_SECRET_TOKEN_STRING" not in result.principal.identity_id
    assert "SUPER_SECRET_TOKEN_STRING" not in result.principal.canonical_identifier
    assert result.principal.identity_id == "usr_mercadolibre_12345"


# ---------------------------------------------------------------------------
# 7. Provider Mismatch & Declared Subject Mismatch
# ---------------------------------------------------------------------------
def test_declared_subject_mismatch(auth_service):
    # Token pertenece a 'autonomous_loop', pero la request declara 'malicious_agent'
    request = AuthenticationRequest(
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal",
        token_or_secret="internal-secret-token-xyz",
        declared_subject="malicious_agent",
    )

    result = auth_service.authenticate_request(request)

    assert result.is_authenticated is False
    assert result.status == AuthenticationStatus.INVALID
    assert "DECLARED_SUBJECT_MISMATCH" in result.reason_codes


# ---------------------------------------------------------------------------
# 8. Method Validation & Enum Handling
# ---------------------------------------------------------------------------
def test_method_validation_and_invalid_values():
    with pytest.raises(ValueError, match="Invalid AuthenticationMethod"):
        AuthenticationRequest(
            method="NON_EXISTENT_METHOD",
            provider="internal",
        )

    with pytest.raises(ValueError, match="Invalid AuthenticationStatus"):
        AuthenticationResult(
            status="INVALID_STATUS_NAME",
            method=AuthenticationMethod.OAUTH2,
            provider="mercadolibre",
        )


# ---------------------------------------------------------------------------
# 9. Secret Sanitization in Request and Result Metadata
# ---------------------------------------------------------------------------
def test_secret_sanitization_in_metadata(auth_service, virtual_clock):
    meta = {
        "access_token": "secret_access_value",
        "api_key": "secret_key_123",
        "password": "my_super_password",
        "operation": "sync_inventory",
        "nested": {
            "bearer_token": "secret_bearer_abc",
            "safe_param": "public_data",
        }
    }

    request = AuthenticationRequest(
        method=AuthenticationMethod.UNKNOWN,
        provider="test_provider",
        metadata=meta,
    )

    # Las llaves sensibles deben estar redactadas
    assert request.metadata["access_token"] == "[REDACTED]"
    assert request.metadata["api_key"] == "[REDACTED]"
    assert request.metadata["password"] == "[REDACTED]"
    assert request.metadata["operation"] == "sync_inventory"
    assert request.metadata["nested"]["bearer_token"] == "[REDACTED]"
    assert request.metadata["nested"]["safe_param"] == "public_data"


# ---------------------------------------------------------------------------
# 10. No Token in Result
# ---------------------------------------------------------------------------
def test_no_token_in_result_attributes_or_repr(auth_service, virtual_clock):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="555444",
        access_token="EXPOSED_RAW_TOKEN_999",
        refresh_token="EXPOSED_RAW_REFRESH_888",
        expires_at=virtual_clock.now() + timedelta(hours=1),
    )

    result = auth_service.authenticate_oauth_connection(conn)

    # Verificar que el resultado no almacena los strings secretos en ningún atributo
    result_str = str(result)
    result_repr = repr(result)

    assert "EXPOSED_RAW_TOKEN_999" not in result_str
    assert "EXPOSED_RAW_TOKEN_999" not in result_repr
    assert "EXPOSED_RAW_REFRESH_888" not in result_str
    assert "EXPOSED_RAW_REFRESH_888" not in result_repr
    assert not hasattr(result, "access_token")
    assert not hasattr(result, "token")
    assert not hasattr(result, "secret")


# ---------------------------------------------------------------------------
# 11. Identity != Authenticated
# ---------------------------------------------------------------------------
def test_identity_existence_does_not_imply_authenticated(auth_service, identity_service, virtual_clock):
    # Registramos una identidad en N.1
    identity = identity_service.register_oauth_user(
        provider="mercadolibre",
        user_id="user_known_001",
    )
    assert identity is not None
    assert identity.identity_id == "usr_mercadolibre_user_known_001"

    # Presentamos una conexión con token expirado para ese mismo usuario
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="user_known_001",
        access_token="EXPIRED_TOKEN",
        refresh_token="REFRESH_TOKEN",
        expires_at=virtual_clock.now() - timedelta(minutes=1),
    )

    auth_result = auth_service.authenticate_oauth_connection(conn)

    # La identidad existe en N.1, pero la autenticación N.2 DEBE fallar
    assert auth_result.is_authenticated is False
    assert auth_result.status == AuthenticationStatus.EXPIRED


# ---------------------------------------------------------------------------
# 12. Authentication != Authorization
# ---------------------------------------------------------------------------
def test_authentication_does_not_grant_authorization():
    principal = IdentityReference(
        identity_id="usr_mercadolibre_1234",
        identity_type=IdentityType.USER,
        canonical_identifier="user:mercadolibre:1234",
    )

    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="mercadolibre",
        principal=principal,
    )

    ctx = PrincipalContext(principal=principal, auth_result=auth_result)

    # PrincipalContext solo expone estado de autenticación, NO permisos ni capacidades de autorización
    assert ctx.is_authenticated is True
    assert not hasattr(ctx, "permissions")
    assert not hasattr(ctx, "roles")
    assert not hasattr(ctx, "is_authorized")
    assert not hasattr(ctx, "allowed_actions")


# ---------------------------------------------------------------------------
# 13. Replay / State Safety & Idempotent Principal Resolution
# ---------------------------------------------------------------------------
def test_replay_and_idempotence(auth_service, virtual_clock):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="replay_user_1",
        access_token="REPLAY_TOKEN",
        refresh_token="REFRESH",
        expires_at=virtual_clock.now() + timedelta(hours=2),
    )

    # Ejecutar 5 veces idénticas
    results = [auth_service.authenticate_oauth_connection(conn) for _ in range(5)]

    for r in results:
        assert r.is_authenticated is True
        assert r.principal.identity_id == "usr_mercadolibre_replay_user_1"
        assert r.checksum == results[0].checksum


# ---------------------------------------------------------------------------
# 14. Deterministic Result Semantics and Immutability
# ---------------------------------------------------------------------------
def test_deterministic_checksum_and_immutability(auth_service, virtual_clock):
    fixed_time = datetime(2026, 9, 4, 15, 30, 0, tzinfo=timezone.utc)
    principal = IdentityReference(
        identity_id="agent_sync",
        identity_type=IdentityType.AGENT,
        canonical_identifier="agent:internal:sync",
    )

    res1 = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal",
        principal=principal,
        authenticated_at=fixed_time,
        reason_codes=("AUTHENTICATION_SUCCESS",),
        correlation_id="corr-det-1",
        metadata={"node": "worker-1"},
    )

    res2 = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal",
        principal=principal,
        authenticated_at=fixed_time,
        reason_codes=("AUTHENTICATION_SUCCESS",),
        correlation_id="corr-det-1",
        metadata={"node": "worker-1"},
    )

    # Checksum determinista
    assert res1.checksum == res2.checksum

    # Inmutabilidad (frozen dataclass)
    with pytest.raises(Exception):  # FrozenInstanceError
        res1.status = AuthenticationStatus.UNAUTHENTICATED

    with pytest.raises(Exception):
        res1.metadata["new_key"] = "tampering"
