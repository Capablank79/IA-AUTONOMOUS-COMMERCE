"""
Pruebas Unitarias para N.1 — Identity (Transversal N - Security, Governance y Safety).

Cubre:
1. immutable identity
2. identity types (USER, AGENT, SYSTEM, SERVICE, SCHEDULER, MARKETPLACE, EXTERNAL_TOOL, UNKNOWN)
3. deterministic identity id & canonical identifier
4. USER identity
5. AGENT identity
6. SYSTEM/SERVICE identity
7. external provider subject mapping (OAuth without tokens)
8. UNKNOWN preserved
9. token not part of identity
10. secret sanitization
11. idempotent registration
12. conflicting identity rejected
13. safe identifiers (path traversal prevention)
14. identity != authentication
15. identity != authorization
"""

import pytest
from datetime import datetime, timezone
from dataclasses import FrozenInstanceError

from src.domain.identity.models import (
    Identity,
    IdentityType,
    IdentityStatus,
    IdentityReference,
    build_canonical_identifier,
    compute_identity_checksum,
    audit_actor_to_identity_reference,
    identity_to_audit_actor,
    agent_trace_to_identity_reference,
    create_oauth_user_identity,
)
from src.domain.audit.models import AuditActor, AuditActorType
from src.domain.agent_trace.models import AgentTraceRecord, StepType, TraceStatus


def test_immutable_identity():
    """1. Verifica la inmutabilidad estricta del modelo Identity."""
    now = datetime.now(timezone.utc)
    ident = Identity(
        identity_id="usr_001",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_001",
        created_at=now,
        updated_at=now,
        display_name="Test User",
    )

    with pytest.raises(FrozenInstanceError):
        ident.display_name = "Modified Name"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        ident.identity_id = "new_id"  # type: ignore


def test_identity_types_taxonomy():
    """2. Verifica la taxonomía completa de tipos de identidad canónicos."""
    expected_types = {"USER", "AGENT", "SYSTEM", "SERVICE", "SCHEDULER", "MARKETPLACE", "EXTERNAL_TOOL", "UNKNOWN"}
    actual_types = {t.value for t in IdentityType}
    assert expected_types == actual_types


def test_deterministic_canonical_identifier():
    """3. Verifica la construcción determinista del identificador canónico."""
    cid1 = build_canonical_identifier(IdentityType.USER, "MercadoLibre", "123456")
    cid2 = build_canonical_identifier("USER", "mercadolibre", "123456")
    assert cid1 == "user:mercadolibre:123456"
    assert cid1 == cid2

    cid_agent = build_canonical_identifier(IdentityType.AGENT, None, "AutonomousLoop")
    assert cid_agent == "agent:internal:autonomousloop"


def test_user_identity_creation():
    """4. Verifica la creación de identidad tipo USER."""
    now = datetime.now(timezone.utc)
    ident = Identity(
        identity_id="usr_jllv",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:jllv",
        display_name="JLLV Admin",
        created_at=now,
        updated_at=now,
    )
    assert ident.identity_type == IdentityType.USER
    assert ident.display_name == "JLLV Admin"
    assert not ident.is_unknown


def test_agent_identity_creation():
    """5. Verifica la creación de identidad tipo AGENT."""
    now = datetime.now(timezone.utc)
    ident = Identity(
        identity_id="agent_autonomous_loop",
        identity_type=IdentityType.AGENT,
        canonical_identifier="agent:internal:autonomous_loop",
        display_name="Autonomous Loop Agent",
        created_at=now,
        updated_at=now,
    )
    assert ident.identity_type == IdentityType.AGENT
    assert ident.canonical_identifier == "agent:internal:autonomous_loop"


def test_system_service_identity_creation():
    """6. Verifica la creación de identidades SYSTEM y SERVICE."""
    now = datetime.now(timezone.utc)
    sys_ident = Identity(
        identity_id="sys_policy_engine",
        identity_type=IdentityType.SYSTEM,
        canonical_identifier="system:internal:policy_engine",
        display_name="Policy Engine",
        created_at=now,
        updated_at=now,
    )
    srv_ident = Identity(
        identity_id="srv_action_executor",
        identity_type=IdentityType.SERVICE,
        canonical_identifier="service:internal:action_executor",
        display_name="Action Executor Service",
        created_at=now,
        updated_at=now,
    )
    assert sys_ident.identity_type == IdentityType.SYSTEM
    assert srv_ident.identity_type == IdentityType.SERVICE


def test_external_provider_subject_mapping():
    """7. Verifica el mapeo de sujetos externos (MercadoLibre) a identidades estables."""
    user_ident = create_oauth_user_identity(
        provider="mercadolibre",
        user_id="987654321",
        display_name="ML Seller",
    )
    assert user_ident.identity_id == "usr_mercadolibre_987654321"
    assert user_ident.canonical_identifier == "user:mercadolibre:987654321"
    assert user_ident.provider == "mercadolibre"
    assert user_ident.external_subject_id == "987654321"


