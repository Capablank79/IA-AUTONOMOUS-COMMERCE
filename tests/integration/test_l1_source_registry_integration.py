"""
Integration and E2E tests for L.1 Source Registry (Transversal L - Data Quality / Governance).

Scenarios:
A. Register Mercado Libre source -> Persist -> Retrieve -> Same identity.
B. Register Supplier source -> Retrieve by canonical identifier.
C. Restart repository/service -> Sources preserved in memory & on disk.
D. Replay same source -> No duplicates created.
E. Same source/version altered -> Explicit conflict detected.
F. Tampered persisted record -> Corruption detected via SHA-256 validation.
G. Unsafe ID/path -> Path traversal strictly rejected.
H. Multi-source adoption scenario representing real repo components (Meli, Supplier, Market Intelligence, Internal System).
I. Audit Trail integration verification.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from src.domain.source_registry.models import (
    SourceType,
    SourceStatus,
    RegisteredSource,
    build_canonical_identifier,
)
from src.infrastructure.persistence.data.json.source_registry_repository import (
    JsonSourceRegistryRepository,
    SourceVersionConflictError,
    SourceCanonicalConflictError,
    CorruptedSourceRecordError,
)
from src.application.source_registry.service import (
    SourceRegistryService,
    SourceConflictException,
)
from src.domain.audit.models import AuditRecordType
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.audit.audit_trail_service import AuditTrailService


def test_scenario_a_mercadolibre_source_lifecycle(tmp_path):
    """Escenario A: Registrar fuente Mercado Libre -> Persistir -> Recuperar -> Misma identidad."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    registered = service.register_source(
        source_id="src-mercadolibre-cl",
        name="Mercado Libre Chile Official API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:mlc",
        endpoint_reference="https://api.mercadolibre.com",
        description="Public and authenticated API for Mercado Libre Chile",
        metadata={"site_id": "MLC", "default_currency": "CLP"},
    )

    assert registered.source_id == "src-mercadolibre-cl"
    assert registered.canonical_identifier == "marketplace_api:mercadolibre:mlc"

    # Recuperar del repositorio
    fetched = service.get_source("src-mercadolibre-cl")
    assert fetched is not None
    assert fetched.source_id == registered.source_id
    assert fetched.checksum == registered.checksum
    assert fetched.canonical_identifier == registered.canonical_identifier
    assert fetched.metadata["site_id"] == "MLC"


def test_scenario_b_supplier_source_lookup_by_canonical(tmp_path):
    """Escenario B: Registrar proveedor -> Buscar por canonical identifier."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    service.register_source(
        source_id="src-supplier-central-tech",
        name="Central Tech Wholesaler Feed",
        source_type=SourceType.SUPPLIER,
        provider="central_tech",
        canonical_identifier="supplier:central_tech:catalog_v1",
        description="B2B supplier catalog",
    )

    found = service.find_by_canonical_identifier("supplier:central_tech:catalog_v1")
    assert found is not None
    assert found.source_id == "src-supplier-central-tech"
    assert found.provider == "central_tech"


def test_scenario_c_restart_resilience(tmp_path):
    """Escenario C: Reiniciar repositorio y servicio -> Fuentes preservadas íntegramente."""
    reg_dir = tmp_path / "registry"

    # 1. Sesión inicial
    repo1 = JsonSourceRegistryRepository(base_dir=reg_dir)
    service1 = SourceRegistryService(repository=repo1)

    s1 = service1.register_source(
        source_id="src-restart-1",
        name="Restart Test Source",
        source_type=SourceType.EXTERNAL_API,
        provider="external_data",
        metadata={"key": "val1"},
    )

    # 2. Reinicio (nueva instancia apuntando al mismo directorio)
    repo2 = JsonSourceRegistryRepository(base_dir=reg_dir)
    service2 = SourceRegistryService(repository=repo2)

    s2 = service2.get_source("src-restart-1")
    assert s2 is not None
    assert s2.source_id == s1.source_id
    assert s2.checksum == s1.checksum
    assert s2.metadata["key"] == "val1"
    assert repo2.exists("src-restart-1")


def test_scenario_d_replay_idempotency(tmp_path):
    """Escenario D: Replay de la misma fuente -> Idempotente, sin duplicados en índice ni disco."""
    reg_dir = tmp_path / "registry"
    repo = JsonSourceRegistryRepository(base_dir=reg_dir)
    service = SourceRegistryService(repository=repo)

    r1 = service.register_source(
        source_id="src-replay-test",
        name="Replay Test",
        source_type=SourceType.WEB_SOURCE,
        provider="scraper_service",
    )

    r2 = service.register_source(
        source_id="src-replay-test",
        name="Replay Test",
        source_type=SourceType.WEB_SOURCE,
        provider="scraper_service",
    )

    assert r1.checksum == r2.checksum

    # Verificar que solo hay un archivo en disco para la versión
    version_files = list((reg_dir / "sources" / "src-replay-test").glob("*.json"))
    assert len(version_files) == 1


def test_scenario_e_altered_content_conflict(tmp_path):
    """Escenario E: Mismo source_id y versión pero contenido alterado -> Conflicto explícito."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    service.register_source(
        source_id="src-alt-test",
        name="Original Name",
        source_type=SourceType.INTERNAL_SYSTEM,
        provider="core",
        version="1.0.0",
    )

    with pytest.raises(SourceConflictException, match="Conflict registering source"):
        service.register_source(
            source_id="src-alt-test",
            name="Altered Name",
            source_type=SourceType.INTERNAL_SYSTEM,
            provider="core",
            version="1.0.0",
        )


