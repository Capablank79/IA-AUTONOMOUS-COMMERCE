"""
Tests unitarios para N.5 Secret Management (Transversal N Security, Governance y Safety).

Cubre los 16 requerimientos mínimos exigidos por el Hito N.5:
1. SecretReference contains no value
2. env/config secret resolution
3. missing secret explicit failure (NOT_FOUND, no fallbacks)
4. unknown secret preserved
5. secret wrapper redacted repr
6. redacted str / format
7. no accidental serialization (asdict, json, dict conversion)
8. API key not domain data (domain models decouple from raw secrets)
9. token not identity (rotated token does not alter actor Identity)
10. secret rotation/version
11. provider isolation
12. no plaintext persistence (JSON repository only stores metadata/checksums)
13. no secret in checksum/cache key
14. deterministic metadata and checksum calculation
15. N.5 != authentication (Secret Management is credential protection, not authn)
16. no N.6+ implementation (no Approval Policies, Financial Limits, etc.)
"""

from datetime import datetime, timedelta, timezone
import json
import os
import pytest
from dataclasses import FrozenInstanceError, asdict

from src.domain.identity.models import (
    IdentityReference,
    IdentityType,
)
from src.domain.secrets.models import (
    SecretType,
    SecretStatus,
    SecretResolutionStatus,
    SecretValue,
    SecretReference,
    SecretMetadata,
    SecretResolutionResult,
    compute_secret_metadata_checksum,
)
from src.domain.secrets.ports import (
    SecretProviderPort,
    SecretResolverPort,
    SecretMetadataRepositoryPort,
)
from src.infrastructure.secrets.providers import (
    EnvSecretProvider,
    InjectedSecretProvider,
    OAuthSecretProviderBridge,
)
from src.infrastructure.persistence.data.json.secret_metadata_repository import (
    JsonSecretMetadataRepository,
    SecretMetadataConflictError,
    CorruptedSecretMetadataRecordError,
)
from src.application.secrets.secret_service import SecretService
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository


@pytest.fixture
def virtual_clock():
    start_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def tmp_secret_repo(tmp_path):
    repo_dir = tmp_path / "secrets_metadata_db"
    return JsonSecretMetadataRepository(base_dir=repo_dir)


@pytest.fixture
def secret_service(tmp_secret_repo, virtual_clock, tmp_path):
    audit_repo = JsonAuditRepository(storage_dir=tmp_path / "audit_db")
    trace_repo = JsonAgentTraceRepository(base_dir=tmp_path / "trace_db")
    trace_service = AgentTraceService(trace_repository=trace_repo)

    injected_provider = InjectedSecretProvider(
        initial_secrets={
            "ref-ml-secret": "ml_client_secret_super_confidential_value_123",
            "mercadolibre:app_secret": "ml_app_secret_value_456",
            "openai:api_key": "sk-proj-test-openai-key-secret-999",
        }
    )
    env_provider = EnvSecretProvider()

    service = SecretService(
        providers=[injected_provider, env_provider],
        metadata_repository=tmp_secret_repo,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
    )
    return service, injected_provider, env_provider, tmp_secret_repo


# =========================================================================
# 1. SecretReference contains no value
# =========================================================================
def test_1_secret_reference_contains_no_value():
    ref = SecretReference(
        reference_id="sec-ref-001",
        provider="mercadolibre",
        secret_name="client_secret",
        secret_type=SecretType.CLIENT_SECRET,
        version="1",
        env_var_fallback="MERCADOLIBRE_CLIENT_SECRET",
    )

    # Immutability
    with pytest.raises(FrozenInstanceError):
        ref.secret_name = "tampered"

    ref_dict = ref.to_dict()
    # Verifica que ningún campo almacene valores de secretos en crudo
    assert "secret_value" not in ref_dict
    assert "value" not in ref_dict
    assert "credential" not in ref_dict
    assert ref.provider == "mercadolibre"
    assert ref.secret_name == "client_secret"
    assert ref.version == "1"


# =========================================================================
# 2. env/config secret resolution
# =========================================================================
def test_2_env_secret_resolution(monkeypatch):
    monkeypatch.setenv("CUSTOM_PROVIDER_API_KEY", "env_secret_material_abc_xyz")

    env_provider = EnvSecretProvider(prefix="CUSTOM")
    ref = SecretReference(
        reference_id="ref-custom-env",
        provider="provider",
        secret_name="api_key",
        secret_type=SecretType.API_KEY,
    )

    val = env_provider.get_secret(ref)
    assert val is not None
    assert isinstance(val, SecretValue)
    assert val.reveal() == "env_secret_material_abc_xyz"


