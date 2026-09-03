"""
Tests de integración y E2E para Duplicate Detection L.7 (Transversal Data Quality / Governance).

Escenarios obligatorios:
A. Identical supplier record imported twice -> DUPLICATE.
B. Same product, new observation time -> NOT_DUPLICATE.
C. Same logical replay -> DUPLICATE / REPLAY_DUPLICATE.
D. Same entity, independent sources -> NOT automatic duplicate.
E. L.6 NO_MATCH -> NOT_DUPLICATE.
F. L.6 UNKNOWN/POSSIBLE -> no false duplicate.
G. Restart -> same result (durabilidad de repositorios y grupos).
H. Tampered persistence -> corruption detected.
I. Concurrent replay -> ONE logical result/group.
J. Flujo E2E Transversal Data Quality / Governance:
   Source Registry (L.1) -> Provenance (L.2) -> Schema Validation (L.5) -> Entity Resolution (L.6) -> Duplicate Detection (L.7).
   Demuestra rigurosamente:
   1) same entity != duplicate (mismo canonical_entity_id pero distintas fuentes o momentos)
   2) same logical record replay -> duplicate.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import pytest
import threading

# L.7
from src.domain.duplicate_detection.models import (
    DuplicateStatus,
    DuplicateReasonCode,
    DuplicateCandidate,
    DuplicateDetectionPolicy,
    DuplicateDetectionResult,
    DuplicateGroup,
    compute_semantic_fingerprint,
    compute_duplicate_candidate_checksum,
    compute_duplicate_policy_checksum,
    compute_duplicate_result_checksum,
    compute_duplicate_group_checksum,
)
from src.application.duplicate_detection.service import (
    DuplicateDetectionService,
    create_default_product_dedup_policy,
    create_default_replay_policy,
)
from src.infrastructure.persistence.data.json.duplicate_detection_repository import (
    JsonDuplicateDetectionPolicyRepository,
    JsonDuplicateDetectionRepository,
    DuplicateDetectionConflictError,
    DuplicateDetectionPolicyConflictError,
    CorruptedDuplicatePolicyError,
    CorruptedDuplicateResultError,
    CorruptedDuplicateGroupError,
    CorruptedDuplicateDetectionRecordError,
)

# L.6
from src.domain.entity_resolution.models import (
    EntityType,
    IdentifierType,
    MatchStatus,
    EntityIdentifier,
    EntityReference,
    EntityResolutionPolicy,
)
from src.application.entity_resolution.service import EntityResolutionService, create_default_product_policy
from src.infrastructure.persistence.data.json.entity_resolution_repository import (
    JsonEntityResolutionPolicyRepository,
    JsonEntityResolutionRepository,
)

# L.1 - L.5
from src.domain.source_registry.models import RegisteredSource, SourceType, SourceStatus
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository
from src.domain.data_provenance.models import ProvenanceRecord, SubjectType
from src.infrastructure.persistence.data.json.data_provenance_repository import JsonProvenanceRepository
from src.domain.schema_validation.models import (
    ValidationStatus,
    FieldType,
    AdditionalFieldsPolicy,
    FieldDefinition,
    SchemaDefinition,
)
from src.application.schema_validation.service import SchemaValidationService
from src.infrastructure.persistence.data.json.schema_repository import JsonSchemaRegistryRepository
from src.domain.reliability.ports import ClockPort


class FrozenClock(ClockPort):
    def __init__(self, fixed_dt: datetime):
        self._dt = fixed_dt

    def now(self) -> datetime:
        return self._dt

    def utcnow(self) -> datetime:
        return self._dt

    def sleep(self, seconds: float) -> None:
        pass


def test_scenario_a_identical_supplier_record_imported_twice(tmp_path: Path):
    """Escenario A: Registro idéntico de proveedor importado dos veces -> DUPLICATE."""
    repo = JsonDuplicateDetectionRepository(tmp_path)
    service = DuplicateDetectionService(repository=repo)
    now = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)

    payload = {"brand": "Logitech", "model": "MX Master 3S", "price": Decimal("99.99"), "currency": "USD"}

    c1 = DuplicateCandidate(
        record_id="sup_batch_001_item1",
        source_id="syscom_supplier",
        canonical_entity_id="canon_mouse_mx3s",
        payload=payload,
        observed_at=now,
    )
    c2 = DuplicateCandidate(
        record_id="sup_batch_002_item1",
        source_id="syscom_supplier",
        canonical_entity_id="canon_mouse_mx3s",
        payload=payload,
        observed_at=now + timedelta(minutes=5),  # Dentro de la ventana de 24h
    )

    result = service.evaluate_pair(c1, c2)
    assert result.status == DuplicateStatus.DUPLICATE
    assert result.reason_code == DuplicateReasonCode.EXACT_SEMANTIC_MATCH

    saved = repo.save_result(result)
    assert saved.result_id == result.result_id


def test_scenario_b_same_product_new_observation_time(tmp_path: Path):
    """Escenario B: Mismo producto, nueva observación en el tiempo -> NOT_DUPLICATE (preserva historia)."""
    service = DuplicateDetectionService()
    t_monday = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)
    t_friday = datetime(2026, 9, 5, 9, 0, 0, tzinfo=timezone.utc)  # > 24h

    payload = {"brand": "Apple", "model": "MacBook Pro 16", "price": Decimal("2499.00")}

    c_monday = DuplicateCandidate(
        record_id="obs_mon_01",
        source_id="apple_store_cl",
        canonical_entity_id="canon_mbp_16",
        payload=payload,
        observed_at=t_monday,
    )
    c_friday = DuplicateCandidate(
        record_id="obs_fri_01",
        source_id="apple_store_cl",
        canonical_entity_id="canon_mbp_16",
        payload=payload,
        observed_at=t_friday,
    )

    result = service.evaluate_pair(c_monday, c_friday)
    assert result.status == DuplicateStatus.NOT_DUPLICATE
    assert result.reason_code == DuplicateReasonCode.SAME_ENTITY_DISTINCT_TEMPORAL_EVENT


def test_scenario_c_same_logical_replay(tmp_path: Path):
    """Escenario C: Mismo logical record replay / idempotencia -> REPLAY_DUPLICATE / EXACT_DUPLICATE."""
    service = DuplicateDetectionService()
    now = datetime(2026, 9, 3, 11, 0, 0, tzinfo=timezone.utc)
    payload = {"order_id": "ORD-9999", "amount": Decimal("150.50"), "user": "usr_42"}

    replay_policy = create_default_replay_policy()

    c_original = DuplicateCandidate(
        record_id="msg_event_888",
        source_id="webhook_bus",
        idempotency_key="idemp_key_webhook_888",
        payload=payload,
        observed_at=now,
    )
    c_replayed = DuplicateCandidate(
        record_id="msg_event_888",
        source_id="webhook_bus",
        idempotency_key="idemp_key_webhook_888",
        payload=payload,
        observed_at=now + timedelta(seconds=12),
    )

    result = service.evaluate_pair(c_original, c_replayed, policy=replay_policy)
    assert result.status == DuplicateStatus.REPLAY_DUPLICATE
    assert result.is_exact_replay is True
    assert result.reason_code == DuplicateReasonCode.REPLAY_PAYLOAD_MATCH


def test_scenario_d_same_entity_independent_sources(tmp_path: Path):
    """Escenario D: Misma entidad en fuentes independientes -> NOT automatic duplicate (evidencia independiente)."""
    service = DuplicateDetectionService()
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    # Ambas fuentes publican el mismo modelo de switch Cisco
    payload_syscom = {"brand": "Cisco", "model": "CBS350-24T-4G", "price": Decimal("420.00")}
    payload_ingram = {"brand": "Cisco", "model": "CBS350-24T-4G", "price": Decimal("420.00")}

    c_syscom = DuplicateCandidate(
        record_id="syscom_item_33",
        source_id="syscom_distributor",
        canonical_entity_id="canon_cisco_switch_350",
        payload=payload_syscom,
        observed_at=now,
    )
    c_ingram = DuplicateCandidate(
        record_id="ingram_item_77",
        source_id="ingram_micro_distributor",
        canonical_entity_id="canon_cisco_switch_350",
        payload=payload_ingram,
        observed_at=now,
    )

    # Política estándar de producto: require_same_source = True
    result = service.evaluate_pair(c_syscom, c_ingram)
    assert result.status == DuplicateStatus.NOT_DUPLICATE
    assert result.reason_code == DuplicateReasonCode.SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE


def test_scenario_e_l6_no_match_leads_to_not_duplicate(tmp_path: Path):
    """Escenario E: L.6 asigna distinto canonical_entity_id (NO_MATCH) -> NOT_DUPLICATE."""
    service = DuplicateDetectionService()
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    c1 = DuplicateCandidate(
        record_id="item_canon_alpha",
        source_id="warehouse_central",
        canonical_entity_id="canonical_product_gtin_111",
        payload={"brand": "Sony", "model": "Headphones"},
        observed_at=now,
    )
    c2 = DuplicateCandidate(
        record_id="item_canon_beta",
        source_id="warehouse_central",
        canonical_entity_id="canonical_product_gtin_222",
        payload={"brand": "Sony", "model": "Headphones"},
        observed_at=now,
    )

    result = service.evaluate_pair(c1, c2)
    assert result.status == DuplicateStatus.NOT_DUPLICATE
    assert result.reason_code == DuplicateReasonCode.DIFFERENT_CANONICAL_ENTITY


def test_scenario_f_l6_unknown_possible_no_false_duplicate(tmp_path: Path):
    """Escenario F: Ambigüedad o payload incompleto -> no produce falso duplicado."""
    service = DuplicateDetectionService()
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    # Payload insuficiente / vacío
    c_empty1 = DuplicateCandidate(
        record_id="rec_empty_a",
        source_id="source_unknown",
        canonical_entity_id=None,
        payload={},
        observed_at=now,
    )
    c_empty2 = DuplicateCandidate(
        record_id="rec_empty_b",
        source_id="source_unknown",
        canonical_entity_id=None,
        payload={},
        observed_at=now,
    )

    result = service.evaluate_pair(c_empty1, c_empty2)
    assert result.status == DuplicateStatus.UNKNOWN
    assert result.reason_code == DuplicateReasonCode.INSUFFICIENT_DATA
    assert result.status != DuplicateStatus.DUPLICATE


def test_scenario_g_restart_durability_and_reload(tmp_path: Path):
    """Escenario G: Restart -> mismos resultados y grupos tras reiniciar repositorios."""
    repo_dir = tmp_path / "dup_store"
    repo1 = JsonDuplicateDetectionRepository(repo_dir)
    service1 = DuplicateDetectionService(repository=repo1)

    now = datetime(2026, 9, 3, 14, 0, 0, tzinfo=timezone.utc)
    payload = {"brand": "SanDisk", "model": "Ultra 128GB", "price": Decimal("15.99")}

    c1 = DuplicateCandidate(record_id="rec_sd1", source_id="src_retail", canonical_entity_id="canon_sd_128", payload=payload, observed_at=now)
    c2 = DuplicateCandidate(record_id="rec_sd2", source_id="src_retail", canonical_entity_id="canon_sd_128", payload=payload, observed_at=now)

    res = service1.evaluate_pair(c1, c2)
    repo1.save_result(res)

    group = DuplicateGroup(
        group_id="grp_sandisk_128",
        canonical_fingerprint=res.primary_fingerprint,
        member_record_ids=("rec_sd1", "rec_sd2"),
        canonical_entity_id="canon_sd_128",
        created_at=now,
        updated_at=now,
    )
    repo1.save_group(group)

    # Simular reinicio del servicio creando nueva instancia de repositorio
    repo2 = JsonDuplicateDetectionRepository(repo_dir)
    loaded_res = repo2.get_result(res.result_id)
    loaded_group = repo2.get_group("grp_sandisk_128")

    assert loaded_res is not None
    assert loaded_res.result_id == res.result_id
    assert loaded_res.status == DuplicateStatus.DUPLICATE
    assert loaded_res.checksum == res.checksum

    assert loaded_group is not None
    assert loaded_group.group_id == group.group_id
    assert set(loaded_group.member_record_ids) == {"rec_sd1", "rec_sd2"}
    assert loaded_group.checksum == group.checksum


def test_scenario_h_tampered_persistence_corruption_detected(tmp_path: Path):
    """Escenario H: Detección de corrupción y alteración maliciosa en persistencia."""
    repo = JsonDuplicateDetectionRepository(tmp_path)
    now = datetime(2026, 9, 3, 15, 0, 0, tzinfo=timezone.utc)

    group = DuplicateGroup(
        group_id="grp_tamper_test",
        canonical_fingerprint="fp_hash_1234567890",
        member_record_ids=("rec_1", "rec_2"),
        canonical_entity_id="canon_1",
        created_at=now,
        updated_at=now,
    )
    repo.save_group(group)

    group_file = tmp_path / "groups" / "grp_tamper_test.json"
    assert group_file.exists()

    content = group_file.read_text(encoding="utf-8")
    tampered_content = content.replace("rec_2", "rec_injected_hacker")
    group_file.write_text(tampered_content, encoding="utf-8")

    with pytest.raises(CorruptedDuplicateDetectionRecordError):
        repo.get_group("grp_tamper_test")


def test_scenario_i_concurrent_replay_one_logical_result(tmp_path: Path):
    """Escenario I: Concurrencia de replays -> un único resultado lógico coherente y seguro."""
    repo = JsonDuplicateDetectionRepository(tmp_path)
    clock = FrozenClock(datetime(2026, 9, 3, 16, 0, 0, tzinfo=timezone.utc))
    service = DuplicateDetectionService(repository=repo, clock=clock)
    now = datetime(2026, 9, 3, 16, 0, 0, tzinfo=timezone.utc)
    payload = {"sku": "CONCUR-1", "stock": 50}

    c1 = DuplicateCandidate(
        record_id="rec_conc_1",
        source_id="src_warehouse",
        idempotency_key="idemp_conc_1",
        payload=payload,
        observed_at=now,
    )
    c2 = DuplicateCandidate(
        record_id="rec_conc_1",
        source_id="src_warehouse",
        idempotency_key="idemp_conc_1",
        payload=payload,
        observed_at=now,
    )

    results = []
    errors = []

    def evaluate_and_persist():
        try:
            res = service.evaluate_pair(c1, c2)
            saved = repo.save_result(res)
            results.append(saved)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=evaluate_and_persist) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(results) == 10
    # Todos retornan el mismo result_id y checksum idempotente
    first_id = results[0].result_id
    assert all(r.result_id == first_id for r in results)
    assert all(r.status == DuplicateStatus.REPLAY_DUPLICATE for r in results)


def test_scenario_j_e2e_data_quality_governance_flow(tmp_path: Path):
    """
    Escenario J (E2E): Flujo Integral Transversal Data Quality / Governance:
    Source Registry (L.1) -> Provenance (L.2) -> Schema Validation (L.5) -> Entity Resolution (L.6) -> Duplicate Detection (L.7).

    Demuestra fehacientemente:
    1. SAME ENTITY != DUPLICATE:
       Dos fuentes distintas (Syscom vs MercadoLibre) con el mismo GTIN se resuelven a la misma entidad canónica (L.6 MATCH),
       pero al evaluarse en L.7 se reconocen como evidencias independientes de fuentes distintas (NOT_DUPLICATE).
    2. SAME LOGICAL RECORD REPLAY:
       La reimportación exacta del mismo registro de proveedor con la misma carga semántica resulta en DUPLICATE / REPLAY_DUPLICATE.
    """
    clock = FrozenClock(datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc))

    # 1. L.1 Source Registry
    source_repo = JsonSourceRegistryRepository(base_dir=tmp_path / "sources")
    src_supplier = RegisteredSource(
        source_id="syscom_cl",
        name="Syscom Chile Official B2B",
        source_type=SourceType.SUPPLIER,
        provider="syscom",
        canonical_identifier="supplier:syscom:cl",
        status=SourceStatus.ACTIVE,
        endpoint_reference="https://api.syscom.cl/catalog",
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    src_marketplace = RegisteredSource(
        source_id="mercadolibre_cl",
        name="MercadoLibre Chile Public Market",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:cl",
        status=SourceStatus.ACTIVE,
        endpoint_reference="https://api.mercadolibre.com/items",
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    source_repo.save_source(src_supplier)
    source_repo.save_source(src_marketplace)

    # 2. L.2 Data Provenance
    prov_repo = JsonProvenanceRepository(tmp_path / "provenance")
    prov_sup = ProvenanceRecord(
        provenance_id="prov_sup_2026_01",
        subject_id="SYS-ROUTER-AX3000",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        source_id=src_supplier.source_id,
        captured_at=clock.now() - timedelta(minutes=30),
    )
    prov_mkt = ProvenanceRecord(
        provenance_id="prov_mkt_2026_02",
        subject_id="MLC-AX3000-LISTING",
        subject_type=SubjectType.MARKET_OBSERVATION,
        source_id=src_marketplace.source_id,
        captured_at=clock.now() - timedelta(minutes=15),
    )
    prov_repo.save_provenance(prov_sup)
    prov_repo.save_provenance(prov_mkt)

    # 3. L.5 Schema Validation
    schema_repo = JsonSchemaRegistryRepository(tmp_path / "schemas")
    product_schema = SchemaDefinition(
        schema_id="router_product_schema",
        name="Router Product Payload",
        version="1.0.0",
        subject_type="PRODUCT",
        additional_fields_policy=AdditionalFieldsPolicy.ALLOW,
        fields=(
            FieldDefinition(field_name="gtin", field_type=FieldType.STRING),
            FieldDefinition(field_name="brand", field_type=FieldType.STRING),
            FieldDefinition(field_name="model", field_type=FieldType.STRING),
            FieldDefinition(field_name="price", field_type=FieldType.DECIMAL),
        ),
    )
    schema_repo.save_schema(product_schema)
    schema_service = SchemaValidationService(schema_registry=schema_repo)

    payload_sup = {"gtin": "6935364089900", "brand": "TP-Link", "model": "Archer AX55", "price": Decimal("89.90")}
    payload_mkt = {"gtin": "6935364089900", "brand": "TP-Link", "model": "Archer AX55", "price": Decimal("109.90")}

    val_sup = schema_service.validate(payload=payload_sup, subject_type="PRODUCT")
    val_mkt = schema_service.validate(payload=payload_mkt, subject_type="PRODUCT")
    assert val_sup.status == ValidationStatus.PASS
    assert val_mkt.status == ValidationStatus.PASS

    # 4. L.6 Entity Resolution
    er_policy_repo = JsonEntityResolutionPolicyRepository(tmp_path / "er_policies")
    er_res_repo = JsonEntityResolutionRepository(tmp_path / "er_resolutions")
    er_policy = create_default_product_policy()
    er_policy_repo.save_policy(er_policy)

    er_service = EntityResolutionService(
        policy_repository=er_policy_repo,
        repository=er_res_repo,
    )

    ref_sup = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id=src_supplier.source_id,
        source_entity_id=prov_sup.subject_id,
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value=payload_sup["gtin"], is_strong=True),
        ),
        canonical_attributes={"brand": payload_sup["brand"], "model": payload_sup["model"]},
        provenance_id=prov_sup.provenance_id,
    )
    ref_mkt = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id=src_marketplace.source_id,
        source_entity_id=prov_mkt.subject_id,
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value=payload_mkt["gtin"], is_strong=True),
        ),
        canonical_attributes={"brand": payload_mkt["brand"], "model": payload_mkt["model"]},
        provenance_id=prov_mkt.provenance_id,
    )

    er_result = er_service.resolve_pair(ref_sup, ref_mkt, policy=er_policy)
    assert er_result.status == MatchStatus.MATCH
    canonical_entity_id = er_result.canonical_entity_id
    assert canonical_entity_id == "canonical_product_gtin_6935364089900"

    # 5. L.7 Duplicate Detection
    dup_policy_repo = JsonDuplicateDetectionPolicyRepository(tmp_path / "dup_policies")
    dup_res_repo = JsonDuplicateDetectionRepository(tmp_path / "dup_results")
    dup_policy = create_default_product_dedup_policy()
    dup_policy_repo.save_policy(dup_policy)

    dup_service = DuplicateDetectionService(
        repository=dup_res_repo,
        policy_repository=dup_policy_repo,
        entity_resolution_service=er_service,
    )

    # Caso 1 E2E: SAME ENTITY != DUPLICATE (Distintas fuentes)
    candidate_sup = DuplicateCandidate(
        record_id=f"rec_{prov_sup.subject_id}",
        source_id=src_supplier.source_id,
        canonical_entity_id=canonical_entity_id,
        payload=payload_sup,
        observed_at=clock.now() - timedelta(minutes=30),
        provenance_id=prov_sup.provenance_id,
    )
    candidate_mkt = DuplicateCandidate(
        record_id=f"rec_{prov_mkt.subject_id}",
        source_id=src_marketplace.source_id,
        canonical_entity_id=canonical_entity_id,
        payload=payload_mkt,
        observed_at=clock.now() - timedelta(minutes=15),
        provenance_id=prov_mkt.provenance_id,
    )

    cross_source_eval = dup_service.evaluate_pair(candidate_sup, candidate_mkt, policy=dup_policy)
    assert cross_source_eval.status == DuplicateStatus.NOT_DUPLICATE
    assert cross_source_eval.reason_code == DuplicateReasonCode.SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE
    # Ambas evidencias quedan preservadas sin colapsar ni borrar

    # Caso 2 E2E: SAME LOGICAL RECORD REPLAY (Re-importación idéntica del proveedor)
    candidate_sup_replay = DuplicateCandidate(
        record_id=f"rec_{prov_sup.subject_id}",
        source_id=src_supplier.source_id,
        canonical_entity_id=canonical_entity_id,
        payload=payload_sup,
        observed_at=clock.now(),
        provenance_id="prov_sup_2026_01_replay",
        idempotency_key="idemp_sup_batch_01",
    )
    candidate_sup_first = DuplicateCandidate(
        record_id=f"rec_{prov_sup.subject_id}",
        source_id=src_supplier.source_id,
        canonical_entity_id=canonical_entity_id,
        payload=payload_sup,
        observed_at=clock.now() - timedelta(minutes=30),
        provenance_id=prov_sup.provenance_id,
        idempotency_key="idemp_sup_batch_01",
    )

    replay_eval = dup_service.evaluate_pair(candidate_sup_first, candidate_sup_replay, policy=dup_policy)
    assert replay_eval.status in (DuplicateStatus.REPLAY_DUPLICATE, DuplicateStatus.DUPLICATE)
    assert replay_eval.reason_code == DuplicateReasonCode.REPLAY_PAYLOAD_MATCH
    assert replay_eval.is_exact_replay is True
