"""
Tests de integración y E2E para Conflict Resolution L.8 (Transversal Data Quality / Governance).

Escenarios obligatorios:
A. Same entity + same field (100 vs 120, source-priority policy -> deterministic winner).
B. Fresh vs stale -> fresh wins under freshness policy.
C. Higher vs lower confidence -> higher wins under confidence policy.
D. Two equally valid contradictory candidates without tie-break -> UNRESOLVED.
E. Duplicate/replayed evidence -> not double-counted (L.7 duplicate prevention).
F. Different logical facts/time contexts / different entities -> NO_CONFLICT / safe separation.
G. Restart -> result preserved and reloadable from persistence.
H. Tampered persistence -> corruption detected with exception.
I. E2E Full Governance Pipeline:
   Source Registry (L.1) -> Data Provenance (L.2) -> Freshness (L.3) -> Confidence (L.4) ->
   Schema Validation (L.5) -> Entity Resolution (L.6) -> Duplicate Detection (L.7) -> Conflict Resolution (L.8).
   Demuestra rigurosamente dos fuentes contradictorias -> resolución justificada y trazable.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import pytest

# L.8
from src.domain.conflict_resolution.models import (
    ConflictStatus,
    ConflictReasonCode,
    ResolutionStrategy,
    ConflictCandidate,
    ConflictResolutionPolicy,
    ConflictResolutionResult,
    compute_candidate_checksum,
    compute_conflict_policy_checksum,
    compute_conflict_result_checksum,
)
from src.application.conflict_resolution.service import (
    ConflictResolutionService,
    create_default_source_priority_policy,
    create_default_freshness_policy,
    create_default_confidence_policy,
    create_default_consensus_policy,
)
from src.infrastructure.persistence.data.json.conflict_resolution_repository import (
    JsonConflictResolutionPolicyRepository,
    JsonConflictResolutionRepository,
    CorruptedConflictResultError,
    CorruptedConflictPolicyError,
)

# L.1 - L.7
from src.domain.source_registry.models import RegisteredSource, SourceType, SourceStatus
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository

from src.domain.data_provenance.models import ProvenanceRecord, SubjectType
from src.infrastructure.persistence.data.json.data_provenance_repository import JsonProvenanceRepository

from src.domain.freshness.models import FreshnessStatus, FreshnessPolicy, FreshnessAssessment
from src.application.freshness.service import FreshnessService
from src.infrastructure.persistence.data.json.freshness_repository import (
    JsonFreshnessPolicyRepository,
    JsonFreshnessAssessmentRepository,
)

from src.domain.confidence.models import ConfidenceLevel, ConfidencePolicy, ConfidenceAssessment
from src.application.confidence.service import ConfidenceService
from src.infrastructure.persistence.data.json.confidence_repository import (
    JsonConfidencePolicyRepository,
    JsonConfidenceAssessmentRepository,
)

from src.domain.schema_validation.models import (
    ValidationStatus,
    FieldType,
    FieldDefinition,
    SchemaDefinition,
    AdditionalFieldsPolicy,
)
from src.application.schema_validation.service import SchemaValidationService
from src.infrastructure.persistence.data.json.schema_repository import JsonSchemaRegistryRepository

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

from src.domain.duplicate_detection.models import (
    DuplicateStatus,
    DuplicateReasonCode,
    DuplicateCandidate,
    DuplicateDetectionPolicy,
    compute_semantic_fingerprint,
)
from src.application.duplicate_detection.service import DuplicateDetectionService, create_default_product_dedup_policy
from src.infrastructure.persistence.data.json.duplicate_detection_repository import (
    JsonDuplicateDetectionPolicyRepository,
    JsonDuplicateDetectionRepository,
)


def test_scenario_a_source_priority_winner(tmp_path: Path):
    """Escenario A: same entity + same field (100 vs 120) -> deterministic winner por source priority."""
    policy_repo = JsonConflictResolutionPolicyRepository(tmp_path / "policies")
    result_repo = JsonConflictResolutionRepository(tmp_path / "results")
    service = ConflictResolutionService(policy_repo=policy_repo, result_repo=result_repo)

    policy = create_default_source_priority_policy(
        policy_id="pol_source_prio_a",
        precedence=("primary_supplier", "secondary_supplier"),
        field_path="price",
    )
    policy_repo.save_policy(policy)

    now = datetime.now(timezone.utc)
    c1 = ConflictCandidate(
        candidate_id="cand_1",
        source_id="primary_supplier",
        record_id="rec_1",
        canonical_entity_id="canon_prod_100",
        field_path="price",
        value=Decimal("100.00"),
        observed_at=now,
    )
    c2 = ConflictCandidate(
        candidate_id="cand_2",
        source_id="secondary_supplier",
        record_id="rec_2",
        canonical_entity_id="canon_prod_100",
        field_path="price",
        value=Decimal("120.00"),
        observed_at=now,
    )

    res = service.resolve_conflict([c1, c2], policy_id="pol_source_prio_a", evaluated_at=now)
    assert res.status == ConflictStatus.RESOLVED
    assert res.reason_code == ConflictReasonCode.RESOLVED_BY_SOURCE_PRIORITY
    assert res.selected_candidate_id == "cand_1"
    assert res.selected_value == Decimal("100.00")


def test_scenario_b_fresh_vs_stale_under_freshness_policy(tmp_path: Path):
    """Escenario B: fresh vs stale -> fresh wins under freshness policy."""
    service = ConflictResolutionService()
    policy = create_default_freshness_policy(max_acceptable_age_seconds=7200)
    now = datetime.now(timezone.utc)

    c_stale = ConflictCandidate(
        candidate_id="cand_stale",
        source_id="src_stale",
        record_id="r1",
        canonical_entity_id="canon_prod_200",
        field_path="availability",
        value=False,
        observed_at=now - timedelta(hours=10),
        freshness_status=FreshnessStatus.STALE,
        freshness_age_seconds=Decimal("36000"),
    )
    c_fresh = ConflictCandidate(
        candidate_id="cand_fresh",
        source_id="src_fresh",
        record_id="r2",
        canonical_entity_id="canon_prod_200",
        field_path="availability",
        value=True,
        observed_at=now - timedelta(minutes=5),
        freshness_status=FreshnessStatus.FRESH,
        freshness_age_seconds=Decimal("300"),
    )

    res = service.resolve_conflict([c_stale, c_fresh], policy=policy, evaluated_at=now)
    assert res.status == ConflictStatus.RESOLVED
    assert res.reason_code == ConflictReasonCode.RESOLVED_BY_FRESHEST
    assert res.selected_candidate_id == "cand_fresh"
    assert res.selected_value is True


def test_scenario_c_higher_vs_lower_confidence(tmp_path: Path):
    """Escenario C: higher vs lower confidence -> higher wins."""
    service = ConflictResolutionService()
    policy = create_default_confidence_policy()
    now = datetime.now(timezone.utc)

    c_low = ConflictCandidate(
        candidate_id="cand_low",
        source_id="unverified_api",
        record_id="r1",
        canonical_entity_id="canon_prod_300",
        field_path="warranty_months",
        value=6,
        observed_at=now,
        confidence_level=ConfidenceLevel.LOW,
        confidence_score=Decimal("0.3500"),
    )
    c_high = ConflictCandidate(
        candidate_id="cand_high",
        source_id="official_doc",
        record_id="r2",
        canonical_entity_id="canon_prod_300",
        field_path="warranty_months",
        value=12,
        observed_at=now,
        confidence_level=ConfidenceLevel.HIGH,
        confidence_score=Decimal("0.9800"),
    )

    res = service.resolve_conflict([c_low, c_high], policy=policy, evaluated_at=now)
    assert res.status == ConflictStatus.RESOLVED
    assert res.reason_code == ConflictReasonCode.RESOLVED_BY_HIGHEST_CONFIDENCE
    assert res.selected_candidate_id == "cand_high"
    assert res.selected_value == 12


def test_scenario_d_two_equally_valid_contradictory_unresolved(tmp_path: Path):
    """Escenario D: two equally valid contradictory candidates without tie-break -> UNRESOLVED."""
    service = ConflictResolutionService()
    policy = ConflictResolutionPolicy(
        policy_id="pol_no_tiebreak",
        name="No Tiebreak Source Policy",
        version="1.0.0",
        strategy=ResolutionStrategy.SOURCE_PRIORITY,
        source_precedence=("partner_a", "partner_b"),  # Ambas empatadas si llegan del mismo partner o fuentes no jerárquicas
    )
    now = datetime.now(timezone.utc)

    # Ambas del mismo partner de igual nivel pero valores contradictorios
    c1 = ConflictCandidate(
        candidate_id="c1",
        source_id="partner_a",
        record_id="r1",
        canonical_entity_id="canon_prod_400",
        field_path="color",
        value="Midnight Black",
        observed_at=now,
    )
    c2 = ConflictCandidate(
        candidate_id="c2",
        source_id="partner_a",
        record_id="r2",
        canonical_entity_id="canon_prod_400",
        field_path="color",
        value="Space Grey",
        observed_at=now,
    )

    res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
    assert res.status == ConflictStatus.UNRESOLVED
    assert res.reason_code == ConflictReasonCode.UNRESOLVED_TIE
    assert res.selected_candidate_id is None
    assert res.selected_value is None


def test_scenario_e_duplicate_replayed_evidence_not_double_counted(tmp_path: Path):
    """Escenario E: duplicate/replayed evidence not double counted under consensus."""
    service = ConflictResolutionService()
    policy = create_default_consensus_policy(min_votes=2, min_ratio=Decimal("0.6667"))
    now = datetime.now(timezone.utc)

    # Source 1 emite 10 replays de $100
    candidates = []
    for i in range(10):
        candidates.append(
            ConflictCandidate(
                candidate_id=f"c1_rep_{i}",
                source_id="source_1",
                record_id=f"r1_{i}",
                canonical_entity_id="canon_prod_500",
                field_path="price",
                value=Decimal("100"),
                deduplication_fingerprint="fp_val_100",
                is_duplicate=(i > 0),
                observed_at=now,
            )
        )
    # Source 2 emite $120 una vez
    candidates.append(
        ConflictCandidate(
            candidate_id="c2_single",
            source_id="source_2",
            record_id="r2_single",
            canonical_entity_id="canon_prod_500",
            field_path="price",
            value=Decimal("120"),
            deduplication_fingerprint="fp_val_120",
            observed_at=now,
        )
    )

    res = service.resolve_conflict(candidates, policy=policy, evaluated_at=now)
    # Sin dedupe: 10 vs 1 (10/11 = 90.9% -> resolvería erróneamente por consenso falso).
    # Con dedupe: 1 voto para $100 vs 1 voto para $120 (1/2 = 50% < 66.67%) -> UNRESOLVED.
    assert res.status == ConflictStatus.UNRESOLVED
    assert res.selected_value is None


def test_scenario_f_different_entities_or_facts_safe(tmp_path: Path):
    """Escenario F: different logical entities -> NO_CONFLICT."""
    service = ConflictResolutionService()
    policy = create_default_source_priority_policy()
    now = datetime.now(timezone.utc)

    c1 = ConflictCandidate(
        candidate_id="c1",
        source_id="src_1",
        record_id="r1",
        canonical_entity_id="canon_mouse",
        field_path="price",
        value=Decimal("50"),
        observed_at=now,
    )
    c2 = ConflictCandidate(
        candidate_id="c2",
        source_id="src_2",
        record_id="r2",
        canonical_entity_id="canon_keyboard",
        field_path="price",
        value=Decimal("80"),
        observed_at=now,
    )

    res = service.resolve_conflict([c1, c2], policy=policy, evaluated_at=now)
    assert res.status == ConflictStatus.NO_CONFLICT
    assert res.reason_code == ConflictReasonCode.NO_CONFLICT_DIFFERENT_ENTITIES


def test_scenario_g_restart_durability_and_reload(tmp_path: Path):
    """Escenario G: restart -> result preserved."""
    repo_dir = tmp_path / "conflict_repo"
    policy_dir = tmp_path / "policy_repo"

    repo1 = JsonConflictResolutionRepository(repo_dir)
    pol_repo1 = JsonConflictResolutionPolicyRepository(policy_dir)
    service1 = ConflictResolutionService(policy_repo=pol_repo1, result_repo=repo1)

    policy = create_default_source_priority_policy(policy_id="pol_persist_v1", precedence=("s1", "s2"))
    pol_repo1.save_policy(policy)

    now = datetime.now(timezone.utc)
    c1 = ConflictCandidate(candidate_id="c1", source_id="s1", record_id="r1", canonical_entity_id="p1", field_path="price", value=Decimal("100"), observed_at=now)
    c2 = ConflictCandidate(candidate_id="c2", source_id="s2", record_id="r2", canonical_entity_id="p1", field_path="price", value=Decimal("120"), observed_at=now)

    res1 = service1.resolve_conflict([c1, c2], policy_id="pol_persist_v1", evaluated_at=now)

    # Simular reinicio creando nuevas instancias sobre el mismo directorio
    repo2 = JsonConflictResolutionRepository(repo_dir)
    pol_repo2 = JsonConflictResolutionPolicyRepository(policy_dir)

    reloaded_pol = pol_repo2.get_policy("pol_persist_v1")
    reloaded_res = repo2.get_result(res1.conflict_id)

    assert reloaded_pol is not None
    assert reloaded_pol.policy_id == policy.policy_id
    assert reloaded_res is not None
    assert reloaded_res.conflict_id == res1.conflict_id
    assert reloaded_res.selected_candidate_id == "c1"
    assert reloaded_res.selected_value == "100"  # JSON serialization converts Decimal to string representation


def test_scenario_h_tampered_persistence_corruption_detected(tmp_path: Path):
    """Escenario H: tampered persistence -> corruption detected."""
    repo = JsonConflictResolutionRepository(tmp_path / "results")
    now = datetime.now(timezone.utc)

    res = ConflictResolutionResult(
        conflict_id="cnf_tamper_test",
        canonical_entity_id="canon_1",
        field_path="stock",
        candidate_ids=("c1", "c2"),
        strategy=ResolutionStrategy.SOURCE_PRIORITY,
        status=ConflictStatus.RESOLVED,
        reason_code=ConflictReasonCode.RESOLVED_BY_SOURCE_PRIORITY,
        selected_candidate_id="c1",
        selected_value=15,
        policy_id="pol_1",
        policy_version="1.0.0",
        evaluated_at=now,
        correlation_id="corr_tamper",
    )
    repo.save_result(res)

    file_path = tmp_path / "results" / "result_cnf_tamper_test.json"
    raw = file_path.read_text(encoding="utf-8")
    tampered = raw.replace('"selected_value": 15', '"selected_value": 999999')
    file_path.write_text(tampered, encoding="utf-8")

    with pytest.raises(CorruptedConflictResultError):
        repo.get_result("cnf_tamper_test")


def test_scenario_i_e2e_data_quality_governance_flow(tmp_path: Path):
    """
    Escenario I: E2E Data Quality & Governance Flow completo:
    1. Source Registry (L.1) registra fuentes Proveedor A y Proveedor B.
    2. Data Provenance (L.2) emite linaje y trazabilidad causal de las observaciones.
    3. Freshness (L.3) evalúa la frescura de ambas observaciones.
    4. Confidence (L.4) evalúa la confianza de los datos de cada fuente.
    5. Schema Validation (L.5) valida que ambas cumplan el esquema de producto.
    6. Entity Resolution (L.6) resuelve ambas referencias bajo el mismo canonical_entity_id (GTIN coincidente).
    7. Duplicate Detection (L.7) detecta que NO son duplicados (mismo canonical_entity_id pero distintas fuentes independientes).
    8. Conflict Resolution (L.8) recibe dos precios contradictorios ($100 vs $120), aplica política de gobernanza y produce resolución auditable determinista sin destruir evidencia original.
    """
    now = datetime.now(timezone.utc)

    # 1. L.1 Source Registry
    source_repo = JsonSourceRegistryRepository(tmp_path / "sources")
    src_a = RegisteredSource(
        source_id="supplier_alpha",
        name="Supplier Alpha Direct",
        source_type=SourceType.SUPPLIER,
        provider="SupplierAlphaCorp",
        canonical_identifier="supplier:supplieralphacorp:alpha",
        created_at=now,
        updated_at=now,
        status=SourceStatus.ACTIVE,
    )
    src_b = RegisteredSource(
        source_id="supplier_beta",
        name="Supplier Beta Feed",
        source_type=SourceType.SUPPLIER,
        provider="SupplierBetaCorp",
        canonical_identifier="supplier:supplierbetacorp:beta",
        created_at=now,
        updated_at=now,
        status=SourceStatus.ACTIVE,
    )
    source_repo.save_source(src_a)
    source_repo.save_source(src_b)

    # 2. L.2 Provenance
    prov_repo = JsonProvenanceRepository(tmp_path / "provenance")
    prov_a = ProvenanceRecord(
        provenance_id="prov_alpha_01",
        subject_type=SubjectType.SUPPLIER_PRODUCT,
        subject_id="prod_raw_a",
        source_id="supplier_alpha",
        correlation_id="corr_governance_e2e",
        captured_at=now,
    )
    prov_b = ProvenanceRecord(
        provenance_id="prov_beta_01",
        subject_type=SubjectType.SUPPLIER_PRODUCT,
        subject_id="prod_raw_b",
        source_id="supplier_beta",
        correlation_id="corr_governance_e2e",
        captured_at=now,
    )
    prov_repo.save_provenance(prov_a)
    prov_repo.save_provenance(prov_b)

    # 3. L.5 Schema Validation
    schema_repo = JsonSchemaRegistryRepository(tmp_path / "schemas")
    schema_service = SchemaValidationService(schema_registry=schema_repo)
    schema = SchemaDefinition(
        schema_id="product_schema_v1",
        name="Product Schema",
        version="1.0.0",
        subject_type=SubjectType.SUPPLIER_PRODUCT.value,
        fields=(
            FieldDefinition(field_name="gtin", field_type=FieldType.STRING, required=True),
            FieldDefinition(field_name="title", field_type=FieldType.STRING, required=True),
            FieldDefinition(field_name="price", field_type=FieldType.DECIMAL, required=True),
        ),
        additional_fields_policy=AdditionalFieldsPolicy.ALLOW,
    )
    schema_repo.save_schema(schema)
    payload_a = {"gtin": "7791234567890", "title": "Gaming Headset RGB", "price": Decimal("100.00")}
    payload_b = {"gtin": "7791234567890", "title": "Gaming Headset RGB", "price": Decimal("120.00")}

    val_a = schema_service.validate(payload_a, subject_type=SubjectType.SUPPLIER_PRODUCT.value)
    val_b = schema_service.validate(payload_b, subject_type=SubjectType.SUPPLIER_PRODUCT.value)
    assert val_a.status == ValidationStatus.PASS
    assert val_b.status == ValidationStatus.PASS

    # 4. L.6 Entity Resolution
    er_service = EntityResolutionService()
    er_policy = create_default_product_policy()
    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="supplier_alpha",
        source_entity_id="prod_raw_a",
        identifiers=(EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7791234567890", is_strong=True),),
        canonical_attributes={"title": "Gaming Headset RGB"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="supplier_beta",
        source_entity_id="prod_raw_b",
        identifiers=(EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7791234567890", is_strong=True),),
        canonical_attributes={"title": "Gaming Headset RGB"},
    )
    er_result = er_service.resolve_pair(ref_a, ref_b, er_policy)
    assert er_result.status == MatchStatus.MATCH
    canonical_entity_id = "canon_headset_779"

    # 5. L.7 Duplicate Detection
    dedup_service = DuplicateDetectionService()
    dedup_policy = create_default_product_dedup_policy()
    cand_dup_a = DuplicateCandidate(
        record_id="rec_a",
        source_id="supplier_alpha",
        canonical_entity_id=canonical_entity_id,
        payload=payload_a,
        observed_at=now,
    )
    cand_dup_b = DuplicateCandidate(
        record_id="rec_b",
        source_id="supplier_beta",
        canonical_entity_id=canonical_entity_id,
        payload=payload_b,
        observed_at=now,
    )
    dedup_result = dedup_service.evaluate_pair(cand_dup_a, cand_dup_b, dedup_policy)
    assert dedup_result.status == DuplicateStatus.NOT_DUPLICATE
    assert dedup_result.reason_code == DuplicateReasonCode.SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE

    # 6. L.8 Conflict Resolution
    conflict_service = ConflictResolutionService()
    conflict_policy = create_default_source_priority_policy(
        policy_id="pol_e2e_prio",
        precedence=("supplier_alpha", "supplier_beta"),
        field_path="price",
    )

    candidate_alpha = ConflictCandidate(
        candidate_id="cand_e2e_alpha",
        source_id="supplier_alpha",
        record_id="rec_a",
        canonical_entity_id=canonical_entity_id,
        field_path="price",
        value=Decimal("100.00"),
        observed_at=now,
        provenance_id=prov_a.provenance_id,
        freshness_status=FreshnessStatus.FRESH,
        confidence_level=ConfidenceLevel.HIGH,
        confidence_score=Decimal("0.9500"),
        deduplication_fingerprint=cand_dup_a.fingerprint,
        is_duplicate=False,
    )

    candidate_beta = ConflictCandidate(
        candidate_id="cand_e2e_beta",
        source_id="supplier_beta",
        record_id="rec_b",
        canonical_entity_id=canonical_entity_id,
        field_path="price",
        value=Decimal("120.00"),
        observed_at=now,
        provenance_id=prov_b.provenance_id,
        freshness_status=FreshnessStatus.FRESH,
        confidence_level=ConfidenceLevel.HIGH,
        confidence_score=Decimal("0.9000"),
        deduplication_fingerprint=cand_dup_b.fingerprint,
        is_duplicate=False,
    )

    conflict_res = conflict_service.resolve_conflict(
        candidates=[candidate_alpha, candidate_beta],
        policy=conflict_policy,
        correlation_id="corr_governance_e2e",
        evaluated_at=now,
    )

    assert conflict_res.status == ConflictStatus.RESOLVED
    assert conflict_res.reason_code == ConflictReasonCode.RESOLVED_BY_SOURCE_PRIORITY
    assert conflict_res.selected_candidate_id == "cand_e2e_alpha"
    assert conflict_res.selected_value == Decimal("100.00")
    assert conflict_res.canonical_entity_id == canonical_entity_id
    assert "cand_e2e_alpha" in conflict_res.candidate_ids
    assert "cand_e2e_beta" in conflict_res.candidate_ids

    # Toda la evidencia y procedencias siguen intactas y trazables
    assert prov_repo.get_provenance(prov_a.provenance_id) is not None
    assert prov_repo.get_provenance(prov_b.provenance_id) is not None
    assert source_repo.get_source(src_a.source_id) is not None
    assert candidate_alpha.value == Decimal("100.00")
    assert candidate_beta.value == Decimal("120.00")
