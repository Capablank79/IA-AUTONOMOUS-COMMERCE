"""
Tests unitarios exhaustivos para Data Provenance (Hito L.2 - Transversal Data Quality / Governance).

Cubre:
1. Inmutabilidad estricta del registro de procedencia (frozen=True, tuples, MappingProxyType).
2. Linaje directo de fuente (RegisteredSource L.1 -> ProvenanceRecord L.2).
3. Requerimiento de referencia de fuente (source_id válido).
4. Identidad determinista (generate_deterministic_provenance_id).
5. Checksum canónico SHA-256 sobre campos semánticos inmutables.
6. Mutación semántica altera el checksum canónico.
7. Idempotencia estricta ante replay con datos idénticos.
8. Conflicto explícito ante colisión de ID con datos/checksum diferente.
9. Field-level provenance (linaje granular a nivel de campo / path).
10. Parent provenance y soporte de datos derivados (DAG simple).
11. Detección y rechazo de auto-ciclo (self-parent).
12. Detección y rechazo de ciclos indirectos en la jerarquía de procedencia.
13. Normalización y deduplicación de parent IDs.
14. Manejo y rechazo explícito de source_id no registrado en L.1.
15. Sanitización de secretos en metadata (tokens, passwords, apikeys, auth headers).
16. Seguridad de identificadores (path traversal safety).
17. Ausencia de lógica de Freshness (L.3).
18. Ausencia de lógica de Confidence (L.4).
19. Ausencia de lógica de Conflict Resolution (L.8).
20. Ausencia de lógica de Duplicate Detection ownership (L.7).
"""

from datetime import datetime, timezone
import pytest
from types import MappingProxyType

from src.domain.data_provenance.models import (
    ProvenanceRecord,
    SubjectType,
    SourceLineageTrace,
    compute_provenance_checksum,
    generate_deterministic_provenance_id,
)
from src.domain.data_provenance.ports import ProvenanceRepositoryPort
from src.application.data_provenance.service import (
    DataProvenanceService,
    UnknownSourceError,
    ProvenanceCycleError,
    ProvenanceConflictServiceError,
)
from src.domain.source_registry.models import RegisteredSource, SourceType, SourceStatus
from src.infrastructure.persistence.data.json.data_provenance_repository import (
    JsonProvenanceRepository,
    ProvenanceConflictError,
    CorruptedProvenanceRecordError,
)
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository


@pytest.fixture
def sample_registered_source(tmp_path):
    src_repo = JsonSourceRegistryRepository(tmp_path / "sources")
    source = RegisteredSource(
        source_id="src-meli-chile",
        name="Mercado Libre Chile API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:mlc",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    src_repo.save_source(source)
    return source, src_repo


@pytest.fixture
def prov_service(tmp_path, sample_registered_source):
    source, src_repo = sample_registered_source
    prov_repo = JsonProvenanceRepository(tmp_path / "provenance")
    service = DataProvenanceService(
        repository=prov_repo,
        source_registry_repository=src_repo,
    )
    return service, prov_repo, src_repo


def test_01_immutable_provenance():
    now = datetime.now(timezone.utc)
    rec = ProvenanceRecord(
        provenance_id="prov-100",
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-item-123",
        captured_at=now,
        metadata={"category": "electronics"},
    )
    assert rec.provenance_id == "prov-100"
    assert rec.checksum != ""
    assert isinstance(rec.metadata, MappingProxyType)

    with pytest.raises(Exception):
        rec.provenance_id = "prov-mutated"  # type: ignore

    with pytest.raises(Exception):
        rec.metadata["category"] = "other"  # type: ignore


def test_02_direct_source_lineage(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)

    rec = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-999",
        captured_at=now,
    )
    assert rec.source_id == "src-meli-chile"
    assert rec.subject_id == "obs-meli-999"
    assert rec.is_derived is False
    assert len(rec.parent_provenance_ids) == 0