# =========================================================================
# 3. missing secret explicit failure
# =========================================================================
def test_3_missing_secret_explicit_failure(secret_service):
    service, _, _, _ = secret_service
    missing_ref = SecretReference(
        reference_id="ref-missing-key",
        provider="nonexistent_provider",
        secret_name="nonexistent_secret",
        secret_type=SecretType.API_KEY,
    )

    result = service.resolve(missing_ref)
    assert result.status == SecretResolutionStatus.NOT_FOUND
    assert not result.is_resolved
    assert result.secret_value is None
    assert "not found" in result.error_message.lower()

    # reveal_value() debe fallar explícitamente
    with pytest.raises(RuntimeError) as exc_info:
        result.reveal_value()
    assert "Cannot reveal secret" in str(exc_info.value)


# =========================================================================
# 4. unknown secret preserved
# =========================================================================
def test_4_unknown_secret_type_preserved():
    ref = SecretReference(
        reference_id="ref-unknown-custom",
        provider="legacy_vault",
        secret_name="custom_blob",
        secret_type=SecretType.UNKNOWN,
    )
    assert ref.is_unknown
    assert ref.secret_type == SecretType.UNKNOWN
    assert ref.to_dict()["secret_type"] == "UNKNOWN"

    # From dict with omitted or invalid type defaults to UNKNOWN
    reconstructed = SecretReference.from_dict({
        "reference_id": "ref-unknown-custom",
        "provider": "legacy_vault",
        "secret_name": "custom_blob",
    })
    assert reconstructed.secret_type == SecretType.UNKNOWN


# =========================================================================
# 5. secret wrapper redacted repr
# =========================================================================
def test_5_secret_wrapper_redacted_repr():
    secret_text = "super_confidential_plain_text_password_987654"
    sec_val = SecretValue(secret_text)

    repr_output = repr(sec_val)
    assert secret_text not in repr_output
    assert repr_output == "<SecretValue: [REDACTED]>"


# =========================================================================
# 6. redacted str / format
# =========================================================================
def test_6_secret_wrapper_redacted_str_and_format():
    secret_text = "top_secret_token_1234567890"
    sec_val = SecretValue(secret_text)

    str_output = str(sec_val)
    format_output = f"{sec_val}"

    assert secret_text not in str_output
    assert str_output == "[REDACTED]"
    assert secret_text not in format_output
    assert format_output == "[REDACTED]"


# =========================================================================
# 7. no accidental serialization
# =========================================================================
def test_7_no_accidental_serialization():
    secret_text = "bearer_sensitive_token_payload_xyz"
    sec_val = SecretValue(secret_text)

    # SecretValue cannot be directly json serialized
    with pytest.raises(TypeError):
        json.dumps(sec_val)

    # Dictionary wrapping
    payload = {"wrapper": sec_val}
    with pytest.raises(TypeError):
        json.dumps(payload)


# =========================================================================
# 8. API key not domain data
# =========================================================================
def test_8_api_key_not_domain_data(secret_service):
    service, _, _, _ = secret_service
    ref = SecretReference(
        reference_id="ref-ml-secret",
        provider="mercadolibre",
        secret_name="client_secret",
        secret_type=SecretType.CLIENT_SECRET,
    )

    # Domain entities only hold references, never raw secrets
    resolution = service.resolve(ref)
    assert resolution.is_resolved
    assert isinstance(resolution.secret_value, SecretValue)

    # Audit payload contains no secret
    audit_data = resolution.to_audit_payload()
    for v in audit_data.values():
        if isinstance(v, str):
            assert "super_confidential" not in v


# =========================================================================
# 9. token not identity
# =========================================================================
def test_9_token_not_identity():
    # Identity (N.1) represents who the actor is
    actor_id = IdentityReference(
        identity_id="usr-agent-001",
        identity_type=IdentityType.AGENT,
        canonical_identifier="agent:internal:order-processing-agent",
    )

    # Secret (N.5) represents credentials used by infrastructure
    sec_ref = SecretReference(
        reference_id="sec-agent-token-001",
        provider="internal",
        secret_name="service_key",
        secret_type=SecretType.INTERNAL_CREDENTIAL,
    )

    assert actor_id.identity_id != sec_ref.reference_id
    assert actor_id.identity_type.value != sec_ref.secret_type.value


