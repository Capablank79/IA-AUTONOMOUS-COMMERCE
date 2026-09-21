"""
Pruebas de Integración y E2E para N.2 — Authentication.

Cubre los escenarios exigidos:
A. Valid MercadoLibre OAuth context -> N.1 identity -> AUTHENTICATED
B. Expired token/context -> not authenticated
C. Invalid provider/subject -> INVALID/UNAUTHENTICATED
D. Token rotation -> same stable identity
E. Restart -> identity persists; auth result correctly re-evaluated
F. Audit/Trace records safe (sin tokens ni secretos)
G. No secrets persisted en disco (JSON)
H. Internal agent/service auth path (contrato explícito requerido)
I. E2E: Actor -> N.1 Identity -> N.2 Authentication -> PrincipalContext -> operation boundary
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationRequest,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.identity.models import IdentityType, IdentityReference
from src.domain.oauth.models import OAuthConnection
from src.domain.audit.models import AuditRecord, AuditRecordType
from src.domain.agent_trace.models import AgentTraceRecord

from src.infrastructure.persistence.data.json.identity_repository import (
    JsonIdentityRepository,
)
from src.infrastructure.persistence.data.json.audit_repository import (
    JsonAuditRepository,
)
from src.infrastructure.persistence.data.json.agent_trace_repository import (
    JsonAgentTraceRepository,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock

from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import (
    AuthenticationService,
)
from src.application.agent_trace.agent_trace_service import AgentTraceService

TEST_TOKEN = "APP_USR_TEST_ACCESS_TOKEN_SECRET"
TEST_REFRESH = "APP_USR_TEST_REFRESH_TOKEN_SECRET"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "n2_auth_db"


@pytest.fixture
def identity_repo(data_dir: Path) -> JsonIdentityRepository:
    return JsonIdentityRepository(data_dir / "identity")


@pytest.fixture
def identity_service(identity_repo: JsonIdentityRepository) -> IdentityService:
    return IdentityService(identity_repo)


@pytest.fixture
def audit_repo(data_dir: Path) -> JsonAuditRepository:
    return JsonAuditRepository(data_dir / "audit")


@pytest.fixture
def trace_repo(data_dir: Path) -> JsonAgentTraceRepository:
    return JsonAgentTraceRepository(data_dir / "trace")


@pytest.fixture
def trace_service(trace_repo: JsonAgentTraceRepository) -> AgentTraceService:
    return AgentTraceService(trace_repo)


@pytest.fixture
def virtual_clock() -> VirtualClock:
    start_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def auth_service(
    identity_service: IdentityService,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> AuthenticationService:
    return AuthenticationService(
        identity_service=identity_service,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
        trusted_internal_tokens={"n2-internal-token-001": "autonomous_loop"},
        trusted_api_credentials={"n2-api-key-001": "partner_gateway"},
    )


# ---------------------------------------------------------------------------
# A. Valid MercadoLibre OAuth context -> N.1 identity -> AUTHENTICATED
# ---------------------------------------------------------------------------
def test_scenario_a_valid_oauth_authenticated(
    auth_service: AuthenticationService,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_100001",
        access_token=TEST_TOKEN,
        refresh_token=TEST_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=6),
    )

    result = auth_service.authenticate_oauth_connection(
        conn, correlation_id="corr-n2-a"
    )

    # Resultado explícito N.2
    assert result.status == AuthenticationStatus.AUTHENTICATED
    assert result.is_authenticated is True
    assert result.method == AuthenticationMethod.OAUTH2
    assert result.provider == "mercadolibre"

    # Resolución a identidad N.1 estable
    assert result.principal is not None
    assert result.principal.identity_type == IdentityType.USER
    assert result.principal.identity_id == "usr_mercadolibre_MLA_100001"
    assert result.principal.canonical_identifier == "user:mercadolibre:mla_100001"

    # La identidad quedó registrada en N.1
    stored = auth_service.identity_service.get_identity("usr_mercadolibre_MLA_100001")
    assert stored is not None
    assert stored.external_subject_id == "MLA_100001"

    # Contexto seguro downstream (sin token)
    ctx = auth_service.create_principal_context(result)
    assert isinstance(ctx, PrincipalContext)
    assert ctx.is_authenticated is True
    assert ctx.identity_id == "usr_mercadolibre_MLA_100001"
    assert ctx.method == AuthenticationMethod.OAUTH2
    assert ctx.expires_at == conn.expires_at
    # El contexto NUNCA expone el token
    assert not hasattr(ctx, "token")
    content = str(ctx)
    assert TEST_TOKEN not in content
    assert TEST_REFRESH not in content


# ---------------------------------------------------------------------------
# B. Expired token/context -> not authenticated
# ---------------------------------------------------------------------------
def test_scenario_b_expired_token_rejected(
    auth_service: AuthenticationService,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_100002",
        access_token=TEST_TOKEN,
        refresh_token=TEST_REFRESH,
        expires_at=virtual_clock.now() - timedelta(minutes=5),  # ya expirado
    )

    result = auth_service.authenticate_oauth_connection(conn, correlation_id="corr-n2-b")

    assert result.is_authenticated is False
    assert result.status == AuthenticationStatus.EXPIRED
    assert result.principal is None
    assert "TOKEN_EXPIRED" in result.reason_codes

    # No debe crearse contexto autenticado a partir de un resultado no autenticado
    with pytest.raises(ValueError):
        auth_service.create_principal_context(result)


# ---------------------------------------------------------------------------
# C. Invalid provider/subject -> INVALID / UNAUTHENTICATED
# ---------------------------------------------------------------------------
def test_scenario_c_invalid_provider_and_subject(
    auth_service: AuthenticationService, virtual_clock: VirtualClock
):
    # C1. Token ausente
    conn_no_token = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_100003",
        access_token="",
        refresh_token=TEST_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=1),
    )
    r1 = auth_service.authenticate_oauth_connection(conn_no_token)
    assert r1.status == AuthenticationStatus.INVALID
    assert r1.is_authenticated is False
    assert "MISSING_ACCESS_TOKEN" in r1.reason_codes

    # C2. user_id inválido
    conn_bad_user = OAuthConnection(
        provider="mercadolibre",
        user_id="   ",
        access_token=TEST_TOKEN,
        refresh_token=TEST_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=1),
    )
    r2 = auth_service.authenticate_oauth_connection(conn_bad_user)
    assert r2.status == AuthenticationStatus.INVALID
    assert r2.is_authenticated is False

    # C3. Credencial API inexistente
    req_invalid = AuthenticationRequest(
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="mercadolibre",
        token_or_secret="not-a-real-api-key",
        declared_subject="partner_gateway",
    )
    r3 = auth_service.authenticate_request(req_invalid)
    assert r3.status == AuthenticationStatus.INVALID
    assert r3.is_authenticated is False
    assert "INVALID_CREDENTIALS" in r3.reason_codes


# ---------------------------------------------------------------------------
# D. Token rotation -> same stable identity
# ---------------------------------------------------------------------------
def test_scenario_d_token_rotation_same_identity(
    auth_service: AuthenticationService, virtual_clock: VirtualClock
):
    # Primer token (válido)
    conn1 = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_ROTATION",
        access_token="TOKEN_V1_SECRET",
        refresh_token="REFRESH_V1_SECRET",
        expires_at=virtual_clock.now() + timedelta(hours=2),
    )
    r1 = auth_service.authenticate_oauth_connection(conn1, correlation_id="corr-rot-1")
    assert r1.status == AuthenticationStatus.AUTHENTICATED
    id_before = r1.principal.identity_id

    # Token rotado (nuevo access_token + nuevo refresh_token, mismo subject)
    conn2 = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_ROTATION",
        access_token="TOKEN_V2_SECRET",
        refresh_token="REFRESH_V2_SECRET",
        expires_at=virtual_clock.now() + timedelta(hours=4),
    )
    r2 = auth_service.authenticate_oauth_connection(conn2, correlation_id="corr-rot-2")
    assert r2.status == AuthenticationStatus.AUTHENTICATED
    id_after = r2.principal.identity_id

    # Rotación de token NO debe crear una nueva identidad
    assert id_before == id_after == "usr_mercadolibre_MLA_ROTATION"
    assert r1.principal.canonical_identifier == r2.principal.canonical_identifier

    # Solo una identidad registrada para este subject
    found = auth_service.identity_service.repository.find_by_external_subject(
        "mercadolibre", "MLA_ROTATION"
    )
    assert found is not None
    assert found.identity_id == id_before


# ---------------------------------------------------------------------------
# E. Restart -> identity persists; auth result correctly re-evaluated
# ---------------------------------------------------------------------------
def test_scenario_e_restart_persists_identity_and_reevaluates(data_dir: Path, virtual_clock: VirtualClock):
    repo_dir = data_dir / "identity"

    # Ciclo 1: registrar + autenticar (persistencia N.1)
    repo1 = JsonIdentityRepository(repo_dir)
    svc1 = AuthenticationService(
        identity_service=IdentityService(repo1),
        clock=virtual_clock,
    )
    r1 = svc1.authenticate_oauth_connection(
        OAuthConnection(
            provider="mercadolibre",
            user_id="MLA_RESTART",
            access_token="TOKEN_RESTART_V1",
            refresh_token="REFRESH_RESTART_V1",
            expires_at=virtual_clock.now() + timedelta(hours=3),
        ),
        correlation_id="corr-restart-1",
    )
    assert r1.status == AuthenticationStatus.AUTHENTICATED
    assert r1.principal.identity_id == "usr_mercadolibre_MLA_RESTART"

    # Simular reinicio: nueva instancia de repositorio sobre el mismo directorio
    repo2 = JsonIdentityRepository(repo_dir)
    svc2 = AuthenticationService(
        identity_service=IdentityService(repo2),
        clock=virtual_clock,
    )

    # La identidad N.1 persiste tras reinicio
    stored = svc2.identity_service.get_identity("usr_mercadolibre_MLA_RESTART")
    assert stored is not None
    assert stored.external_subject_id == "MLA_RESTART"

    # Re-evaluación autenticada con token rotado y mismo subject -> misma identidad
    r2 = svc2.authenticate_oauth_connection(
        OAuthConnection(
            provider="mercadolibre",
            user_id="MLA_RESTART",
            access_token="TOKEN_RESTART_V2",
            refresh_token="REFRESH_RESTART_V2",
            expires_at=virtual_clock.now() + timedelta(hours=3),
        ),
        correlation_id="corr-restart-2",
    )
    assert r2.status == AuthenticationStatus.AUTHENTICATED
    assert r2.principal.identity_id == "usr_mercadolibre_MLA_RESTART"

    # Re-evaluación con token expirado tras reinicio -> NO autenticado
    r3 = svc2.authenticate_oauth_connection(
        OAuthConnection(
            provider="mercadolibre",
            user_id="MLA_RESTART",
            access_token="TOKEN_RESTART_EXPIRED",
            refresh_token="REFRESH_RESTART_EXPIRED",
            expires_at=virtual_clock.now() - timedelta(seconds=30),
        ),
        correlation_id="corr-restart-3",
    )
    assert r3.status == AuthenticationStatus.EXPIRED
    assert r3.is_authenticated is False


# ---------------------------------------------------------------------------
# F. Audit/Trace records safe (sin tokens ni secretos)
# ---------------------------------------------------------------------------
def test_scenario_f_audit_trace_records_safe(
    auth_service: AuthenticationService,
    audit_repo: JsonAuditRepository,
    trace_repo: JsonAgentTraceRepository,
    virtual_clock: VirtualClock,
):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_AUDIT_SAFE",
        access_token="AUDIT_TOP_SECRET_TOKEN",
        refresh_token="AUDIT_TOP_SECRET_REFRESH",
        expires_at=virtual_clock.now() + timedelta(hours=2),
    )
    auth_service.authenticate_oauth_connection(conn, correlation_id="corr-n2-f")

    # K.1: debe existir un registro de autenticación con metadatos sanitizados
    audit_records = audit_repo.list_records(
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED
    )
    assert len(audit_records) >= 1

    for rec in audit_records:
        # El tipo es el nuevo AUTHENTICATION_EVALUATED (N.2)
        assert rec.record_type == AuditRecordType.AUTHENTICATION_EVALUATED
        # Cero secretos en el registro
        rec_str = json.dumps(rec.__dict__, default=str)
        assert "AUDIT_TOP_SECRET_TOKEN" not in rec_str
        assert "AUDIT_TOP_SECRET_REFRESH" not in rec_str
        assert "access_token" not in json.dumps(dict(rec.metadata))
        assert "refresh_token" not in json.dumps(dict(rec.metadata))

    # K.2: los pasos de traza del AuthenticationService existen y no filtran secretos
    traces = trace_repo.list_records(component_name="AuthenticationService")
    assert len(traces) >= 1
    for t in traces:
        t_str = json.dumps(t.__dict__, default=str)
        assert "AUDIT_TOP_SECRET_TOKEN" not in t_str
        assert "AUDIT_TOP_SECRET_REFRESH" not in t_str


# ---------------------------------------------------------------------------
# G. No secrets persisted en disco (JSON files)
# ---------------------------------------------------------------------------
def test_scenario_g_no_secrets_persisted(data_dir: Path, auth_service, virtual_clock):
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_DISK_SAFE",
        access_token="DISK_SECRET_TOKEN_XYZ99",
        refresh_token="DISK_SECRET_REFRESH_XYZ99",
        expires_at=virtual_clock.now() + timedelta(hours=1),
    )
    auth_service.authenticate_oauth_connection(conn, correlation_id="corr-n2-g")

    # Recorrer todos los archivos JSON generados en el data_dir
    json_files = list(data_dir.rglob("*.json"))
    assert len(json_files) > 0

    for f in json_files:
        content = f.read_text(encoding="utf-8")
        assert "DISK_SECRET_TOKEN_XYZ99" not in content, (
            f"Secret access token persistido en {f}"
        )
        assert "DISK_SECRET_REFRESH_XYZ99" not in content, (
            f"Secret refresh token persistido en {f}"
        )
        # Verificar que nunca se escriben los campos crudos de credencial
        assert '"access_token"' not in content
        assert '"refresh_token"' not in content


# ---------------------------------------------------------------------------
# H. Internal agent/service auth path (contrato explícito requerido)
# ---------------------------------------------------------------------------
def test_scenario_h_internal_agent_auth_path(
    auth_service: AuthenticationService, virtual_clock: VirtualClock
):
    # H1. Contrato interno reconocido -> AUTHENTICATED
    req_ok = AuthenticationRequest(
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal",
        token_or_secret="n2-internal-token-001",
        declared_subject="autonomous_loop",
        correlation_id="corr-n2-h1",
    )
    r_ok = auth_service.authenticate_request(req_ok)
    assert r_ok.status == AuthenticationStatus.AUTHENTICATED
    assert r_ok.principal.identity_id == "agent_autonomous_loop"
    assert r_ok.principal.identity_type == IdentityType.AGENT

    # H2. "Es interno" NO implica autenticado: sin credencial de contrato -> UNAUTHENTICATED
    req_no_contract = AuthenticationRequest(
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal",
        token_or_secret=None,
        declared_subject="autonomous_loop",
        correlation_id="corr-n2-h2",
    )
    r_no = auth_service.authenticate_request(req_no_contract)
    assert r_no.status == AuthenticationStatus.UNAUTHENTICATED
    assert r_no.is_authenticated is False
    assert "MISSING_INTERNAL_CREDENTIAL" in r_no.reason_codes

    # H3. Contrato no reconocido -> INVALID (no asumir confianza)
    req_bad = AuthenticationRequest(
        method=AuthenticationMethod.SYSTEM_ASSERTION,
        provider="internal",
        token_or_secret="fake-internal-assertion",
        declared_subject="autonomous_loop",
        correlation_id="corr-n2-h3",
    )
    r_bad = auth_service.authenticate_request(req_bad)
    assert r_bad.status == AuthenticationStatus.INVALID
    assert r_bad.is_authenticated is False

    # H4. Contrato reconocido pero subject declarado no coincide -> INVALID (binding)
    req_mismatch = AuthenticationRequest(
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="internal",
        token_or_secret="n2-internal-token-001",
        declared_subject="unauthorized_component",
        correlation_id="corr-n2-h4",
    )
    r_mismatch = auth_service.authenticate_request(req_mismatch)
    assert r_mismatch.status == AuthenticationStatus.INVALID
    assert "DECLARED_SUBJECT_MISMATCH" in r_mismatch.reason_codes


# ---------------------------------------------------------------------------
# I. E2E: Actor -> N.1 Identity -> N.2 Authentication -> PrincipalContext -> boundary
# ---------------------------------------------------------------------------
def test_scenario_e2e_authenticated_reaches_operation_boundary(
    auth_service: AuthenticationService, virtual_clock: VirtualClock
):
    # Actor externo (MercadoLibre) con token válido
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_E2E_555",
        access_token=TEST_TOKEN,
        refresh_token=TEST_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=2),
    )

    # 1. N.1 Identity (resuelta vía N.2)
    result = auth_service.authenticate_oauth_connection(conn, correlation_id="corr-e2e")
    assert result.status == AuthenticationStatus.AUTHENTICATED

    # 2. N.2 -> PrincipalContext seguro
    ctx = auth_service.create_principal_context(result)

    # 3. Boundary de operación: simula consumo downstream (MOCK de operación)
    def operation_boundary(principal_ctx: PrincipalContext) -> str:
        # El boundary solo consume identidad + estado de autenticación (sin token)
        if not principal_ctx.is_authenticated:
            return "REJECTED"
        return (
            f"EXECUTE_AS_{principal_ctx.identity_id}|"
            f"PROVIDER_{principal_ctx.provider}|"
            f"METHOD_{principal_ctx.method.value}"
        )

    boundary_output = operation_boundary(ctx)
    assert boundary_output == (
        "EXECUTE_AS_usr_mercadolibre_MLA_E2E_555|"
        "PROVIDER_mercadolibre|"
        "METHOD_OAUTH2"
    )
    # El actor autenticado llega al siguiente boundary


def test_scenario_e2e_unauthenticated_explicitly_identified(
    auth_service: AuthenticationService, virtual_clock: VirtualClock
):
    # Actor con token expirado -> queda explícitamente identificado como NO autenticado
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_E2E_777",
        access_token=TEST_TOKEN,
        refresh_token=TEST_REFRESH,
        expires_at=virtual_clock.now() - timedelta(minutes=1),
    )

    result = auth_service.authenticate_oauth_connection(conn, correlation_id="corr-e2e-unauth")
    assert result.is_authenticated is False
    assert result.status == AuthenticationStatus.EXPIRED
    assert result.principal is None

    # El contexto NO puede crearse (no hay principal autenticado)
    with pytest.raises(ValueError):
        auth_service.create_principal_context(result)

    # Boundary de operación rechaza explícitamente al actor no autenticado
    def operation_boundary(principal_ctx: PrincipalContext) -> str:
        if not principal_ctx.is_authenticated:
            return "REJECTED_UNAUTHENTICATED"
        return "EXECUTE"

    # El resultado explícito permite al sistema identificar al actor no autenticado
    assert result.status.value == "EXPIRED"
    assert "TOKEN_EXPIRED" in result.reason_codes
