"""
Tests unitarios para N.3 Authorization (Transversal N Security, Governance y Safety).

Cubre los 15 requerimientos mínimos exigidos por el Hito N.3:
1. Authenticated + allowed policy → ALLOW
2. Authenticated + deny rule → DENY
3. Unauthenticated → no ALLOW
4. Expired auth → no ALLOW
5. Unknown auth → no ALLOW
6. Unknown policy / no rule → no default allow (DEFAULT DENY)
7. Action-specific authorization
8. Resource-specific authorization
9. Different action → different decision
10. Deterministic decision (mismos inputs → misma decisión y checksum)
11. Policy versioning
12. Safe reason codes (sin secretos)
13. No secrets in models or metadata
14. Authorization != Authentication
15. No RBAC implementation (no roles ni asignaciones N.4)
"""

from datetime import datetime, timedelta, timezone
import pytest

from src.domain.identity.models import (
    IdentityReference,
    IdentityType,
)
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.authorization.models import (
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
    ActionReference,
    AuthorizationRequest,
    AuthorizationDecision,
    compute_authorization_checksum,
)
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import (
    AuthorizationPolicyRule,
    PriceFloorPolicyRule,
)
from src.application.authorization.authorization_service import AuthorizationService
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock


@pytest.fixture
def virtual_clock():
    start_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def valid_principal():
    return IdentityReference(
        identity_id="usr_mercadolibre_123456",
        identity_type=IdentityType.USER,
        canonical_identifier="user:mercadolibre:123456",
        display_name="User 123456",
    )


@pytest.fixture
def authenticated_context(valid_principal, virtual_clock):
    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="mercadolibre",
        principal=valid_principal,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
        correlation_id="corr-auth-unit-1",
    )
    return PrincipalContext(principal=valid_principal, auth_result=auth_result)


@pytest.fixture
def policy_engine():
    return PolicyEngine(rules=[AuthorizationPolicyRule()])


@pytest.fixture
def authz_service(policy_engine, virtual_clock):
    return AuthorizationService(
        policy_engine=policy_engine,
        clock=virtual_clock,
    )


# ---------------------------------------------------------------------------
# 1. Authenticated + Allowed Policy → ALLOW
# ---------------------------------------------------------------------------
def test_authenticated_and_allowed_policy_returns_allow(authz_service, authenticated_context):
    req = AuthorizationRequest(
        action="PUBLISH_LISTING",
        principal_context=authenticated_context,
        resource="listing:MLA_9999",
        commercial_context={"allowed_actions": ["PUBLISH_LISTING", "UPDATE_PRICE"]},
        correlation_id="corr-unit-1",
    )

    decision = authz_service.authorize(req)

    assert decision.is_allowed is True
    assert decision.status == AuthorizationStatus.ALLOW
    assert decision.identity_id == "usr_mercadolibre_123456"
    assert decision.action == "PUBLISH_LISTING"
    assert AuthorizationReasonCode.AUTHORIZED_BY_POLICY.value in decision.reason_codes
    assert decision.checksum is not None


# ---------------------------------------------------------------------------
# 2. Authenticated + Deny Rule → DENY
# ---------------------------------------------------------------------------
def test_authenticated_and_prohibited_action_returns_deny(authz_service, authenticated_context):
    req = AuthorizationRequest(
        action="DELETE_ACCOUNT",
        principal_context=authenticated_context,
        commercial_context={"prohibited_actions": ["DELETE_ACCOUNT", "PURGE_DATA"]},
        correlation_id="corr-unit-2",
    )

    decision = authz_service.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == AuthorizationStatus.DENY
    assert AuthorizationReasonCode.ACTION_PROHIBITED.value in decision.reason_codes
    assert "DELETE_ACCOUNT" in decision.reasons[0]


