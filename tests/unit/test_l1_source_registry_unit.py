"""
Unit tests for L.1 Source Registry (Transversal L - Data Quality / Governance).

Covers:
1. Immutable source (frozen dataclass and MappingProxyType metadata)
2. Valid source registration
3. Canonical identity deterministic
4. Source types taxonomy
5. UNKNOWN source type support
6. Invalid ID rejected
7. Path traversal rejected in source_id and version
8. Secret and credential sanitization in metadata and endpoint
9. Checksum deterministic
10. Checksum changes on semantic payload change
11. Same source replay idempotent
12. Conflicting content rejected (different checksum on same version)
13. Status semantics (ACTIVE, INACTIVE, DEPRECATED, UNKNOWN)
14. List and get operations with filtering
15. No TTL logic present in L.1
16. No confidence calculation logic present in L.1
17. No provenance ownership/tracing present in L.1
18. Safe metadata conversion and deep freezing
"""

from datetime import datetime, timezone
import pytest
from types import MappingProxyType

from src.domain.source_registry.models import (
    SourceType,
    SourceStatus,
    RegisteredSource,
    build_canonical_identifier,
    compute_source_checksum,
    sanitize_endpoint_reference,
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


def test_immutable_source():
    """1. Verifica que RegisteredSource es inmutable (frozen) y que su metadata no puede mutar."""
    now = datetime.now(timezone.utc)
    source = RegisteredSource(
        source_id="src-meli-chile",
        name="Mercado Libre Chile API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:mlc",
        created_at=now,
        updated_at=now,
        metadata={"site": "MLC", "category_level": 3},
    )

    with pytest.raises(Exception):
        source.name = "New Name"

    assert isinstance(source.metadata, MappingProxyType)
    with pytest.raises(TypeError):
        source.metadata["new_key"] = "forbidden"


def test_valid_source_registration(tmp_path):
    """2. Verifica el registro exitoso de una fuente válida."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    source = service.register_source(
        source_id="src-supplier-pc-factory",
        name="PC Factory Supplier Feed",
        source_type=SourceType.SUPPLIER,
        provider="pc_factory",
        description="Supplier inventory feed for tech products",
    )

    assert source.source_id == "src-supplier-pc-factory"
    assert source.source_type == SourceType.SUPPLIER
    assert source.provider == "pc_factory"
    assert source.status == SourceStatus.ACTIVE
    assert source.canonical_identifier == "supplier:pc_factory:src-supplier-pc-factory"
    assert source.checksum != ""
    assert repo.exists("src-supplier-pc-factory")


def test_canonical_identity_deterministic():
    """3. Verifica que la identidad canónica se construye de forma determinista y normalizada."""
    cid1 = build_canonical_identifier(SourceType.MARKETPLACE_API, " MercadoLibre ", " MLC-V1 ")
    cid2 = build_canonical_identifier("MARKETPLACE_API", "mercadolibre", "mlc-v1")
    assert cid1 == "marketplace_api:mercadolibre:mlc-v1"
    assert cid1 == cid2


def test_source_types_taxonomy():
    """4. Verifica la taxonomía de tipos de fuentes admitidas."""
    expected = {
        "MARKETPLACE_API",
        "SUPPLIER",
        "WEB_SOURCE",
        "INTERNAL_SYSTEM",
        "USER_INPUT",
        "DERIVED_DATASET",
        "EXTERNAL_API",
        "UNKNOWN",
    }
    actual = {st.value for st in SourceType}
    assert expected == actual


def test_unknown_source_type_support(tmp_path):
    """5. Verifica que el tipo UNKNOWN es soportado sin fallar."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    source = service.register_source(
        source_id="src-unclassified-feed",
        name="Unclassified Feed",
        source_type=SourceType.UNKNOWN,
        provider="legacy_partner",
        status=SourceStatus.UNKNOWN,
    )

    assert source.source_type == SourceType.UNKNOWN
    assert source.status == SourceStatus.UNKNOWN