def test_scenario_f_tampered_record_corruption_detected(tmp_path):
    """Escenario F: Archivo en disco alterado manualmente -> Detección estricta de corrupción por checksum."""
    reg_dir = tmp_path / "registry"
    repo = JsonSourceRegistryRepository(base_dir=reg_dir)
    service = SourceRegistryService(repository=repo)

    src = service.register_source(
        source_id="src-tamper-target",
        name="Untampered Source",
        source_type=SourceType.SUPPLIER,
        provider="supplier_x",
    )

    file_path = reg_dir / "sources" / "src-tamper-target" / "1.0.0.json"
    assert file_path.exists()

    # Modificar maliciosamente el archivo sin actualizar el checksum
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["name"] = "Maliciously Modified Name"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Al intentar leer, debe detectar la corrupción
    repo_fresh = JsonSourceRegistryRepository(base_dir=reg_dir)
    with pytest.raises(CorruptedSourceRecordError, match="Checksum mismatch|Corrupted source record"):
        repo_fresh.get_source("src-tamper-target")


def test_scenario_g_unsafe_path_rejected(tmp_path):
    """Escenario G: Intento de path traversal o IDs inseguros -> Rechazado."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    with pytest.raises(ValueError, match="contains unsafe path traversal"):
        service.register_source(
            source_id="../../etc/passwd",
            name="Exploit Attempt",
            source_type=SourceType.EXTERNAL_API,
            provider="attacker",
        )


def test_scenario_h_representative_repo_sources_adoption(tmp_path):
    """
    Escenario H: E2E L.1 de adopción de fuentes representativas reales del proyecto.
    Registra fuentes reales del sistema (Mercado Libre, Proveedor local, Market Hunter, Sistema Interno).
    Demuestra que reciben IDs y canonical_identifiers únicos y estables para L.2.
    """
    reg_dir = tmp_path / "registry"
    repo = JsonSourceRegistryRepository(base_dir=reg_dir)
    service = SourceRegistryService(repository=repo)

    # 1. Mercado Libre Marketplace
    meli_src = service.register_source(
        source_id="src-marketplace-mercadolibre-cl",
        name="Mercado Libre Chile Marketplace",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:mlc",
        endpoint_reference="https://api.mercadolibre.com",
        description="Mercado Libre Chile items, searches, trends and visits",
    )

    # 2. Proveedor B2B
    supp_src = service.register_source(
        source_id="src-supplier-pc-factory-cl",
        name="PC Factory Wholesale Catalog",
        source_type=SourceType.SUPPLIER,
        provider="pc_factory",
        canonical_identifier="supplier:pc_factory:catalog_feed",
        description="Local tech distributor and inventory feed",
    )

    # 3. Market Intelligence Hunter
    hunter_src = service.register_source(
        source_id="src-hunter-trending-scraper",
        name="Market Trends Discovery Engine",
        source_type=SourceType.WEB_SOURCE,
        provider="internal_hunter",
        canonical_identifier="web_source:internal_hunter:trends_v1",
        description="Autonomous scraper and trends extractor",
    )

    # 4. Sistema Interno de Decisiones
    internal_src = service.register_source(
        source_id="src-internal-policy-engine",
        name="Policy & Economics Rule Engine",
        source_type=SourceType.INTERNAL_SYSTEM,
        provider="autonomous_core",
        canonical_identifier="internal_system:autonomous_core:policy_rules",
        description="Internal governance and rule definitions",
    )

    all_sources = service.list_sources()
    assert len(all_sources) == 4

    # Verificar que todas tienen identidades deterministas y distintas
    cids = {s.canonical_identifier for s in all_sources}
    assert len(cids) == 4
    assert meli_src.canonical_identifier in cids
    assert supp_src.canonical_identifier in cids
    assert hunter_src.canonical_identifier in cids
    assert internal_src.canonical_identifier in cids


def test_scenario_i_audit_trail_integration(tmp_path):
    """Escenario I: Integración con Audit Trail (K.1) durante el registro de fuentes."""
    audit_repo = JsonAuditRepository(storage_dir=tmp_path / "audit")
    audit_service = AuditTrailService(audit_repository=audit_repo)

    registry_repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(
        repository=registry_repo,
        audit_repository=audit_repo,
    )

    service.register_source(
        source_id="src-audit-tested-1",
        name="Audited Source",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
    )

    # Verificar que el evento SOURCE_REGISTERED quedó registrado en auditoría
    records = audit_repo.list_records(subject_type="SOURCE_REGISTRY", subject_id="src-audit-tested-1")
    assert len(records) >= 1
    assert records[0].action_or_operation == "SOURCE_REGISTERED"
    assert records[0].status == "SUCCESS"