# ---------------------------------------------------------------------------
# 3. Unauthenticated → No ALLOW
# ---------------------------------------------------------------------------
def test_unauthenticated_request_is_denied(authz_service):
    # Request sin principal_context
    req = AuthorizationRequest(
        action="PUBLISH_LISTING",
        principal_context=None,
        correlation_id="corr-unit-3",
    )

    decision = authz_service.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == AuthorizationStatus.DENY
    assert AuthorizationReasonCode.MISSING_PRINCIPAL_CONTEXT.value in decision.reason_codes


# ---------------------------------------------------------------------------
# 4. Expired Auth → No ALLOW
# ---------------------------------------------------------------------------
def test_expired_authentication_returns_deny(authz_service, valid_principal, virtual_clock):
    expired_result = AuthenticationResult(
        status=AuthenticationStatus.EXPIRED,
        method=AuthenticationMethod.OAUTH2,
        provider="mercadolibre",
        principal=None,
        reason_codes=("TOKEN_EXPIRED",),
    )
    expired_ctx = PrincipalContext(principal=valid_principal, auth_result=expired_result)

    req = AuthorizationRequest(
        action="UPDATE_INVENTORY",
        principal_context=expired_ctx,
        correlation_id="corr-unit-4",
    )

    decision = authz_service.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == AuthorizationStatus.DENY
    assert AuthorizationReasonCode.EXPIRED_AUTHENTICATION.value in decision.reason_codes


# ---------------------------------------------------------------------------
# 5. Unknown Auth → No ALLOW (Preserves UNKNOWN)
# ---------------------------------------------------------------------------
def test_unknown_authentication_returns_unknown_status(authz_service, valid_principal):
    unknown_result = AuthenticationResult(
        status=AuthenticationStatus.UNKNOWN,
        method=AuthenticationMethod.UNKNOWN,
        provider="unknown_provider",
        principal=None,
        reason_codes=("UNKNOWN_AUTHENTICATION_METHOD",),
    )
    unknown_ctx = PrincipalContext(principal=valid_principal, auth_result=unknown_result)

    req = AuthorizationRequest(
        action="UPDATE_PRICE",
        principal_context=unknown_ctx,
        correlation_id="corr-unit-5",
    )

    decision = authz_service.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == AuthorizationStatus.UNKNOWN
    assert AuthorizationReasonCode.UNKNOWN_AUTHENTICATION.value in decision.reason_codes


# ---------------------------------------------------------------------------
# 6. Unknown Policy / No Rule → Default DENY (No default allow)
# ---------------------------------------------------------------------------
def test_action_not_in_allowed_list_is_denied(authz_service, authenticated_context):
    # Lista de allowed_actions explícita que NO incluye la acción pedida
    req = AuthorizationRequest(
        action="EXECUTE_FINANCIAL_PAYMENT",
        principal_context=authenticated_context,
        commercial_context={"allowed_actions": ["READ_CATALOG", "ANALYZE_MARKET"]},
        correlation_id="corr-unit-6",
    )

    decision = authz_service.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == AuthorizationStatus.DENY
    assert AuthorizationReasonCode.ACTION_NOT_ALLOWED.value in decision.reason_codes


# ---------------------------------------------------------------------------
# 7. Action-Specific Authorization
# ---------------------------------------------------------------------------
def test_action_specific_authorization_granularity(authz_service, authenticated_context):
    # READ está permitido, MUTATE está denegado
    req_read = AuthorizationRequest(
        action="READ_INVENTORY",
        principal_context=authenticated_context,
        commercial_context={
            "allowed_actions": ["READ_INVENTORY"],
            "prohibited_actions": ["UPDATE_INVENTORY"],
        },
    )
    req_mutate = AuthorizationRequest(
        action="UPDATE_INVENTORY",
        principal_context=authenticated_context,
        commercial_context={
            "allowed_actions": ["READ_INVENTORY"],
            "prohibited_actions": ["UPDATE_INVENTORY"],
        },
    )

    dec_read = authz_service.authorize(req_read)
    dec_mutate = authz_service.authorize(req_mutate)

    assert dec_read.is_allowed is True
    assert dec_read.status == AuthorizationStatus.ALLOW

    assert dec_mutate.is_allowed is False
    assert dec_mutate.status == AuthorizationStatus.DENY