def test_invalid_id_rejected():
    """6. Verifica que IDs vacíos o inválidos son rechazados."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="source_id must be a non-empty string"):
        RegisteredSource(
            source_id="",
            name="Invalid",
            source_type=SourceType.EXTERNAL_API,
            provider="ext",
            canonical_identifier="external_api:ext:invalid",
            created_at=now,
            updated_at=now,
        )


@pytest.mark.parametrize("unsafe_id", [
    "../evil_source",
    "..\\evil_source",
    "sources/sub",
    "sources\\sub",
    "/root/src",
    "C:\\Windows\\src",
    "test:id",
])
def test_path_traversal_rejected(unsafe_id):
    """7. Verifica el rechazo estricto de secuencias de path traversal."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="contains unsafe path traversal"):
        RegisteredSource(
            source_id=unsafe_id,
            name="Traversal Attempt",
            source_type=SourceType.EXTERNAL_API,
            provider="evil",
            canonical_identifier="external_api:evil:test",
            created_at=now,
            updated_at=now,
        )


def test_secret_sanitization_in_metadata_and_endpoint(tmp_path):
    """8. Verifica la sanitización recursiva de secretos en metadata y URLs."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    source = service.register_source(
        source_id="src-secure-api",
        name="Secure API Feed",
        source_type=SourceType.MARKETPLACE_API,
        provider="meli",
        endpoint_reference="https://user:secretpassword@api.mercadolibre.com/items?api_key=SECRET123&site=MLC",
        metadata={
            "normal_config": "enabled",
            "api_key": "SECRET-TOKEN-999",
            "nested": {
                "access_token": "BEARER-XYZ",
                "safe_field": 1234,
            },
        },
    )

    # Endpoint query con secreto y credenciales eliminados
    assert "secretpassword" not in source.endpoint_reference
    assert "SECRET123" not in source.endpoint_reference

    # Metadata recursivamente sanitizada
    assert source.metadata["normal_config"] == "enabled"
    assert source.metadata["api_key"] == "[REDACTED]"
    assert source.metadata["nested"]["access_token"] == "[REDACTED]"
    assert source.metadata["nested"]["safe_field"] == 1234


def test_checksum_deterministic():
    """9. Verifica que el cálculo de checksum es determinista."""
    now = datetime.now(timezone.utc)
    s1 = RegisteredSource(
        source_id="src-det-1",
        name="Deterministic Source",
        source_type=SourceType.MARKETPLACE_API,
        provider="meli",
        canonical_identifier="marketplace_api:meli:src-det-1",
        created_at=now,
        updated_at=now,
        metadata={"a": 1, "b": 2},
    )
    s2 = RegisteredSource(
        source_id="src-det-1",
        name="Deterministic Source",
        source_type=SourceType.MARKETPLACE_API,
        provider="meli",
        canonical_identifier="marketplace_api:meli:src-det-1",
        created_at=now,
        updated_at=now,
        metadata={"b": 2, "a": 1},
    )
    assert s1.checksum == s2.checksum


def test_checksum_changes_on_semantic_change():
    """10. Verifica que el checksum cambia si cambia cualquier campo semántico."""
    now = datetime.now(timezone.utc)
    base = RegisteredSource(
        source_id="src-det-1",
        name="Base Name",
        source_type=SourceType.MARKETPLACE_API,
        provider="meli",
        canonical_identifier="marketplace_api:meli:src-det-1",
        created_at=now,
        updated_at=now,
    )
    mod = RegisteredSource(
        source_id="src-det-1",
        name="Modified Name",
        source_type=SourceType.MARKETPLACE_API,
        provider="meli",
        canonical_identifier="marketplace_api:meli:src-det-1",
        created_at=now,
        updated_at=now,
    )
    assert base.checksum != mod.checksum


def test_same_source_replay_idempotent(tmp_path):
    """11. Verifica que re-registrar la misma fuente con mismo contenido es idempotente."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    s1 = service.register_source(
        source_id="src-idemp-1",
        name="Idempotent Source",
        source_type=SourceType.SUPPLIER,
        provider="direct_supplier",
    )

    s2 = service.register_source(
        source_id="src-idemp-1",
        name="Idempotent Source",
        source_type=SourceType.SUPPLIER,
        provider="direct_supplier",
    )

    assert s1.checksum == s2.checksum
    assert s1.source_id == s2.source_id


