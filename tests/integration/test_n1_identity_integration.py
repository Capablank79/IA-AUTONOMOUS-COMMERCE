"""
Pruebas de Integración y E2E para N.1 — Identity.

Cubre:
A. register internal agent -> retrieve stable identity
B. register MercadoLibre/external subject -> map to stable internal identity
C. restart -> identity preserved in JSON persistence
D. same external subject replay -> same identity/no duplicate (idempotency)
E. different external subject -> different identity
F. AuditRecord linked to identity
G. AgentTrace linked to identity
H. tampered persisted identity -> corruption detected
I. E2E flow: Actor resolution -> operation context -> Audit / Trace reference
"""

import pytest
import json
from datetime import datetime, timezone
from pathlib import Path

from src.domain.identity.models import (
    Identity,
    IdentityType,
    IdentityStatus,
    IdentityReference,
    create_oauth_user_identity,
    identity_to_audit_actor,
    audit_actor_to_identity_reference,
    agent_trace_to_identity_reference,
)
from src.infrastructure.persistence.data.json.identity_repository import (
    JsonIdentityRepository,
    IdentityConflictError,
    IdentityCanonicalConflictError,
    CorruptedIdentityRecordError,
)
from src.application.identity.identity_service import IdentityService
from src.domain.audit.models import AuditRecord, AuditActor, AuditActorType, AuditRecordType
from src.domain.agent_trace.models import AgentTraceRecord, StepType, TraceStatus


@pytest.fixture
def identity_repo(tmp_path: Path) -> JsonIdentityRepository:
    repo_dir = tmp_path / "identity_db"
    return JsonIdentityRepository(repo_dir)


@pytest.fixture
def identity_service(identity_repo: JsonIdentityRepository) -> IdentityService:
    return IdentityService(identity_repo)


def test_scenario_a_register_internal_agent(identity_service: IdentityService):
    """Escenario A: Registrar agente interno y recuperar identidad estable."""
    agent_ident = identity_service.register_system_agent("AutonomousLoop", display_name="Main Loop Agent")
    assert agent_ident.identity_id == "agent_AutonomousLoop"
    assert agent_ident.identity_type == IdentityType.AGENT
    assert agent_ident.canonical_identifier == "agent:internal:autonomousloop"

    retrieved = identity_service.get_identity("agent_AutonomousLoop")
    assert retrieved is not None
    assert retrieved.identity_id == "agent_AutonomousLoop"
    assert retrieved.checksum == agent_ident.checksum


def test_scenario_b_register_mercadolibre_external_subject(identity_service: IdentityService):
    """Escenario B: Registrar sujeto MercadoLibre y mapear a identidad interna estable."""
    user_ident = identity_service.register_oauth_user(
        provider="mercadolibre",
        user_id="MLA_998877",
        display_name="ML Seller Shop",
    )
    assert user_ident.identity_id == "usr_mercadolibre_MLA_998877"
    assert user_ident.external_subject_id == "MLA_998877"
    assert user_ident.provider == "mercadolibre"

    found = identity_service.repository.find_by_external_subject("mercadolibre", "MLA_998877")
    assert found is not None
    assert found.identity_id == user_ident.identity_id


def test_scenario_c_persistence_survives_restart(tmp_path: Path):
    """Escenario C: Reinicio del servicio y persistencia íntegra."""
    repo_dir = tmp_path / "restart_identity_db"
    repo1 = JsonIdentityRepository(repo_dir)
    service1 = IdentityService(repo1)

    service1.register_system_agent("PolicyEngine")
    service1.register_oauth_user("mercadolibre", "MLC_12345")

    # Simular reinicio creando nueva instancia de repositorio sobre el mismo directorio
    repo2 = JsonIdentityRepository(repo_dir)
    service2 = IdentityService(repo2)

    agent = service2.get_identity("agent_PolicyEngine")
    assert agent is not None
    assert agent.canonical_identifier == "agent:internal:policyengine"

    user = repo2.find_by_external_subject("mercadolibre", "MLC_12345")
    assert user is not None
    assert user.identity_id == "usr_mercadolibre_MLC_12345"


def test_scenario_d_same_external_subject_replay_idempotent(identity_service: IdentityService):
    """Escenario D: Replay del mismo sujeto externo -> misma identidad sin duplicación."""
    u1 = identity_service.register_oauth_user("mercadolibre", "SELLER_100")
    u2 = identity_service.register_oauth_user("mercadolibre", "SELLER_100")

    assert u1.identity_id == u2.identity_id
    assert u1.checksum == u2.checksum

    all_users = identity_service.list_identities(provider="mercadolibre")
    assert len(all_users) == 1


def test_scenario_e_different_external_subjects_distinct_identities(identity_service: IdentityService):
    """Escenario E: Diferentes sujetos externos producen identidades distintas."""
    u1 = identity_service.register_oauth_user("mercadolibre", "SELLER_100")
    u2 = identity_service.register_oauth_user("mercadolibre", "SELLER_200")

    assert u1.identity_id != u2.identity_id
    assert u1.canonical_identifier != u2.canonical_identifier