# ---------------------------------------------------------------------------
# 8. Resource-Specific Authorization
# ---------------------------------------------------------------------------
def test_resource_specific_authorization_and_account_mismatch(authz_service, authenticated_context):
    # Recurso con account_id coincidente -> permitido
    res_matching = ResourceReference(
        resource_type="listing",
        resource_id="MLA_123",
        provider="mercadolibre",
        account_id="123456",  # Coincide con valid_principal.external_subject_id
    )
    req_ok = AuthorizationRequest(
        action="PUBLISH_LISTING",
        principal_context=authenticated_context,
        resource=res_matching,
        commercial_context={"allowed_actions": ["PUBLISH_LISTING"]},
    )
    dec_ok = authz_service.authorize(req_ok)
    assert dec_ok.is_allowed is True

    # Recurso con account_id de OTRO usuario -> DENY por RESOURCE_MISMATCH
    res_other = ResourceReference(
        resource_type="listing",
        resource_id="MLA_999",
        provider="mercadolibre",
        account_id="999999",  # NO coincide con 123456
    )
    req_mismatch = AuthorizationRequest(
        action="PUBLISH_LISTING",
        principal_context=authenticated_context,
        resource=res_other,
        commercial_context={"allowed_actions": ["PUBLISH_LISTING"]},
    )
    dec_mismatch = authz_service.authorize(req_mismatch)
    assert dec_mismatch.is_allowed is False
    assert dec_mismatch.status == AuthorizationStatus.DENY
    assert AuthorizationReasonCode.RESOURCE_MISMATCH.value in dec_mismatch.reason_codes


# ---------------------------------------------------------------------------
# 9. Different Action → Different Decision
# ---------------------------------------------------------------------------
def test_different_action_yields_different_decision(authz_service, authenticated_context):
    ctx_allowed = {"allowed_actions": ["SAFE_QUERY"]}

    req_query = AuthorizationRequest(
        action="SAFE_QUERY",
        principal_context=authenticated_context,
        commercial_context=ctx_allowed,
    )
    req_publish = AuthorizationRequest(
        action="PUBLISH_LISTING",
        principal_context=authenticated_context,
        commercial_context=ctx_allowed,
    )

    dec_query = authz_service.authorize(req_query)
    dec_publish = authz_service.authorize(req_publish)

    assert dec_query.status == AuthorizationStatus.ALLOW
    assert dec_publish.status == AuthorizationStatus.DENY


# ---------------------------------------------------------------------------
# 10. Deterministic Decision (Same inputs → same decision & checksum)
# ---------------------------------------------------------------------------
def test_deterministic_decision_and_checksum(authz_service, authenticated_context):
    req = AuthorizationRequest(
        action="UPDATE_PRICE",
        principal_context=authenticated_context,
        resource="listing:MLA123",
        commercial_context={"allowed_actions": ["UPDATE_PRICE"]},
        correlation_id="corr-det-10",
        policy_version="1.0.0",
    )

    dec1 = authz_service.authorize(req)
    dec2 = authz_service.authorize(req)

    assert dec1.status == dec2.status == AuthorizationStatus.ALLOW
    assert dec1.identity_id == dec2.identity_id == "usr_mercadolibre_123456"
    assert dec1.checksum == dec2.checksum
    assert dec1.reason_codes == dec2.reason_codes


# ---------------------------------------------------------------------------
# 11. Policy Versioning
# ---------------------------------------------------------------------------
def test_policy_versioning_propagates_to_decision(authz_service, authenticated_context):
    req_v1 = AuthorizationRequest(
        action="READ",
        principal_context=authenticated_context,
        policy_version="1.0.0",
        commercial_context={"allowed_actions": ["READ"]},
    )
    req_v2 = AuthorizationRequest(
        action="READ",
        principal_context=authenticated_context,
        policy_version="2.1.0",
        commercial_context={"allowed_actions": ["READ"]},
    )

    dec_v1 = authz_service.authorize(req_v1)
    dec_v2 = authz_service.authorize(req_v2)

    assert dec_v1.policy_version == "1.0.0"
    assert dec_v2.policy_version == "2.1.0"
    assert dec_v1.checksum != dec_v2.checksum  # Versioning impacts cryptographic integrity