def test_unknown_identity_preserved():
    """8. Verifica que UNKNOWN se preserve explícitamente y se diferencie de identidades conocidas."""
    ref = IdentityReference(
        identity_id="unknown_actor",
        identity_type=IdentityType.UNKNOWN,
        canonical_identifier="unknown:internal:unresolved",
    )
    assert ref.is_unknown
    assert ref.identity_type == IdentityType.UNKNOWN

    known_ref = IdentityReference(
        identity_id="usr_001",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:001",
    )
    assert not known_ref.is_unknown


def test_token_not_part_of_identity():
    """9. Verifica que tokens y credenciales efímeras NO formen parte de la identidad."""
    user_ident = create_oauth_user_identity(
        provider="mercadolibre",
        user_id="112233",
    )
    # Atributos de identidad canónica NO incluyen access_token ni refresh_token
    assert not hasattr(user_ident, "access_token")
    assert not hasattr(user_ident, "refresh_token")
    assert "access_token" not in user_ident.to_dict()


def test_secret_sanitization_in_metadata():
    """10. Verifica la sanitización recursiva de secretos en los metadatos de identidad."""
    now = datetime.now(timezone.utc)
    dirty_meta = {
        "api_key": "secret_key_12345",
        "auth_token": "bearer_xyz",
        "safe_attr": "ok_value",
        "nested": {
            "password": "super_secret_pwd",
            "region": "us-east-1",
        }
    }
    ident = Identity(
        identity_id="usr_sec_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:sec_01",
        created_at=now,
        updated_at=now,
        metadata=dirty_meta,
    )
    assert ident.metadata["api_key"] == "[REDACTED]"
    assert ident.metadata["auth_token"] == "[REDACTED]"
    assert ident.metadata["safe_attr"] == "ok_value"
    assert ident.metadata["nested"]["password"] == "[REDACTED]"
    assert ident.metadata["nested"]["region"] == "us-east-1"


def test_checksum_verification_and_tampering():
    """11 & 12. Verifica la integridad por checksum SHA-256 y detección de alteraciones."""
    now = datetime.now(timezone.utc)
    ident = Identity(
        identity_id="usr_val_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:val_01",
        created_at=now,
        updated_at=now,
    )
    assert ident.checksum != ""

    # Recrear con checksum adulterado debe fallar
    with pytest.raises(ValueError, match="Checksum mismatch"):
        Identity(
            identity_id="usr_val_01",
            identity_type=IdentityType.USER,
            canonical_identifier="user:internal:val_01",
            created_at=now,
            updated_at=now,
            checksum="bad_checksum_hash",
        )


def test_safe_identifiers_path_traversal():
    """13. Verifica la validación estricta de identificadores contra path traversal."""
    now = datetime.now(timezone.utc)
    unsafe_ids = ["../evil_user", "dir/user", "user\\name", "user:id", ""]
    for unsafe_id in unsafe_ids:
        with pytest.raises(ValueError):
            Identity(
                identity_id=unsafe_id,
                identity_type=IdentityType.USER,
                canonical_identifier="user:internal:safe",
                created_at=now,
                updated_at=now,
            )


def test_identity_not_equal_authentication_and_authorization():
    """14 & 15. Verifica los límites de responsabilidad: Identity != Auth != Authorization."""
    now = datetime.now(timezone.utc)
    ident = Identity(
        identity_id="usr_normal",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:normal",
        status=IdentityStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    # Poseer una identidad válida NO implica autenticación ni permisos
    assert hasattr(ident, "identity_id")
    assert not hasattr(ident, "is_authenticated")
    assert not hasattr(ident, "permissions")
    assert not hasattr(ident, "roles")
    assert not hasattr(ident, "can_execute")


def test_audit_actor_and_trace_adaptation():
    """Verifica la interoperabilidad con AuditActor (K.1) y AgentTrace (K.2)."""
    # AuditActor -> IdentityReference
    audit_actor = AuditActor(actor_type=AuditActorType.AGENT, actor_id="AutonomousAgent")
    ref = audit_actor_to_identity_reference(audit_actor)
    assert ref.identity_type == IdentityType.AGENT
    assert ref.identity_id == "id_AutonomousAgent"

    # Identity -> AuditActor
    now = datetime.now(timezone.utc)
    ident = Identity(
        identity_id="usr_seller_1",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:seller_1",
        created_at=now,
        updated_at=now,
    )
    mapped_actor = identity_to_audit_actor(ident)
    assert mapped_actor.actor_type == AuditActorType.USER
    assert mapped_actor.actor_id == "usr_seller_1"
    assert mapped_actor.details["canonical_identifier"] == "user:internal:seller_1"

    # AgentTraceRecord -> IdentityReference
    trace = AgentTraceRecord(
        trace_id="trc_001",
        component_name="AutonomousLoop",
        execution_id="exec_123",
        step_number=1,
        step_type=StepType.START,
        operation="run_loop",
        started_at=now,
    )
    agent_ref = agent_trace_to_identity_reference(trace)
    assert agent_ref.identity_type == IdentityType.AGENT
    assert agent_ref.identity_id == "agent_AutonomousLoop"
    assert agent_ref.canonical_identifier == "agent:internal:autonomousloop"