def test_conflicting_content_rejected(tmp_path):
    """12. Verifica que registrar mismo source_id y versión con distinto contenido lanza conflicto."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    service.register_source(
        source_id="src-conflict-1",
        name="Initial Source",
        source_type=SourceType.WEB_SOURCE,
        provider="web_hunter",
        version="1.0.0",
    )

    with pytest.raises(SourceConflictException, match="Conflict registering source"):
        service.register_source(
            source_id="src-conflict-1",
            name="Conflicting Altered Name",
            source_type=SourceType.WEB_SOURCE,
            provider="web_hunter",
            version="1.0.0",
        )


def test_status_semantics(tmp_path):
    """13. Verifica los estados de lifecycle del registro."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    src_active = service.register_source("s1", "S1", SourceType.INTERNAL_SYSTEM, "sys", status=SourceStatus.ACTIVE)
    src_dep = service.register_source("s2", "S2", SourceType.INTERNAL_SYSTEM, "sys", status=SourceStatus.DEPRECATED)

    assert src_active.status == SourceStatus.ACTIVE
    assert src_dep.status == SourceStatus.DEPRECATED


def test_list_and_get_operations(tmp_path):
    """14. Verifica listar y obtener fuentes con filtros."""
    repo = JsonSourceRegistryRepository(base_dir=tmp_path / "registry")
    service = SourceRegistryService(repository=repo)

    service.register_source("meli-1", "MercadoLibre 1", SourceType.MARKETPLACE_API, "meli")
    service.register_source("meli-2", "MercadoLibre 2", SourceType.MARKETPLACE_API, "meli")
    service.register_source("supp-1", "Supplier 1", SourceType.SUPPLIER, "supplier_a")

    all_sources = service.list_sources()
    assert len(all_sources) == 3

    meli_sources = service.list_sources(source_type=SourceType.MARKETPLACE_API)
    assert len(meli_sources) == 2

    supp_sources = service.list_sources(source_type=SourceType.SUPPLIER)
    assert len(supp_sources) == 1
    assert supp_sources[0].source_id == "supp-1"

    retrieved = service.get_source("meli-1")
    assert retrieved is not None
    assert retrieved.name == "MercadoLibre 1"


def test_no_ttl_logic():
    """15. Verifica estricta frontera L.1: RegisteredSource y el servicio no calculan ni gestionan TTL/freshness."""
    assert not hasattr(RegisteredSource, "is_fresh")
    assert not hasattr(RegisteredSource, "ttl_seconds")
    assert not hasattr(RegisteredSource, "expires_at")
    assert not hasattr(SourceRegistryService, "check_freshness")


def test_no_confidence_logic():
    """16. Verifica estricta frontera L.1: No hay cálculo de confidence en L.1."""
    assert not hasattr(RegisteredSource, "calculate_confidence")
    assert not hasattr(SourceRegistryService, "score_confidence")


def test_no_provenance_ownership():
    """17. Verifica estricta frontera L.1: No hay trazabilidad de datos individuales en L.1."""
    assert not hasattr(RegisteredSource, "lineage")
    assert not hasattr(RegisteredSource, "trace_record")
    assert not hasattr(SourceRegistryService, "record_data_point")


def test_safe_metadata_deep_freeze():
    """18. Verifica que la metadata se congela profundamente en estructuras anidadas."""
    now = datetime.now(timezone.utc)
    source = RegisteredSource(
        source_id="src-nested-meta",
        name="Nested Metadata Test",
        source_type=SourceType.INTERNAL_SYSTEM,
        provider="core",
        canonical_identifier="internal_system:core:src-nested-meta",
        created_at=now,
        updated_at=now,
        metadata={"level1": {"level2": ["a", "b"]}},
    )

    assert isinstance(source.metadata["level1"], MappingProxyType)
    assert isinstance(source.metadata["level1"]["level2"], tuple)
    with pytest.raises(TypeError):
        source.metadata["level1"]["new"] = 1