def test_03_source_reference_required(prov_service):
    service, _, _ = prov_service

    with pytest.raises(UnknownSourceError):
        service.record_provenance(
            source_id="src-unregistered-999",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-test",
        )


def test_04_deterministic_id():
    id_1 = generate_deterministic_provenance_id(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="MLC-123",
        field_path="price.amount",
        evidence_id="ev-01",
    )
    id_2 = generate_deterministic_provenance_id(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="MLC-123",
        field_path="price.amount",
        evidence_id="ev-01",
    )
    id_diff = generate_deterministic_provenance_id(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="MLC-123",
        field_path="stock",
        evidence_id="ev-01",
    )
    assert id_1 == id_2
    assert id_1.startswith("prov-")
    assert id_1 != id_diff


def test_05_checksum_verification():
    now = datetime.now(timezone.utc)
    rec = ProvenanceRecord(
        provenance_id="prov-chk-1",
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-chk-1",
        captured_at=now,
    )
    assert rec.checksum is not None
    assert len(rec.checksum) == 64  # SHA-256 hex string


def test_06_semantic_mutation_changes_checksum():
    now = datetime.now(timezone.utc)
    c1 = compute_provenance_checksum(
        provenance_id="prov-1",
        source_id="src-1",
        source_version="1.0.0",
        source_record_id=None,
        evidence_id=None,
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="sub-1",
        field_path="price",
        captured_at=now.isoformat(),
        parent_provenance_ids=(),
        transformation_id=None,
        correlation_id="corr-1",
        causation_id=None,
        schema_version="1.0.0",
        metadata={"k": "v1"},
    )
    c2 = compute_provenance_checksum(
        provenance_id="prov-1",
        source_id="src-1",
        source_version="1.0.0",
        source_record_id=None,
        evidence_id=None,
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="sub-1",
        field_path="price",
        captured_at=now.isoformat(),
        parent_provenance_ids=(),
        transformation_id=None,
        correlation_id="corr-1",
        causation_id=None,
        schema_version="1.0.0",
        metadata={"k": "v2"},
    )
    assert c1 != c2


def test_07_same_replay_idempotent(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)

    rec1 = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-idemp-1",
        captured_at=now,
        metadata={"batch": 1},
    )
    rec2 = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-idemp-1",
        captured_at=now,
        metadata={"batch": 1},
    )
    assert rec1.provenance_id == rec2.provenance_id
    assert rec1.checksum == rec2.checksum


def test_08_conflict_rejected(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)

    service.record_provenance(
        provenance_id="prov-conflict-explicit",
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-conflict-1",
        captured_at=now,
        metadata={"attr": "alpha"},
    )

    with pytest.raises(ProvenanceConflictServiceError):
        service.record_provenance(
            provenance_id="prov-conflict-explicit",
            source_id="src-meli-chile",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-conflict-1",
            captured_at=now,
            metadata={"attr": "beta"},  # diferente contenido
        )


def test_09_field_level_provenance(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)

    rec_price = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_LISTING,
        subject_id="item-mlc-100",
        field_path="price.amount",
        captured_at=now,
    )
    rec_stock = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_LISTING,
        subject_id="item-mlc-100",
        field_path="available_quantity",
        captured_at=now,
    )

    assert rec_price.field_path == "price.amount"
    assert rec_stock.field_path == "available_quantity"
    assert rec_price.provenance_id != rec_stock.provenance_id

    subject_records = service.find_for_subject("item-mlc-100")
    assert len(subject_records) == 2