# ---------------------------------------------------------------------------
# 12. Safe Reason Codes
# ---------------------------------------------------------------------------
def test_safe_reason_codes_are_canonical_enums(authz_service, authenticated_context):
    req = AuthorizationRequest(
        action="UNAUTHORIZED_OPERATION",
        principal_context=authenticated_context,
        commercial_context={"allowed_actions": ["ALLOWED_OPERATION"]},
    )

    decision = authz_service.authorize(req)

    for code in decision.reason_codes:
        assert isinstance(code, str)
        # Reason codes must be non-empty and devoid of secrets
        assert len(code) > 0
        assert "password" not in code.lower()
        assert "token" not in code.lower()


# ---------------------------------------------------------------------------
# 13. No Secrets in Models, Metadata or String Representations
# ---------------------------------------------------------------------------
def test_secret_sanitization_in_authorization_request_and_decision(authz_service, authenticated_context):
    dirty_context = {
        "access_token": "SUPER_SECRET_TOKEN",
        "api_key": "SUPER_SECRET_KEY",
        "password": "SUPER_SECRET_PASSWORD",
        "nested": {
            "token": "INNER_TOKEN",
            "safe_key": "safe_val",
        },
        "allowed_actions": ["PUBLISH_LISTING"],
    }

    req = AuthorizationRequest(
        action="PUBLISH_LISTING",
        principal_context=authenticated_context,
        commercial_context=dirty_context,
    )

    # Metadata sanitizada en Request
    assert req.commercial_context["access_token"] == "[REDACTED]"
    assert req.commercial_context["api_key"] == "[REDACTED]"
    assert req.commercial_context["password"] == "[REDACTED]"
    assert req.commercial_context["nested"]["token"] == "[REDACTED]"
    assert req.commercial_context["nested"]["safe_key"] == "safe_val"

    decision = authz_service.authorize(req)

    # Cero secretos en Decision string/repr
    dec_repr = repr(decision)
    dec_str = str(decision)
    assert "SUPER_SECRET_TOKEN" not in dec_repr
    assert "SUPER_SECRET_KEY" not in dec_str
    assert "SUPER_SECRET_PASSWORD" not in dec_repr


# ---------------------------------------------------------------------------
# 14. Authorization != Authentication
# ---------------------------------------------------------------------------
def test_authenticated_identity_does_not_imply_automatic_authorization(authz_service, authenticated_context):
    # Principal 100% autenticado en N.2
    assert authenticated_context.is_authenticated is True

    # Petición para acción que no está en la política
    req = AuthorizationRequest(
        action="HIGH_RISK_FINANCIAL_OPERATION",
        principal_context=authenticated_context,
        commercial_context={"allowed_actions": ["READ_ONLY"]},
    )

    decision = authz_service.authorize(req)

    # La autenticación exitosa NO otorga autorización automática
    assert decision.is_allowed is False
    assert decision.status == AuthorizationStatus.DENY


# ---------------------------------------------------------------------------
# 15. No RBAC Implementation (Boundary N.3 vs N.4)
# ---------------------------------------------------------------------------
def test_n3_does_not_implement_rbac_roles_or_catalogs(authz_service, authenticated_context):
    req = AuthorizationRequest(
        action="READ",
        principal_context=authenticated_context,
    )
    decision = authz_service.authorize(req)

    # N.3 models & service do not expose RBAC roles or assignment catalogs
    assert not hasattr(req, "roles")
    assert not hasattr(req, "role_hierarchy")
    assert not hasattr(decision, "assigned_roles")
    assert not hasattr(decision, "inherited_roles")
    assert not hasattr(authz_service, "assign_role")
    assert not hasattr(authz_service, "get_roles_for_principal")