def test_scenario_f_audit_record_linked_to_identity(identity_service: IdentityService):
    """Escenario F: AuditRecord de K.1 vinculado a identidad canónica N.1."""
    # 1. Registrar identidad del actor
    agent = identity_service.register_system_agent("PricingOptimizer")

    # 2. Mapear a AuditActor y emitir AuditRecord
    audit_actor = identity_to_audit_actor(agent)
    audit_record = AuditRecord(
        audit_id="aud_101",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=datetime.now(timezone.utc),
        actor=audit_actor,
        subject_type="MARKETPLACE_LISTING",
        subject_id="item_mlc_999",
        action_or_operation="PRICING_UPDATED",
        status="SUCCESS",
        correlation_id="corr_999",
    )

    # 3. Validar resolución inversa de auditoría a identidad
    resolved_ref = identity_service.resolve_identity(audit_record.actor)
    assert resolved_ref.identity_id == agent.identity_id
    assert resolved_ref.canonical_identifier == agent.canonical_identifier
    assert not resolved_ref.is_unknown


def test_scenario_g_agent_trace_linked_to_identity(identity_service: IdentityService):
    """Escenario G: AgentTraceRecord de K.2 vinculado a identidad canónica N.1."""
    agent = identity_service.register_system_agent("MarketDiscoveryAgent")

    trace = AgentTraceRecord(
        trace_id="trc_202",
        component_name="MarketDiscoveryAgent",
        execution_id="exec_run_1",
        step_number=1,
        step_type=StepType.START,
        operation="discover_opportunities",
        started_at=datetime.now(timezone.utc),
    )

    resolved_ref = identity_service.resolve_identity(trace)
    assert resolved_ref.identity_id == agent.identity_id
    assert resolved_ref.canonical_identifier == agent.canonical_identifier


def test_scenario_h_tampered_persisted_identity_detected(tmp_path: Path):
    """Escenario H: Detección de corrupción al adulterar un archivo de identidad persistido."""
    repo_dir = tmp_path / "tamper_db"
    repo = JsonIdentityRepository(repo_dir)

    now = datetime.now(timezone.utc)
    ident = Identity(
        identity_id="usr_tamper",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:tamper",
        created_at=now,
        updated_at=now,
    )
    repo.save_identity(ident)

    # Adulterar el archivo en disco
    target_file = repo_dir / "identities" / "usr_tamper.json"
    with open(target_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["display_name"] = "HACKED_NAME"  # Modificar contenido sin actualizar checksum
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Debe lanzar error de corrupción al intentar leer
    with pytest.raises(CorruptedIdentityRecordError):
        repo.get_identity("usr_tamper")


def test_scenario_i_conflict_detection(identity_repo: JsonIdentityRepository):
    """Detección de conflictos en caso de sobreescritura de ID con datos incompatibles o colisión de canonical_identifier."""
    now = datetime.now(timezone.utc)
    id1 = Identity(
        identity_id="usr_clash",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:clash1",
        display_name="User 1",
        created_at=now,
        updated_at=now,
    )
    identity_repo.save_identity(id1)

    # Mismo ID con diferente canonical_identifier -> IdentityConflictError
    id2 = Identity(
        identity_id="usr_clash",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:clash2",
        display_name="User 2",
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(IdentityConflictError):
        identity_repo.save_identity(id2)

    # Diferente ID con mismo canonical_identifier -> IdentityCanonicalConflictError
    id3 = Identity(
        identity_id="usr_other",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:clash1",
        display_name="User 1 Duplicate",
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(IdentityCanonicalConflictError):
        identity_repo.save_identity(id3)


def test_scenario_e2e_actor_resolution_and_unknown_handling(identity_service: IdentityService):
    """
    E2E: Demostrar resolución de identidad en contexto comercial/operacional.
    Actor conocido -> identidad estable.
    Actor desconocido -> UNKNOWN preservado sin inventar identidades.
    """
    # 1. Registrar actores conocidos
    admin_user = identity_service.register_identity(
        identity_id="usr_admin",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:admin",
        display_name="Security Admin",
    )
    bot_agent = identity_service.register_system_agent("AutonomousScheduler")

    # 2. Resolución de actor conocido por ID
    res1 = identity_service.resolve_identity("usr_admin")
    assert res1.identity_id == "usr_admin"
    assert res1.identity_type == IdentityType.USER
    assert not res1.is_unknown

    # 3. Resolución de actor conocido por canonical_identifier
    res2 = identity_service.resolve_identity("agent:internal:autonomousscheduler")
    assert res2.identity_id == "agent_AutonomousScheduler"
    assert res2.identity_type == IdentityType.AGENT

    # 4. Resolución de actor desconocido / nulo
    res_none = identity_service.resolve_identity(None)
    assert res_none.is_unknown
    assert res_none.identity_type == IdentityType.UNKNOWN

    res_unregistered = identity_service.resolve_identity("unregistered_external_client")
    assert res_unregistered.is_unknown
    assert res_unregistered.identity_type == IdentityType.UNKNOWN
    assert "unregistered" in res_unregistered.canonical_identifier