def test_10_parent_provenance_and_derived_data(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)

    # Padre 1
    p1 = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-1",
        captured_at=now,
    )
    # Padre 2
    p2 = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-2",
        captured_at=now,
    )

    # Derivado
    derived = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="fact-derived-1",
        parent_provenance_ids=[p1.provenance_id, p2.provenance_id],
        transformation_id="trans-aggregate-scores",
        captured_at=now,
    )

    assert derived.is_derived is True
    assert p1.provenance_id in derived.parent_provenance_ids
    assert p2.provenance_id in derived.parent_provenance_ids

    trace = service.trace_to_sources(provenance_id=derived.provenance_id)
    assert trace.is_complete is True
    assert "src-meli-chile" in trace.root_source_ids
    assert len(trace.records_in_lineage) == 3


def test_11_self_cycle_rejected(prov_service):
    service, _, _ = prov_service
    with pytest.raises(ProvenanceCycleError):
        service.record_provenance(
            provenance_id="prov-self-cycle",
            source_id="src-meli-chile",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-self",
            parent_provenance_ids=["prov-self-cycle"],
        )


def test_12_cycle_rejected_in_dag(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)

    # A -> B -> A (ciclo indirecto)
    rec_a = service.record_provenance(
        provenance_id="prov-node-a",
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-a",
        captured_at=now,
    )
    rec_b = service.record_provenance(
        provenance_id="prov-node-b",
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-b",
        parent_provenance_ids=[rec_a.provenance_id],
        captured_at=now,
    )

    with pytest.raises(ProvenanceCycleError):
        # Intentar crear C con padre B y luego agregar A con padre C que apunta a A
        service._verify_no_cycles(candidate_id="prov-node-a", current_parent_id="prov-node-b", visited=set())


def test_13_duplicate_parents_normalized():
    now = datetime.now(timezone.utc)
    rec = ProvenanceRecord(
        provenance_id="prov-parents-norm",
        source_id="src-meli-chile",
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="fact-1",
        captured_at=now,
        parent_provenance_ids=("prov-p1", "prov-p1", "prov-p2", "prov-p2"),
    )
    assert rec.parent_provenance_ids == ("prov-p1", "prov-p2")


def test_14_unknown_source_handling(prov_service):
    service, _, _ = prov_service
    with pytest.raises(UnknownSourceError):
        service.record_provenance(
            source_id="src-unknown-ghost",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-ghost",
        )


def test_15_secret_sanitization(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)

    rec = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-sec",
        captured_at=now,
        metadata={
            "api_key": "secret-12345",
            "bearer_token": "token-abcde",
            "safe_tag": "public-value",
        },
    )
    assert rec.metadata["api_key"] == "[REDACTED]"
    assert rec.metadata["bearer_token"] == "[REDACTED]"
    assert rec.metadata["safe_tag"] == "public-value"


def test_16_path_safety():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        ProvenanceRecord(
            provenance_id="../../etc/passwd",
            source_id="src-meli-chile",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-bad",
            captured_at=now,
        )


def test_17_no_freshness_logic(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)
    rec = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-nofresh",
        captured_at=now,
    )
    # L.2 no debe tener campos de evaluación de freshness / expiración comercial
    assert not hasattr(rec, "is_fresh")
    assert not hasattr(rec, "ttl_seconds")
    assert not hasattr(rec, "freshness_score")


def test_18_no_confidence_logic(prov_service):
    service, _, _ = prov_service
    now = datetime.now(timezone.utc)
    rec = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-noconf",
        captured_at=now,
    )
    # L.2 no debe computar scores de confianza (L.4)
    assert not hasattr(rec, "calculated_confidence")
    assert not hasattr(rec, "confidence_weight")


def test_19_no_conflict_resolution(prov_service):
    service, _, _ = prov_service
    # L.2 no resuelve discrepancias entre fuentes (L.8)
    assert not hasattr(service, "resolve_conflicts")
    assert not hasattr(service, "arbitrate_sources")


def test_20_no_duplicate_detection_ownership(prov_service):
    service, _, _ = prov_service
    # L.2 no deduplica entidades de negocio como productos o proveedores (L.7)
    assert not hasattr(service, "deduplicate_entities")
    assert not hasattr(service, "find_duplicate_products")