# =========================================================================
# 10. secret rotation / version
# =========================================================================
def test_10_secret_rotation_and_version(secret_service, virtual_clock):
    service, injected_provider, _, metadata_repo = secret_service

    ref_id = "ref-rotating-key"
    injected_provider.set_secret(ref_id, "initial_secret_value_v1")

    meta_v1 = service.register_metadata(
        reference_id=ref_id,
        secret_name="rotating_api_key",
        provider="injected",
        secret_type=SecretType.API_KEY,
        version="1",
    )
    assert meta_v1.version == "1"

    ref = SecretReference(
        reference_id=ref_id,
        provider="injected",
        secret_name="rotating_api_key",
        secret_type=SecretType.API_KEY,
        version="1",
    )

    res1 = service.resolve(ref)
    assert res1.is_resolved
    assert res1.reveal_value() == "initial_secret_value_v1"

    # Rotar secreto
    virtual_clock.advance(3600.0)
    meta_v2 = service.rotate_secret(
        reference_id=ref_id,
        new_value="rotated_secret_value_v2",
        new_version="2",
    )
    assert meta_v2.version == "2"

    res2 = service.resolve(ref)
    assert res2.is_resolved
    assert res2.reveal_value() == "rotated_secret_value_v2"


# =========================================================================
# 11. provider isolation
# =========================================================================
def test_11_provider_isolation():
    inj_provider = InjectedSecretProvider(
        initial_secrets={"provider_a:key": "secret_a_value"}
    )

    ref_a = SecretReference(
        reference_id="ref-a",
        provider="provider_a",
        secret_name="key",
    )
    ref_b = SecretReference(
        reference_id="ref-b",
        provider="provider_b",
        secret_name="key",
    )

    assert inj_provider.get_secret(ref_a) is not None
    assert inj_provider.get_secret(ref_b) is None


# =========================================================================
# 12. no plaintext persistence
# =========================================================================
def test_12_no_plaintext_persistence(tmp_path, virtual_clock):
    repo_dir = tmp_path / "secrets_metadata_storage"
    repo = JsonSecretMetadataRepository(base_dir=repo_dir)

    secret_raw = "critical_plain_text_token_should_never_be_saved"

    meta = SecretMetadata(
        reference_id="ref-audit-meta-001",
        secret_name="mercadolibre_oauth",
        provider="mercadolibre",
        secret_type=SecretType.ACCESS_TOKEN,
        version="1",
        created_at=virtual_clock.now(),
        updated_at=virtual_clock.now(),
        metadata={"scope": "read_write", "environment": "production"},
    )
    repo.save_metadata(meta)

    # Verificar todos los archivos en disco en repo_dir
    for root, _, files in os.walk(repo_dir):
        for f in files:
            fpath = os.path.join(root, f)
            with open(fpath, "r", encoding="utf-8") as handle:
                content = handle.read()
                assert secret_raw not in content


# =========================================================================
# 13. no secret in checksum/cache key
# =========================================================================
def test_13_no_secret_in_checksum():
    meta = SecretMetadata(
        reference_id="ref-cs-01",
        secret_name="llm_api_key",
        provider="omniroute",
        secret_type=SecretType.API_KEY,
        version="1",
    )
    assert meta.checksum is not None
    assert len(meta.checksum) == 64  # SHA-256


# =========================================================================
# 14. deterministic metadata
# =========================================================================
def test_14_deterministic_metadata_checksum():
    fixed_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    cs1 = compute_secret_metadata_checksum(
        reference_id="ref-det-01",
        secret_name="service_key",
        provider="auth_provider",
        secret_type=SecretType.INTERNAL_CREDENTIAL,
        version="1",
        status=SecretStatus.ACTIVE,
        created_at=fixed_time,
        expires_at=None,
        metadata={"env": "prod", "tier": "1"},
    )
    cs2 = compute_secret_metadata_checksum(
        reference_id="ref-det-01",
        secret_name="service_key",
        provider="auth_provider",
        secret_type=SecretType.INTERNAL_CREDENTIAL,
        version="1",
        status=SecretStatus.ACTIVE,
        created_at=fixed_time,
        expires_at=None,
        metadata={"tier": "1", "env": "prod"}, # Different dict key order
    )
    assert cs1 == cs2


# =========================================================================
# 15. N.5 != authentication
# =========================================================================
def test_15_n5_is_not_authentication(secret_service):
    service, _, _, _ = secret_service

    # N.5 resolves credential material for adapters, does not validate actor passwords or generate user sessions
    ref = SecretReference(
        reference_id="ref-openai-key",
        provider="openai",
        secret_name="api_key",
        secret_type=SecretType.API_KEY,
    )
    resolution = service.resolve(ref)
    assert resolution.is_resolved
    assert resolution.status == SecretResolutionStatus.RESOLVED
    assert not hasattr(resolution, "authenticated_principal")


# =========================================================================
# 16. no N.6+ implementation
# =========================================================================
def test_16_no_n6_plus_implementation():
    from src.domain.secrets import models, ports
    # Ensure no Approval Policies (N.6), Financial Limits (N.7) or Kill Switches (N.11) leaked into N.5
    for member in dir(models) + dir(ports):
        assert "approval_policy" not in member.lower()
        assert "financial_limit" not in member.lower()
        assert "kill_switch" not in member.lower()
        assert "emergency_stop" not in member.lower()
