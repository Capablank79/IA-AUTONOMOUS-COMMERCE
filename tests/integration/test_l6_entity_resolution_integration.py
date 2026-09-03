"""
Tests de integración y E2E para Entity Resolution L.6 (Transversal Data Quality / Governance).

Escenarios cubiertos:
A. Resolución de entidades exacta por GTIN (Cross-Source Supplier ↔ Marketplace) -> MATCH indiscutible con canonical_id determinista.
B. Conflicto de GTIN/Strong Identifiers con título idéntico -> NO_MATCH forzado.
C. Matching por SKU en mismo namespace gobernado por política explícita.
D. Matching por SKU en distinto namespace -> NO MATCH automático.
E. Matching ponderado por atributos canónicos con score Decimal exacto.
F. Ambigüedad de candidatos y preservación de status UNKNOWN / POSSIBLE_MATCH sin auto-fusión.
G. Replay determinista e idempotente del servicio y repositorio ante llamadas repetidas con timestamps diferentes.
H. Persistencia duradera, recarga post-restart y detección de manipulación física/tampering (checksum recompute -> compare).
I. Detección de colisiones/conflictos semánticos bajo el mismo ID de resolución o política.
J. Sanitización de secretos K.8 en metadatos y trazabilidad canónica.
K. E2E Data Quality Flow: Source Registry (L.1) -> Data Provenance (L.2) -> Schema Validation (L.5) -> Freshness (L.3) -> Confidence (L.4) -> Entity Resolution (L.6).
   Demuestra que dos referencias válidas, validadas por esquema y con procedencia se resuelven a una entidad canónica determinista.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from pathlib import Path

# L.6
from src.domain.entity_resolution.models import (
    EntityType,
    IdentifierType,
    MatchStatus,
    ResolutionReasonCode,
    EntityIdentifier,
    EntityReference,
    EntityResolutionPolicy,
    EntityResolutionResult,
    ResolvedEntity,
    compute_resolution_policy_checksum,
    compute_resolution_result_checksum,
    compute_resolution_input_fingerprint,
    compute_resolved_entity_checksum,
)
from src.application.entity_resolution.service import (
    EntityResolutionService,
    create_default_product_policy,
)
from src.infrastructure.persistence.data.json.entity_resolution_repository import (
    JsonEntityResolutionPolicyRepository,
    JsonEntityResolutionRepository,
    EntityResolutionConflictError,
    EntityResolutionPolicyConflictError,
    CorruptedResolutionPolicyError,
    CorruptedResolutionResultError,
    CorruptedCanonicalEntityError,
)

# Integración con L.1 - L.5
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
from src.domain.freshness.models import FreshnessPolicy, FreshnessStatus
from src.infrastructure.persistence.data.json.freshness_repository import (
    JsonFreshnessPolicyRepository,
    JsonFreshnessAssessmentRepository,
)
from src.application.freshness.service import FreshnessService
from src.domain.confidence.models import ConfidencePolicy, ConfidenceLevel
from src.infrastructure.persistence.data.json.confidence_repository import (
    JsonConfidencePolicyRepository,
    JsonConfidenceAssessmentRepository,
)
from src.application.confidence.service import ConfidenceService
from src.domain.reliability.ports import ClockPort


class FrozenClock(ClockPort):
    def __init__(self, fixed_now: datetime):
        self._now = fixed_now

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        pass


def test_scenario_a_exact_gtin_cross_source_match(tmp_path):
    """Escenario A: Resolución exacta de entidades entre mayorista y marketplace por GTIN."""
    policy_repo = JsonEntityResolutionPolicyRepository(tmp_path / "policies")
    res_repo = JsonEntityResolutionRepository(tmp_path / "resolutions")
    policy = create_default_product_policy()
    policy_repo.save_policy(policy)

    service = EntityResolutionService(policy_repository=policy_repo, repository=res_repo)

    supplier_ref = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="supplier_syscom",
        source_entity_id="SYS-10492",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="01234567890123", is_strong=True),
        ),
        canonical_attributes={"brand": "Ubiquiti", "model": "U6-Pro", "name": "Access Point UniFi U6 Pro"},
    )

    marketplace_ref = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="mercadolibre_cl",
        source_entity_id="MLC-99887766",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value=" 0-1234567890123 ", is_strong=True),
        ),
        canonical_attributes={"title": "Ubiquiti UniFi 6 Pro Access Point Wi-Fi 6"},
    )

    res = service.resolve_pair(supplier_ref, marketplace_ref, policy=policy)
    assert res.status == MatchStatus.MATCH
    assert res.confidence_score == Decimal("1.0000")
    assert res.canonical_entity_id.startswith("canonical_product_gtin_01234567890123")
    assert "GTIN:01234567890123" in res.matched_identifiers

    # Persistencia y recuperación
    saved = res_repo.save_resolution(res)
    loaded = res_repo.get_resolution(res.resolution_id)
    assert loaded is not None
    assert loaded.resolution_id == res.resolution_id
    assert loaded.canonical_entity_id == res.canonical_entity_id


def test_scenario_b_gtin_conflict_overrides_title(tmp_path):
    """Escenario B: GTIN en conflicto fuerza NO_MATCH aun cuando los atributos son idénticos."""
    service = EntityResolutionService()
    policy = create_default_product_policy()

    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="supplier_1",
        source_entity_id="p1",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="1111111111111", is_strong=True),
        ),
        canonical_attributes={"brand": "Apple", "model": "iPhone 15 128GB Black"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="marketplace_1",
        source_entity_id="p2",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="2222222222222", is_strong=True),
        ),
        canonical_attributes={"brand": "Apple", "model": "iPhone 15 128GB Black"},
    )

    res = service.resolve_pair(ref_a, ref_b, policy=policy)
    assert res.status == MatchStatus.NO_MATCH
    assert res.confidence_score == Decimal("0.0000")
    assert ResolutionReasonCode.CONTRADICTORY_STRONG_IDENTIFIERS.value in res.reason_codes


def test_scenario_c_sku_governed_by_policy(tmp_path):
    """Escenario C: Matching de SKU dentro del mismo namespace gobernado por política."""
    service = EntityResolutionService()

    # Política explícita con SKU strong
    sku_strong_policy = EntityResolutionPolicy(
        policy_id="policy_sku_strong",
        name="SKU Strong Policy",
        version="1.0.0",
        entity_type=EntityType.PRODUCT,
        strong_identifier_types=(IdentifierType.SKU, IdentifierType.GTIN),
    )

    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="internal_erp",
        source_entity_id="ERP-001",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.SKU, value="SKU-LOGI-01", namespace="internal_erp", is_strong=True),
        ),
        canonical_attributes={"brand": "Logitech"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="internal_erp",
        source_entity_id="ERP-002",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.SKU, value="SKU-LOGI-01", namespace="internal_erp", is_strong=True),
        ),
        canonical_attributes={"brand": "Logitech"},
    )

    res = service.resolve_pair(ref_a, ref_b, policy=sku_strong_policy)
    assert res.status == MatchStatus.MATCH
    assert res.confidence_score == Decimal("1.0000")


def test_scenario_d_sku_cross_namespace_no_match(tmp_path):
    """Escenario D: Mismo SKU pero en namespaces distintos no produce MATCH automático."""
    service = EntityResolutionService()
    sku_policy = EntityResolutionPolicy(
        policy_id="policy_sku",
        name="SKU Policy",
        version="1.0.0",
        entity_type=EntityType.PRODUCT,
        strong_identifier_types=(IdentifierType.SKU,),
        allow_cross_source_sku_match=False,
    )

    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="distributor_alpha",
        source_entity_id="a1",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.SKU, value="PROD-99", namespace="distributor_alpha", is_strong=True),
        ),
        canonical_attributes={"brand": "BrandAlpha"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="distributor_beta",
        source_entity_id="b1",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.SKU, value="PROD-99", namespace="distributor_beta", is_strong=True),
        ),
        canonical_attributes={"brand": "BrandBeta"},
    )

    res = service.resolve_pair(ref_a, ref_b, policy=sku_policy)
    assert res.status != MatchStatus.MATCH
    assert ResolutionReasonCode.SCOPED_IDENTIFIER_CROSS_NAMESPACE.value in res.reason_codes


def test_scenario_e_weighted_attribute_matching(tmp_path):
    """Escenario E: Emparejamiento por ponderación estricta de atributos canónicos."""
    service = EntityResolutionService()
    policy = EntityResolutionPolicy(
        policy_id="policy_attr_match",
        name="Attribute Match Policy",
        version="1.0.0",
        entity_type=EntityType.PRODUCT,
        required_attributes=("brand", "model"),
        optional_attributes=("color",),
        attribute_weights={
            "brand": Decimal("0.40"),
            "model": Decimal("0.40"),
            "color": Decimal("0.20"),
        },
        match_threshold=Decimal("0.80"),
        possible_match_threshold=Decimal("0.50"),
        allow_attribute_only_auto_match=True,
    )

    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_1",
        source_entity_id="item_1",
        canonical_attributes={"brand": "Sony", "model": "WH-1000XM5", "color": "Black"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_2",
        source_entity_id="item_2",
        canonical_attributes={"brand": "sony", "model": "WH-1000XM5", "color": "Silver"},
    )

    res = service.resolve_pair(ref_a, ref_b, policy=policy)
    # brand (0.4) + model (0.4) = 0.80 -> alcanza match_threshold
    assert res.status == MatchStatus.MATCH
    assert res.confidence_score == Decimal("0.8000")


def test_scenario_f_candidate_ambiguity_no_auto_merge(tmp_path):
    """Escenario F: Ambigüedad en múltiples candidatos previene auto-fusión indebida."""
    service = EntityResolutionService()
    policy = create_default_product_policy()

    target = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="catalog",
        source_entity_id="target_01",
        canonical_attributes={"brand": "Dell", "model": "XPS 15"},
    )
    candidate_1 = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="vendor_1",
        source_entity_id="cand_01",
        canonical_attributes={"brand": "Dell", "model": "XPS 15", "ram": "16GB"},
    )
    candidate_2 = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="vendor_2",
        source_entity_id="cand_02",
        canonical_attributes={"brand": "Dell", "model": "XPS 15", "ram": "32GB"},
    )

    results = service.resolve_candidates(target, [candidate_1, candidate_2], policy=policy)
    assert len(results) == 2
    for r in results:
        assert r.status != MatchStatus.MATCH
        assert r.status in (MatchStatus.POSSIBLE_MATCH, MatchStatus.UNKNOWN)


def test_scenario_g_replay_idempotence_different_clocks(tmp_path):
    """Escenario G: Replay determinista e idempotente ante llamadas repetidas con tiempos de reloj distintos."""
    repo = JsonEntityResolutionRepository(tmp_path / "resolutions")
    service = EntityResolutionService(repository=repo)
    policy = create_default_product_policy()

    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_a",
        source_entity_id="p100",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7790001112223", is_strong=True),
        ),
        canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_b",
        source_entity_id="p200",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7790001112223", is_strong=True),
        ),
        canonical_attributes={"brand": "Logitech", "model": "MX Master 3S"},
    )

    # Primera resolución
    res_1 = service.resolve_pair(ref_a, ref_b, policy=policy)
    saved_1 = repo.save_resolution(res_1)

    # Segunda resolución en instante posterior
    res_2 = service.resolve_pair(ref_a, ref_b, policy=policy)
    saved_2 = repo.save_resolution(res_2)

    assert saved_1.resolution_id == saved_2.resolution_id
    assert saved_1.input_fingerprint == saved_2.input_fingerprint
    assert saved_1.canonical_entity_id == saved_2.canonical_entity_id
    assert saved_1.checksum == saved_2.checksum


def test_scenario_h_durability_and_tamper_detection(tmp_path):
    """Escenario H: Durabilidad post-restart y detección estricta de manipulación de archivos."""
    repo_dir = tmp_path / "resolution_store"
    repo = JsonEntityResolutionRepository(repo_dir)
    policy = create_default_product_policy()

    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_a",
        source_entity_id="p1",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="9998887776665", is_strong=True),
        ),
        canonical_attributes={"brand": "Samsung"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_b",
        source_entity_id="p2",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="9998887776665", is_strong=True),
        ),
        canonical_attributes={"brand": "Samsung"},
    )

    service = EntityResolutionService(repository=repo)
    res = service.resolve_pair(ref_a, ref_b, policy=policy)
    repo.save_resolution(res)

    # Reinicio (nueva instancia sobre el mismo directorio)
    repo_restart = JsonEntityResolutionRepository(repo_dir)
    loaded = repo_restart.get_resolution(res.resolution_id)
    assert loaded is not None
    assert loaded.canonical_entity_id == res.canonical_entity_id

    # Manipulación deliberada del archivo
    file_path = repo_restart._get_resolution_path(res.resolution_id)
    content = file_path.read_text(encoding="utf-8")
    tampered_content = content.replace('"MATCH"', '"NO_MATCH"')
    file_path.write_text(tampered_content, encoding="utf-8")

    with pytest.raises(CorruptedResolutionResultError):
        repo_restart.get_resolution(res.resolution_id)


def test_scenario_i_conflict_detection_on_mutated_payload(tmp_path):
    """Escenario I: Detección de conflicto ante mutación de payload bajo el mismo resolution_id."""
    repo = JsonEntityResolutionRepository(tmp_path)
    policy = create_default_product_policy()

    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_a",
        source_entity_id="p1",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="1234567890123", is_strong=True),
        ),
        canonical_attributes={"brand": "Sony"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_b",
        source_entity_id="p2",
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value="1234567890123", is_strong=True),
        ),
        canonical_attributes={"brand": "Sony"},
    )

    service = EntityResolutionService(repository=repo)
    res_1 = service.resolve_pair(ref_a, ref_b, policy=policy)
    repo.save_resolution(res_1)

    # Crear una resolución con el mismo resolution_id pero diferente canonical_entity_id
    mutated_res = EntityResolutionResult(
        resolution_id=res_1.resolution_id,
        entity_type=res_1.entity_type,
        status=MatchStatus.NO_MATCH,
        canonical_entity_id="canonical_product_different",
        reference_a=res_1.reference_a,
        reference_b=res_1.reference_b,
        confidence_score=Decimal("0.0000"),
        policy_id=res_1.policy_id,
        policy_version=res_1.policy_version,
    )

    with pytest.raises(EntityResolutionConflictError):
        repo.save_resolution(mutated_res)


def test_scenario_j_secret_sanitization_in_metadata(tmp_path):
    """Escenario J: Sanitización de secretos K.8 en metadatos de referencia."""
    ref_a = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_a",
        source_entity_id="p1",
        canonical_attributes={"brand": "TestBrand"},
        metadata={"api_key": "secret_token_12345", "safe_note": "integration_test"},
    )
    ref_b = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_b",
        source_entity_id="p2",
        canonical_attributes={"brand": "TestBrand"},
        metadata={"client_secret": "pass_xyz", "safe_note": "integration_test"},
    )

    assert "secret_token_12345" not in str(ref_a.metadata)
    assert ref_a.metadata.get("api_key") == "[REDACTED]"
    assert ref_a.metadata.get("safe_note") == "integration_test"
    assert ref_b.metadata.get("client_secret") == "[REDACTED]"


def test_scenario_k_e2e_data_quality_governance_flow(tmp_path):
    """Escenario K: Flujo E2E completo L.1 -> L.2 -> L.5 -> L.3 -> L.4 -> L.6."""
    clock = FrozenClock(datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc))

    # 1. L.1 Source Registry
    source_repo = JsonSourceRegistryRepository(base_dir=tmp_path / "sources")
    src_supplier = RegisteredSource(
        source_id="syscom_chile",
        name="Syscom Chile Official API",
        source_type=SourceType.SUPPLIER,
        provider="syscom",
        canonical_identifier="supplier:syscom:cl",
        status=SourceStatus.ACTIVE,
        endpoint_reference="https://api.syscom.cl/v1",
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    src_marketplace = RegisteredSource(
        source_id="mercadolibre_cl",
        name="MercadoLibre Chile Public Search",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:cl",
        status=SourceStatus.ACTIVE,
        endpoint_reference="https://api.mercadolibre.com",
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    source_repo.save_source(src_supplier)
    source_repo.save_source(src_marketplace)

    # 2. L.2 Data Provenance
    prov_repo = JsonProvenanceRepository(tmp_path / "provenance")
    prov_sup = ProvenanceRecord(
        provenance_id="prov_sup_101",
        subject_id="SYS-U6-PRO",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        source_id=src_supplier.source_id,
        captured_at=clock.now() - timedelta(hours=2),
    )
    prov_mkt = ProvenanceRecord(
        provenance_id="prov_mkt_202",
        subject_id="MLC-998811",
        subject_type=SubjectType.MARKET_OBSERVATION,
        source_id=src_marketplace.source_id,
        captured_at=clock.now() - timedelta(hours=1),
    )
    prov_repo.save_provenance(prov_sup)
    prov_repo.save_provenance(prov_mkt)

    # 3. L.5 Schema Validation
    schema_repo = JsonSchemaRegistryRepository(tmp_path / "schemas")
    product_schema = SchemaDefinition(
        schema_id="canonical_product_schema",
        name="Canonical Product Payload",
        version="1.0.0",
        subject_type="PRODUCT",
        additional_fields_policy=AdditionalFieldsPolicy.ALLOW,
        fields=(
            FieldDefinition(field_name="gtin", field_type=FieldType.STRING),
            FieldDefinition(field_name="brand", field_type=FieldType.STRING),
            FieldDefinition(field_name="model", field_type=FieldType.STRING),
        ),
    )
    schema_repo.save_schema(product_schema)
    schema_service = SchemaValidationService(schema_registry=schema_repo)

    payload_sup = {"gtin": "0810010078443", "brand": "Ubiquiti", "model": "U6-Pro"}
    payload_mkt = {"gtin": "0810010078443", "brand": "Ubiquiti Networks", "model": "U6-Pro"}

    val_sup = schema_service.validate(payload=payload_sup, subject_type="PRODUCT")
    val_mkt = schema_service.validate(payload=payload_mkt, subject_type="PRODUCT")
    assert val_sup.status == ValidationStatus.PASS
    assert val_mkt.status == ValidationStatus.PASS

    # 4. L.3 Freshness Assessment
    freshness_policy_repo = JsonFreshnessPolicyRepository(tmp_path / "freshness_policies")
    freshness_assess_repo = JsonFreshnessAssessmentRepository(tmp_path / "freshness_assessments")
    fresh_policy = FreshnessPolicy(
        policy_id="fresh_prod_policy",
        name="Product Freshness Policy",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        ttl_seconds=86400.0,
        stale_threshold_seconds=172800.0,
    )
    freshness_policy_repo.save_policy(fresh_policy)
    freshness_service = FreshnessService(
        policy_repository=freshness_policy_repo,
        assessment_repository=freshness_assess_repo,
        clock=clock,
    )
    fresh_sup = freshness_service.evaluate_timestamp(
        observed_at=prov_sup.captured_at,
        subject_id=prov_sup.subject_id,
        subject_type=SubjectType.SUPPLIER_QUOTE,
        source_id=src_supplier.source_id,
        provenance_id=prov_sup.provenance_id,
    )
    assert fresh_sup.status == FreshnessStatus.FRESH

    # 5. L.4 Confidence Assessment
    conf_policy_repo = JsonConfidencePolicyRepository(tmp_path / "conf_policies")
    conf_assess_repo = JsonConfidenceAssessmentRepository(tmp_path / "conf_assessments")
    conf_policy = ConfidencePolicy(
        policy_id="conf_prod_policy",
        name="Product Confidence Policy",
        version="1.0.0",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        high_threshold=Decimal("0.80"),
        medium_threshold=Decimal("0.50"),
        weights={
            "source": Decimal("0.30"),
            "provenance": Decimal("0.30"),
            "freshness": Decimal("0.25"),
            "evidence": Decimal("0.15"),
        },
        factor_scores={
            "source_active": Decimal("1.00"),
            "source_inactive": Decimal("0.25"),
            "provenance_direct": Decimal("1.00"),
            "provenance_derived": Decimal("0.80"),
            "freshness_fresh": Decimal("1.00"),
            "freshness_stale": Decimal("0.50"),
            "freshness_expired": Decimal("0.10"),
            "evidence_present": Decimal("1.00"),
        },
        require_provenance=True,
        require_freshness=False,
    )
    conf_policy_repo.save_policy(conf_policy)
    conf_service = ConfidenceService(
        policy_repository=conf_policy_repo,
        assessment_repository=conf_assess_repo,
        source_registry=source_repo,
        provenance_repository=prov_repo,
        freshness_repository=freshness_assess_repo,
        clock=clock,
    )
    conf_sup = conf_service.assess(
        subject_id=prov_sup.subject_id,
        subject_type=SubjectType.SUPPLIER_QUOTE,
        source_id=src_supplier.source_id,
        provenance_id=prov_sup.provenance_id,
        freshness_assessment=fresh_sup,
    )
    assert conf_sup.level == ConfidenceLevel.HIGH
    assert conf_sup.score == Decimal("1.0000")
    assert conf_sup.policy_id == "conf_prod_policy"
    assert conf_sup.policy_version == "1.0.0"
    assert len(conf_sup.factors) == 4

    # 6. L.6 Entity Resolution
    er_policy_repo = JsonEntityResolutionPolicyRepository(tmp_path / "er_policies")
    er_res_repo = JsonEntityResolutionRepository(tmp_path / "er_resolutions")
    er_policy = create_default_product_policy()
    er_policy_repo.save_policy(er_policy)

    er_service = EntityResolutionService(
        policy_repository=er_policy_repo,
        repository=er_res_repo,
    )

    ref_supplier = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id=src_supplier.source_id,
        source_entity_id=prov_sup.subject_id,
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value=payload_sup["gtin"], is_strong=True),
        ),
        canonical_attributes={"brand": payload_sup["brand"], "model": payload_sup["model"]},
        provenance_id=prov_sup.provenance_id,
    )

    ref_marketplace = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id=src_marketplace.source_id,
        source_entity_id=prov_mkt.subject_id,
        identifiers=(
            EntityIdentifier(identifier_type=IdentifierType.GTIN, value=payload_mkt["gtin"], is_strong=True),
        ),
        canonical_attributes={"brand": payload_mkt["brand"], "model": payload_mkt["model"]},
        provenance_id=prov_mkt.provenance_id,
    )

    resolution_result = er_service.resolve_pair(ref_supplier, ref_marketplace, policy=er_policy)
    assert resolution_result.status == MatchStatus.MATCH
    assert resolution_result.confidence_score == Decimal("1.0000")
    assert resolution_result.canonical_entity_id == "canonical_product_gtin_0810010078443"

    # Persistir resolución canónica
    saved_res = er_res_repo.save_resolution(resolution_result)
    assert saved_res.resolution_id == resolution_result.resolution_id
    assert saved_res.canonical_entity_id == resolution_result.canonical_entity_id
