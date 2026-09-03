"""
Tests de integración exhaustivos para Data Provenance (Hito L.2 - Transversal Data Quality / Governance).

Escenarios cubiertos:
A. Source Registry source -> Data Provenance -> persistencia física JSON -> recuperación íntegra.
B. Market Intelligence fact / observation -> trazabilidad recursiva hasta la fuente registrada en L.1.
C. Supplier fact / quote -> trazabilidad hasta el proveedor registrado en L.1.
D. Derived fact -> linaje multidimensional con múltiples padres -> resolución de todas las fuentes raíz.
E. Restart / Reload -> persistencia de índices e inmutabilidad preservada tras reinicio del proceso.
F. Tampered provenance record -> detección inmediata de corrupción SHA-256 sin autorreparación silenciosa.
G. Replay determinista -> sin duplicación de registros físicos o índices secundarios.
H. Conflicting lineage replay -> conflicto explícito sin sobrescritura destructiva.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from src.domain.data_provenance.models import (
    ProvenanceRecord,
    SubjectType,
    SourceLineageTrace,
    generate_deterministic_provenance_id,
)
from src.domain.source_registry.models import RegisteredSource, SourceType, SourceStatus
from src.application.data_provenance.service import (
    DataProvenanceService,
    ProvenanceConflictServiceError,
    UnknownSourceError,
)
from src.infrastructure.persistence.data.json.data_provenance_repository import (
    JsonProvenanceRepository,
    ProvenanceConflictError,
    CorruptedProvenanceRecordError,
)
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository


@pytest.fixture
def integrated_setup(tmp_path):
    sources_dir = tmp_path / "sources"
    provenance_dir = tmp_path / "provenance"

    source_repo = JsonSourceRegistryRepository(sources_dir)
    prov_repo = JsonProvenanceRepository(provenance_dir)

    # Registrar fuentes oficiales L.1
    s_meli = RegisteredSource(
        source_id="src-meli-chile",
        name="Mercado Libre Chile Official API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:mlc",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    s_supplier = RegisteredSource(
        source_id="src-supplier-direct-101",
        name="Distribuidora Mayorista Central",
        source_type=SourceType.SUPPLIER,
        provider="mayorista_central",
        canonical_identifier="supplier:mayorista_central:prov-101",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    source_repo.save_source(s_meli)
    source_repo.save_source(s_supplier)

    service = DataProvenanceService(
        repository=prov_repo,
        source_registry_repository=source_repo,
    )

    return {
        "service": service,
        "prov_repo": prov_repo,
        "source_repo": source_repo,
        "sources_dir": sources_dir,
        "provenance_dir": provenance_dir,
    }


def test_scenario_a_source_registry_to_provenance_lifecycle(integrated_setup):
    service = integrated_setup["service"]
    prov_repo = integrated_setup["prov_repo"]
    now = datetime.now(timezone.utc)

    # 1. Registrar procedencia
    prov_rec = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-item-MLC999888",
        evidence_id="ev-traffic-signal-01",
        field_path="price.amount",
        captured_at=now,
        metadata={"category": "electronics", "query": "gadget"},
    )

    assert prov_rec.provenance_id.startswith("prov-")
    assert prov_rec.checksum != ""

    # 2. Recuperar directamente por ID
    loaded = prov_repo.get_provenance(prov_rec.provenance_id)
    assert loaded is not None
    assert loaded.provenance_id == prov_rec.provenance_id
    assert loaded.source_id == "src-meli-chile"
    assert loaded.evidence_id == "ev-traffic-signal-01"
    assert loaded.checksum == prov_rec.checksum


def test_scenario_b_market_intelligence_trace_to_source(integrated_setup):
    service = integrated_setup["service"]
    now = datetime.now(timezone.utc)

    # Observación de mercado
    service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-smartwatch",
        evidence_id="ev-meli-raw-snapshot-01",
        field_path="buy_box_winner_price",
        captured_at=now,
    )

    # Rastrear a la fuente de L.1
    trace = service.trace_to_sources(subject_id="obs-meli-smartwatch", field_path="buy_box_winner_price")
    assert trace.is_complete is True
    assert "src-meli-chile" in trace.root_source_ids
    assert len(trace.records_in_lineage) == 1
    assert trace.records_in_lineage[0].source_id == "src-meli-chile"


def test_scenario_c_supplier_fact_trace_to_supplier_source(integrated_setup):
    service = integrated_setup["service"]
    now = datetime.now(timezone.utc)

    # Cotización de proveedor
    service.record_provenance(
        source_id="src-supplier-direct-101",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="quote-sku-888-tier1",
        evidence_id="ev-supplier-catalog-pdf",
        field_path="unit_price",
        captured_at=now,
    )

    trace = service.trace_to_sources(subject_id="quote-sku-888-tier1", field_path="unit_price")
    assert trace.is_complete is True
    assert "src-supplier-direct-101" in trace.root_source_ids


def test_scenario_d_derived_fact_multiple_parents_root_resolution(integrated_setup):
    service = integrated_setup["service"]
    now = datetime.now(timezone.utc)

    # Hecho de Mercado 1
    meli_prov = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_LISTING,
        subject_id="item-mlc-123",
        captured_at=now,
    )

    # Hecho de Proveedor 2
    supplier_prov = service.record_provenance(
        source_id="src-supplier-direct-101",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="quote-sku-123",
        captured_at=now,
    )

    # Hecho Derivado: Oportunidad con Margen Calculado
    opp_prov = service.record_provenance(
        source_id="src-meli-chile",  # Contextual o canal de publicación
        subject_type=SubjectType.PRODUCT_OPPORTUNITY,
        subject_id="opp-calc-margin-123",
        parent_provenance_ids=[meli_prov.provenance_id, supplier_prov.provenance_id],
        transformation_id="trans-unit-economics-calc",
        captured_at=now,
    )

    # Decisión posterior basada en la oportunidad
    decision_prov = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.DECISION,
        subject_id="dec-publish-product-123",
        parent_provenance_ids=[opp_prov.provenance_id],
        transformation_id="trans-autonomous-decision-loop",
        captured_at=now,
    )

    # Trazabilidad completa desde la decisión final hasta ambas fuentes raíz originales
    trace = service.trace_to_sources(provenance_id=decision_prov.provenance_id)
    assert trace.is_complete is True
    assert set(trace.root_source_ids) == {"src-meli-chile", "src-supplier-direct-101"}
    assert len(trace.records_in_lineage) == 4


def test_scenario_e_restart_and_durability(integrated_setup):
    service = integrated_setup["service"]
    prov_dir = integrated_setup["provenance_dir"]
    source_repo = integrated_setup["source_repo"]
    now = datetime.now(timezone.utc)

    # 1. Registrar
    rec = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-durable-01",
        captured_at=now,
    )

    # 2. Simular reinicio creando nueva instancia de repositorio y servicio apuntando al mismo directorio
    restarted_repo = JsonProvenanceRepository(prov_dir)
    restarted_service = DataProvenanceService(
        repository=restarted_repo,
        source_registry_repository=source_repo,
    )

    loaded = restarted_service.get_provenance(rec.provenance_id)
    assert loaded is not None
    assert loaded.provenance_id == rec.provenance_id
    assert loaded.checksum == rec.checksum

    # Búsqueda por sujeto preservada
    found = restarted_service.find_for_subject("obs-durable-01")
    assert len(found) == 1
    assert found[0].provenance_id == rec.provenance_id


def test_scenario_f_tampered_provenance_detected(integrated_setup):
    service = integrated_setup["service"]
    prov_dir = integrated_setup["provenance_dir"]
    now = datetime.now(timezone.utc)

    rec = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-tamper-target",
        captured_at=now,
    )

    # Alterar archivo en disco físicamente
    rec_file = Path(prov_dir) / "provenance" / f"{rec.provenance_id}.json"
    with open(rec_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["subject_id"] = "obs-tampered-altered"  # modificación no autorizada

    with open(rec_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Al intentar leer, debe lanzar error de verificación de checksum / corrupción
    prov_repo = integrated_setup["prov_repo"]
    with pytest.raises(ValueError) as excinfo:
        prov_repo.get_provenance(rec.provenance_id)
    assert "Checksum mismatch" in str(excinfo.value)


def test_scenario_g_replay_no_duplicate(integrated_setup):
    service = integrated_setup["service"]
    prov_repo = integrated_setup["prov_repo"]
    now = datetime.now(timezone.utc)

    rec1 = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-replay-check",
        captured_at=now,
    )
    rec2 = service.record_provenance(
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-replay-check",
        captured_at=now,
    )
    assert rec1.provenance_id == rec2.provenance_id

    subject_list = prov_repo.find_by_subject("obs-replay-check")
    assert len(subject_list) == 1


def test_scenario_h_conflicting_lineage_explicit_conflict(integrated_setup):
    service = integrated_setup["service"]
    now = datetime.now(timezone.utc)

    # Registrar primero
    service.record_provenance(
        provenance_id="prov-fixed-id-1",
        source_id="src-meli-chile",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-original",
        captured_at=now,
    )

    # Replay con payload conflictivo bajo el mismo ID
    with pytest.raises(ProvenanceConflictServiceError):
        service.record_provenance(
            provenance_id="prov-fixed-id-1",
            source_id="src-meli-chile",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-DIFFERENT",
            captured_at=now,
        )
