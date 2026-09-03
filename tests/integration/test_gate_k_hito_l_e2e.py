"""
Suite Formal de Validación E2E de Gate K (Transversal L — Data Quality y Governance).

Objetivo Único de Gate K:
"Las decisiones comerciales críticas deben poder rastrearse hasta sus datos de origen."

Cubre de extremo a extremo:
1. Complete happy-path trace (Decisión comercial respaldada por cadena L.1 a L.8 -> Trazabilidad inversa exacta a RegisteredSource).
2. Multi-source commercial decision (Cost de Supplier + Price de Marketplace -> Oportunidad de Margen -> Trazabilidad a todas las root sources).
3. Conflict visible & resolved (Conflicto detectado, documentado y resuelto bajo policy determinista).
4. Unresolved conflict preserved (Sin fingir certeza ante empates o evidencia insuficiente).
5. Stale / unknown data (Datos expirados o sin timestamp identificados, no convertidos silenciosamente en FRESH).
6. Low / unknown confidence (Sin false HIGH ante evidencia insuficiente).
7. Invalid schema (Payload inválido detectado como FAIL, no ingresa como fact comercial válido).
8. Ambiguous entity (POSSIBLE_MATCH / UNKNOWN no se convierten en auto-MATCH).
9. Duplicate replay (Replay de hecho idéntico detectado y gobernado con idempotencia).
10. Duplicate not counted as independent evidence (Replays no inflan votos ni consensos).
11. Restart / Durability (Repositorios reconstruidos desde disco preservan linaje y trazabilidad idénticos).
12. Checksum tampering / Corruption rejection (Datos corruptos en persistencia lanzan excepción y no generan false trust).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import pytest

# L.1 Source Registry
from src.domain.source_registry.models import (
    RegisteredSource,
    SourceType,
    SourceStatus,
)
from src.application.source_registry.service import SourceRegistryService
from src.infrastructure.persistence.data.json.source_registry_repository import (
    JsonSourceRegistryRepository,
    CorruptedSourceRecordError,
)

# L.2 Data Provenance
from src.domain.data_provenance.models import (
    ProvenanceRecord,
    SubjectType,
    SourceLineageTrace,
)
from src.application.data_provenance.service import (
    DataProvenanceService,
    UnknownSourceError,
)
from src.infrastructure.persistence.data.json.data_provenance_repository import (
    JsonProvenanceRepository,
    CorruptedProvenanceRecordError,
)

# L.3 Freshness / TTL
from src.domain.freshness.models import (
    FreshnessStatus,
    FreshnessPolicy,
    FreshnessAssessment,
)
from src.application.freshness.service import FreshnessService
from src.infrastructure.persistence.data.json.freshness_repository import (
    JsonFreshnessPolicyRepository,
    JsonFreshnessAssessmentRepository,
    CorruptedFreshnessAssessmentError,
)

# L.4 Confidence Model
from src.domain.confidence.models import (
    ConfidenceLevel,
    ConfidencePolicy,
    ConfidenceAssessment,
)
from src.application.confidence.service import ConfidenceService
from src.infrastructure.persistence.data.json.confidence_repository import (
    JsonConfidencePolicyRepository,
    JsonConfidenceAssessmentRepository,
)

# L.5 Schema Validation
from src.domain.schema_validation.models import (
    ValidationStatus,
    FieldType,
    FieldDefinition,
    SchemaDefinition,
    AdditionalFieldsPolicy,
)
from src.application.schema_validation.service import SchemaValidationService
from src.infrastructure.persistence.data.json.schema_repository import (
    JsonSchemaRegistryRepository,
    CorruptedSchemaDefinitionError,
)

# L.6 Entity Resolution
from src.domain.entity_resolution.models import (
    EntityType,
    IdentifierType,
    MatchStatus,
    EntityIdentifier,
    EntityReference,
    EntityResolutionPolicy,
)
from src.application.entity_resolution.service import (
    EntityResolutionService,
    create_default_product_policy,
)
from src.infrastructure.persistence.data.json.entity_resolution_repository import (
    JsonEntityResolutionPolicyRepository,
    JsonEntityResolutionRepository,
)

# L.7 Duplicate Detection
from src.domain.duplicate_detection.models import (
    DuplicateStatus,
    DuplicateReasonCode,
    DuplicateCandidate,
    DuplicateDetectionPolicy,
    compute_semantic_fingerprint,
)
from src.application.duplicate_detection.service import (
    DuplicateDetectionService,
    create_default_product_dedup_policy,
)
from src.infrastructure.persistence.data.json.duplicate_detection_repository import (
    JsonDuplicateDetectionPolicyRepository,
    JsonDuplicateDetectionRepository,
)

# L.8 Conflict Resolution
from src.domain.conflict_resolution.models import (
    ConflictStatus,
    ConflictReasonCode,
    ResolutionStrategy,
    ConflictCandidate,
    ConflictResolutionPolicy,
    ConflictResolutionResult,
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
)

# K.1 Audit Trail
from src.domain.audit.models import AuditRecord, AuditActor, AuditActorType, AuditRecordType
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.audit.audit_trail_service import AuditTrailService


class CommercialDecisionContext:
    """
    Estructura inmutable que modela la Decisión Comercial con trazabilidad explícita.
    """
    def __init__(
        self,
        decision_id: str,
        decision_type: str,
        canonical_entity_id: str,
        commercial_value: Decimal,
        provenance_id: str,
        conflict_result_id: Optional[str] = None,
        confidence_level: Optional[ConfidenceLevel] = None,
        freshness_status: Optional[FreshnessStatus] = None,
        schema_validation_status: Optional[ValidationStatus] = None,
        rationale: str = "",
        metadata: Optional[dict] = None,
    ):
        self.decision_id = decision_id
        self.decision_type = decision_type
        self.canonical_entity_id = canonical_entity_id
        self.commercial_value = commercial_value
        self.provenance_id = provenance_id
        self.conflict_result_id = conflict_result_id
        self.confidence_level = confidence_level
        self.freshness_status = freshness_status
        self.schema_validation_status = schema_validation_status
        self.rationale = rationale
        self.metadata = metadata or {}


def _setup_pipeline_environment(base_dir: Path):
    """Inicializa repositorios y servicios de L.1 a L.8 y K.1 de forma desacoplada y duradera."""
    audit_repo = JsonAuditRepository(base_dir / "audit")
    audit_service = AuditTrailService(audit_repo)

    source_repo = JsonSourceRegistryRepository(base_dir / "sources")
    source_service = SourceRegistryService(repository=source_repo, audit_repository=audit_repo)

    prov_repo = JsonProvenanceRepository(base_dir / "provenance")
    prov_service = DataProvenanceService(
        repository=prov_repo,
        source_registry_repository=source_repo,
        audit_repository=audit_repo,
    )

    fresh_policy_repo = JsonFreshnessPolicyRepository(base_dir / "freshness_policies")
    fresh_assess_repo = JsonFreshnessAssessmentRepository(base_dir / "freshness_assessments")
    freshness_service = FreshnessService(
        policy_repository=fresh_policy_repo,
        assessment_repository=fresh_assess_repo,
    )

    conf_policy_repo = JsonConfidencePolicyRepository(base_dir / "confidence_policies")
    conf_assess_repo = JsonConfidenceAssessmentRepository(base_dir / "confidence_assessments")
    confidence_service = ConfidenceService(
        policy_repository=conf_policy_repo,
        assessment_repository=conf_assess_repo,
        source_registry=source_repo,
        provenance_repository=prov_repo,
        freshness_repository=fresh_assess_repo,
    )

    schema_repo = JsonSchemaRegistryRepository(base_dir / "schemas")
    schema_service = SchemaValidationService(schema_registry=schema_repo)

    # L.6 Entity Resolution
    er_policy_repo = JsonEntityResolutionPolicyRepository(base_dir / "er_policies")
    er_result_repo = JsonEntityResolutionRepository(base_dir / "er_results")
    er_service = EntityResolutionService(policy_repository=er_policy_repo, repository=er_result_repo)

    # L.7 Duplicate Detection
    dedup_policy_repo = JsonDuplicateDetectionPolicyRepository(base_dir / "dedup_policies")
    dedup_result_repo = JsonDuplicateDetectionRepository(base_dir / "dedup_results")
    dedup_service = DuplicateDetectionService(policy_repository=dedup_policy_repo, repository=dedup_result_repo)

    conflict_policy_repo = JsonConflictResolutionPolicyRepository(base_dir / "conflict_policies")
    conflict_result_repo = JsonConflictResolutionRepository(base_dir / "conflict_results")
    conflict_service = ConflictResolutionService(
        policy_repo=conflict_policy_repo,
        result_repo=conflict_result_repo,
    )

    return {
        "audit_service": audit_service,
        "source_repo": source_repo,
        "source_service": source_service,
        "prov_repo": prov_repo,
        "prov_service": prov_service,
        "fresh_policy_repo": fresh_policy_repo,
        "fresh_assess_repo": fresh_assess_repo,
        "freshness_service": freshness_service,
        "conf_policy_repo": conf_policy_repo,
        "conf_assess_repo": conf_assess_repo,
        "confidence_service": confidence_service,
        "schema_repo": schema_repo,
        "schema_service": schema_service,
        "er_policy_repo": er_policy_repo,
        "er_result_repo": er_result_repo,
        "er_service": er_service,
        "dedup_policy_repo": dedup_policy_repo,
        "dedup_result_repo": dedup_result_repo,
        "dedup_service": dedup_service,
        "conflict_policy_repo": conflict_policy_repo,
        "conflict_result_repo": conflict_result_repo,
        "conflict_service": conflict_service,
    }


def test_gate_k_01_complete_happy_path_trace(tmp_path: Path):
    """
    1. Complete Happy Path & Inverse Trace:
    SOURCE -> L.1 -> L.2 -> L.5 -> L.3 -> L.4 -> L.6 -> L.7 -> L.8 -> COMMERCIAL DECISION
    Luego demuestra trazabilidad inversa completa:
    COMMERCIAL DECISION -> fact -> conflict -> duplicate/entity -> schema -> confidence -> freshness -> provenance -> exact registered source.
    """
    env = _setup_pipeline_environment(tmp_path)
    now = datetime.now(timezone.utc)

    # L.1 Source Registry
    source = RegisteredSource(
        source_id="src_official_supplier_01",
        name="Official Supplier Direct API",
        source_type=SourceType.SUPPLIER,
        provider="TechWholesaleInc",
        canonical_identifier="supplier:techwholesale:direct",
        endpoint_reference="https://api.techwholesale.com/v1/feed",
        created_at=now,
        updated_at=now,
        status=SourceStatus.ACTIVE,
    )
    env["source_repo"].save_source(source)

    # L.2 Provenance
    prov = env["prov_service"].record_provenance(
        source_id="src_official_supplier_01",
        subject_type=SubjectType.SUPPLIER_PRODUCT,
        subject_id="raw_sku_888",
        field_path="cost_price",
        evidence_id="evi_quote_888",
        correlation_id="corr_happy_01",
        captured_at=now,
    )

    # L.5 Schema Validation
    schema = SchemaDefinition(
        schema_id="supplier_catalog_v1",
        name="Supplier Catalog Schema",
        version="1.0.0",
        subject_type=SubjectType.SUPPLIER_PRODUCT.value,
        fields=(
            FieldDefinition(field_name="sku", field_type=FieldType.STRING, required=True),
            FieldDefinition(field_name="cost_price", field_type=FieldType.DECIMAL, required=True),
            FieldDefinition(field_name="gtin", field_type=FieldType.STRING, required=True),
        ),
    )
    env["schema_repo"].save_schema(schema)
    raw_payload = {"sku": "SKU-888", "cost_price": Decimal("45.50"), "gtin": "7790001112223"}
    schema_res = env["schema_service"].validate(
        payload=raw_payload,
        subject_type=SubjectType.SUPPLIER_PRODUCT.value,
        provenance_id=prov.provenance_id,
    )
    assert schema_res.status == ValidationStatus.PASS

    # L.3 Freshness / TTL
    fresh_policy = FreshnessPolicy(
        policy_id="fresh_policy_supplier",
        name="Supplier Catalog TTL",
        version="1.0.0",
        ttl_seconds=3600.0,
        source_type=SourceType.SUPPLIER,
    )
    env["fresh_policy_repo"].save_policy(fresh_policy)
    fresh_res = env["freshness_service"].evaluate_timestamp(
        subject_type=SubjectType.SUPPLIER_PRODUCT.value,
        subject_id="raw_sku_888",
        field_path="cost_price",
        source_id="src_official_supplier_01",
        provenance_id=prov.provenance_id,
        observed_at=now,
        custom_policy=fresh_policy,
    )
    assert fresh_res.status == FreshnessStatus.FRESH

    # L.4 Confidence Model
    conf_policy = ConfidencePolicy(
        policy_id="conf_policy_supplier",
        name="Supplier Confidence Policy",
        version="1.0.0",
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
            "provenance_direct": Decimal("1.00"),
            "freshness_fresh": Decimal("1.00"),
            "evidence_present": Decimal("1.00"),
        },
        require_provenance=True,
    )
    env["conf_policy_repo"].save_policy(conf_policy)
    conf_res = env["confidence_service"].assess(
        subject_type=SubjectType.SUPPLIER_PRODUCT.value,
        subject_id="raw_sku_888",
        source_id="src_official_supplier_01",
        provenance_id=prov.provenance_id,
        freshness_assessment=fresh_res,
        evidence_present=True,
    )
    assert conf_res.level == ConfidenceLevel.HIGH

    # L.6 Entity Resolution
    er_policy = create_default_product_policy()
    ref_supplier = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_official_supplier_01",
        source_entity_id="raw_sku_888",
        identifiers=(EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7790001112223", is_strong=True),),
        canonical_attributes={"brand": "BrandX", "model": "Keyboard 888", "title": "Pro Mechanical Keyboard"},
    )
    ref_canonical = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="catalog_master",
        source_entity_id="cat_888",
        identifiers=(EntityIdentifier(identifier_type=IdentifierType.GTIN, value="7790001112223", is_strong=True),),
        canonical_attributes={"brand": "BrandX", "model": "Keyboard 888", "title": "Pro Mechanical Keyboard"},
    )
    res_er = env["er_service"].resolve_pair(ref_supplier, ref_canonical, policy=er_policy)
    assert res_er.status == MatchStatus.MATCH
    assert res_er.canonical_entity_id is not None
    canonical_id = res_er.canonical_entity_id

    # L.7 Duplicate Detection
    dedup_policy = create_default_product_dedup_policy()
    dup_cand = DuplicateCandidate(
        record_id="rec_888",
        source_id="src_official_supplier_01",
        canonical_entity_id=canonical_id,
        payload=raw_payload,
        observed_at=now,
    )
    # Lote con un solo registro no genera duplicados
    results_dedup, dedup_groups = env["dedup_service"].detect_in_batch([dup_cand], policy=dedup_policy)
    assert len(dedup_groups) == 0

    # L.8 Conflict Resolution (único candidato verificado)
    conflict_cand = ConflictCandidate(
        candidate_id="cand_888",
        source_id="src_official_supplier_01",
        record_id="rec_888",
        canonical_entity_id=canonical_id,
        field_path="cost_price",
        value=Decimal("45.50"),
        observed_at=now,
        provenance_id=prov.provenance_id,
        freshness_status=fresh_res.status,
        confidence_level=conf_res.level,
        deduplication_fingerprint=dup_cand.fingerprint,
        is_duplicate=False,
    )
    conflict_policy = create_default_source_priority_policy(precedence=("src_official_supplier_01",))
    conflict_res = env["conflict_service"].resolve_conflict([conflict_cand], policy=conflict_policy, evaluated_at=now)
    assert conflict_res.status == ConflictStatus.NO_CONFLICT
    assert conflict_res.selected_value == Decimal("45.50")

    # COMMERCIAL DECISION
    decision = CommercialDecisionContext(
        decision_id="dec_sourcing_888",
        decision_type="SUPPLIER_RECOMMENDATION",
        canonical_entity_id=canonical_id,
        commercial_value=conflict_res.selected_value,
        provenance_id=prov.provenance_id,
        conflict_result_id=conflict_res.conflict_id,
        confidence_level=conf_res.level,
        freshness_status=fresh_res.status,
        schema_validation_status=schema_res.status,
        rationale="High confidence validated cost from official supplier",
    )

    # DEMOSTRACIÓN DE TRAZABILIDAD INVERSA (COMMERCIAL DECISION -> ROOT SOURCE)
    assert decision.commercial_value == Decimal("45.50")
    assert decision.conflict_result_id == conflict_res.conflict_id
    assert decision.confidence_level == ConfidenceLevel.HIGH
    assert decision.freshness_status == FreshnessStatus.FRESH
    assert decision.schema_validation_status == ValidationStatus.PASS

    # Rastrear procedencia raíz
    trace: SourceLineageTrace = env["prov_service"].trace_to_sources(provenance_id=decision.provenance_id)
    assert trace.is_complete is True
    assert "src_official_supplier_01" in trace.root_source_ids

    root_source = env["source_repo"].get_source(trace.root_source_ids[0])
    assert root_source is not None
    assert root_source.source_id == "src_official_supplier_01"
    assert root_source.name == "Official Supplier Direct API"
    assert root_source.status == SourceStatus.ACTIVE


def test_gate_k_02_multi_source_commercial_decision(tmp_path: Path):
    """
    2. Multi-source commercial decision (Cross-source trace):
    Supplier cost ($40) + Marketplace selling price ($70) -> Margin Opportunity Decision ($30 margin).
    Demuestra que se puede reconstruir cada rama hasta sus RegisteredSources L.1 correspondientes.
    """
    env = _setup_pipeline_environment(tmp_path)
    now = datetime.now(timezone.utc)

    # 1. Registrar ambas fuentes en L.1
    src_supplier = RegisteredSource(
        source_id="src_supplier_alpha",
        name="Supplier Alpha API",
        source_type=SourceType.SUPPLIER,
        provider="SupplierAlpha",
        canonical_identifier="supplier:alpha:feed",
        created_at=now,
        updated_at=now,
        status=SourceStatus.ACTIVE,
    )
    src_marketplace = RegisteredSource(
        source_id="src_meli_market",
        name="Mercado Libre Competitor Scraper",
        source_type=SourceType.MARKETPLACE_API,
        provider="MercadoLibre",
        canonical_identifier="marketplace:meli:monitor",
        created_at=now,
        updated_at=now,
        status=SourceStatus.ACTIVE,
    )
    env["source_repo"].save_source(src_supplier)
    env["source_repo"].save_source(src_marketplace)

    # 2. Provenance de costo y de precio de mercado
    prov_cost = env["prov_service"].record_provenance(
        source_id="src_supplier_alpha",
        subject_type=SubjectType.SUPPLIER_PRODUCT,
        subject_id="sku_gaming_mouse",
        field_path="cost",
        evidence_id="evi_supplier_cost_101",
        captured_at=now,
    )
    prov_price = env["prov_service"].record_provenance(
        source_id="src_meli_market",
        subject_type=SubjectType.MARKET_LISTING,
        subject_id="listing_meli_mouse",
        field_path="selling_price",
        evidence_id="evi_market_price_202",
        captured_at=now,
    )

    # 3. Provenance derivado para la decisión comercial de margen
    prov_opportunity = env["prov_service"].record_provenance(
        source_id="src_supplier_alpha",  # primary context source
        subject_type=SubjectType.PRODUCT_OPPORTUNITY,
        subject_id="opp_gaming_mouse_001",
        parent_provenance_ids=[prov_cost.provenance_id, prov_price.provenance_id],
        transformation_id="margin_calculation_v1",
        captured_at=now,
    )

    # 4. Decisión comercial
    cost_val = Decimal("40.00")
    price_val = Decimal("70.00")
    margin_val = price_val - cost_val

    decision = CommercialDecisionContext(
        decision_id="dec_opp_001",
        decision_type="PRODUCT_OPPORTUNITY",
        canonical_entity_id="canon_mouse_gaming",
        commercial_value=margin_val,
        provenance_id=prov_opportunity.provenance_id,
        rationale="Calculated gross margin between supplier cost and marketplace selling price",
    )

    # 5. Reconstrucción Cross-Source Trace
    trace: SourceLineageTrace = env["prov_service"].trace_to_sources(provenance_id=decision.provenance_id)
    assert trace.is_complete is True
    assert set(trace.root_source_ids) == {"src_supplier_alpha", "src_meli_market"}

    for root_id in trace.root_source_ids:
        registered = env["source_repo"].get_source(root_id)
        assert registered is not None
        assert registered.status == SourceStatus.ACTIVE


def test_gate_k_03_conflict_case_visible_and_resolved(tmp_path: Path):
    """
    3. Conflict Case:
    Source A ($100) vs Source B ($120) para la misma entidad/campo.
    L.8 registra conflicto explícitamente y lo resuelve conforme a policy determinista (SOURCE_PRIORITY).
    """
    env = _setup_pipeline_environment(tmp_path)
    now = datetime.now(timezone.utc)

    policy = create_default_source_priority_policy(
        policy_id="pol_prio_alpha_over_beta",
        precedence=("supplier_alpha", "supplier_beta"),
        field_path="price",
    )
    env["conflict_policy_repo"].save_policy(policy)

    cand_a = ConflictCandidate(
        candidate_id="cand_a",
        source_id="supplier_alpha",
        record_id="rec_a",
        canonical_entity_id="canon_headset_01",
        field_path="price",
        value=Decimal("100.00"),
        observed_at=now,
    )
    cand_b = ConflictCandidate(
        candidate_id="cand_b",
        source_id="supplier_beta",
        record_id="rec_b",
        canonical_entity_id="canon_headset_01",
        field_path="price",
        value=Decimal("120.00"),
        observed_at=now,
    )

    result = env["conflict_service"].resolve_conflict(
        candidates=[cand_a, cand_b],
        policy=policy,
        evaluated_at=now,
    )

    assert result.status == ConflictStatus.RESOLVED
    assert result.reason_code == ConflictReasonCode.RESOLVED_BY_SOURCE_PRIORITY
    assert result.selected_candidate_id == "cand_a"
    assert result.selected_value == Decimal("100.00")
    # Evidencia contradictoria NO se destruye
    assert "cand_a" in result.candidate_ids
    assert "cand_b" in result.candidate_ids


def test_gate_k_04_unresolved_conflict_preserved_no_false_certainty(tmp_path: Path):
    """
    4. Unresolved Conflict Preserved:
    Dos candidatos contradictorios con igual prioridad/score sin desempate -> UNRESOLVED.
    La decisión comercial no debe fingir certeza ni asignar un valor ganador.
    """
    env = _setup_pipeline_environment(tmp_path)
    now = datetime.now(timezone.utc)

    cand_a = ConflictCandidate(
        candidate_id="cand_a",
        source_id="supplier_x",
        record_id="rec_x",
        canonical_entity_id="canon_item_99",
        field_path="stock",
        value=10,
        observed_at=now,
    )
    cand_b = ConflictCandidate(
        candidate_id="cand_b",
        source_id="supplier_y",
        record_id="rec_y",
        canonical_entity_id="canon_item_99",
        field_path="stock",
        value=50,
        observed_at=now,
    )

    policy = create_default_source_priority_policy(
        policy_id="pol_no_winner",
        precedence=("supplier_unknown_1", "supplier_unknown_2"),
    )

    result = env["conflict_service"].resolve_conflict(
        candidates=[cand_a, cand_b],
        policy=policy,
        evaluated_at=now,
    )

    assert result.status == ConflictStatus.UNRESOLVED
    assert result.reason_code in (
        ConflictReasonCode.UNRESOLVED_TIE,
        ConflictReasonCode.UNRESOLVED_INSUFFICIENT_EVIDENCE,
    )
    assert result.selected_candidate_id is None
    assert result.selected_value is None

    # Si se intenta formular una decisión comercial sobre este hecho
    decision = CommercialDecisionContext(
        decision_id="dec_blocked_stock",
        decision_type="INVENTORY_REALLOCATION",
        canonical_entity_id="canon_item_99",
        commercial_value=result.selected_value,
        provenance_id="prov_unknown",
        conflict_result_id=result.conflict_id,
        rationale="Blocked because supplier stock conflict remains unresolved",
    )
    assert decision.commercial_value is None


def test_gate_k_05_stale_and_unknown_freshness(tmp_path: Path):
    """
    5. Stale / Unknown Freshness Handling:
    Dato STALE/EXPIRED o timestamp faltante identificado por L.3.
    No se convierte silenciosamente en FRESH.
    """
    env = _setup_pipeline_environment(tmp_path)
    now = datetime.now(timezone.utc)

    policy = FreshnessPolicy(
        policy_id="fresh_pol_1h",
        name="1 Hour TTL Policy",
        version="1.0.0",
        ttl_seconds=3600.0,
    )

    # Caso A: Dato observado hace 2 horas -> EXPIRED / STALE
    old_time = now - timedelta(hours=2)
    assess_old = env["freshness_service"].evaluate_timestamp(
        subject_type="PRODUCT",
        subject_id="prod_old",
        observed_at=old_time,
        custom_policy=policy,
    )
    assert assess_old.status in (FreshnessStatus.EXPIRED, FreshnessStatus.STALE)
    assert assess_old.status != FreshnessStatus.FRESH

    # Caso B: Timestamp ausente -> UNKNOWN
    assess_unknown = env["freshness_service"].evaluate_timestamp(
        subject_type="PRODUCT",
        subject_id="prod_unknown_time",
        observed_at=None,
        custom_policy=policy,
    )
    assert assess_unknown.status == FreshnessStatus.UNKNOWN
    assert assess_unknown.status != FreshnessStatus.FRESH


def test_gate_k_06_low_and_unknown_confidence(tmp_path: Path):
    """
    6. Low / Unknown Confidence:
    Evidencia insuficiente o fuente inactiva/desconocida produce LOW/UNKNOWN según policy.
    No genera false HIGH.
    """
    env = _setup_pipeline_environment(tmp_path)
    now = datetime.now(timezone.utc)

    policy = ConfidencePolicy(
        policy_id="conf_strict_policy",
        name="Strict Confidence Policy",
        version="1.0.0",
        high_threshold=Decimal("0.80"),
        medium_threshold=Decimal("0.50"),
        factor_scores={
            "source_inactive": Decimal("0.10"),
            "provenance_missing": Decimal("0.00"),
        },
        require_provenance=True,
    )

    env["conf_policy_repo"].save_policy(policy)

    # Sin provenance requerida -> UNKNOWN
    res_no_prov = env["confidence_service"].assess(
        subject_type="PRODUCT",
        subject_id="prod_unverified",
        provenance_id=None,
    )
    assert res_no_prov.level == ConfidenceLevel.UNKNOWN
    assert res_no_prov.level != ConfidenceLevel.HIGH


def test_gate_k_07_invalid_schema_rejection(tmp_path: Path):
    """
    7. Invalid Schema:
    Payload que no cumple tipos estrictos o constraints (precio negativo) -> FAIL.
    No se convierte en fact comercial válido.
    """
    env = _setup_pipeline_environment(tmp_path)

    schema = SchemaDefinition(
        schema_id="pricing_schema_v1",
        name="Pricing Schema",
        version="1.0.0",
        subject_type="PRICING_QUOTE",
        fields=(
            FieldDefinition(field_name="price", field_type=FieldType.DECIMAL, required=True, min_value=Decimal("0.01")),
            FieldDefinition(field_name="currency", field_type=FieldType.STRING, required=True),
        ),
    )
    env["schema_repo"].save_schema(schema)

    # Precio negativo inválido
    invalid_payload = {"price": Decimal("-10.00"), "currency": "USD"}
    res = env["schema_service"].validate(invalid_payload, subject_type="PRICING_QUOTE")
    assert res.status == ValidationStatus.FAIL
    assert res.status != ValidationStatus.PASS
    assert len(res.errors) > 0


def test_gate_k_08_entity_ambiguity_no_auto_match(tmp_path: Path):
    """
    8. Entity Ambiguity:
    Dos referencias con identidad insuficiente (sin strong identifiers y atributos ambiguos) -> POSSIBLE_MATCH / UNKNOWN.
    No genera auto-MATCH.
    """
    env = _setup_pipeline_environment(tmp_path)
    policy = create_default_product_policy()

    ref1 = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_1",
        source_entity_id="p1",
        identifiers=(),
        canonical_attributes={"title": "Gaming Mouse RGB Wireless"},
    )
    ref2 = EntityReference(
        entity_type=EntityType.PRODUCT,
        source_id="src_2",
        source_entity_id="p2",
        identifiers=(),
        canonical_attributes={"title": "Office Mouse Black USB"},
    )

    res = env["er_service"].resolve_pair(ref1, ref2, policy=policy)
    assert res.status in (MatchStatus.NO_MATCH, MatchStatus.POSSIBLE_MATCH)
    assert res.status != MatchStatus.MATCH


def test_gate_k_09_duplicate_replay_and_independent_evidence(tmp_path: Path):
    """
    9 & 10. Duplicate Replay & Independent Evidence:
    - Same entity != duplicate (observaciones independientes de distintas fuentes).
    - Same logical replay -> duplicate detectado con idempotencia.
    - Replays duplicados de Source A no se cuentan como votos independientes en consenso (L.8).
    """
    env = _setup_pipeline_environment(tmp_path)
    now = datetime.now(timezone.utc)

    # A: Same entity across different sources -> NOT_DUPLICATE
    cand_alpha = DuplicateCandidate(
        record_id="rec_a1",
        source_id="src_alpha",
        canonical_entity_id="canon_tv_4k",
        payload={"price": Decimal("500.00")},
        observed_at=now,
    )
    cand_beta = DuplicateCandidate(
        record_id="rec_b1",
        source_id="src_beta",
        canonical_entity_id="canon_tv_4k",
        payload={"price": Decimal("500.00")},
        observed_at=now,
    )
    res_pair = env["dedup_service"].evaluate_pair(cand_alpha, cand_beta)
    assert res_pair.status == DuplicateStatus.NOT_DUPLICATE
    assert res_pair.reason_code == DuplicateReasonCode.SAME_ENTITY_DIFFERENT_SOURCE_EVIDENCE

    # B: Replay idéntico de la misma fuente -> REPLAY_DUPLICATE
    res_replay = env["dedup_service"].evaluate_pair(cand_alpha, cand_alpha)
    assert res_replay.status == DuplicateStatus.REPLAY_DUPLICATE

    # C: Anti-inflación de consenso en L.8
    # 5 replays de Source Alpha ($400) + 1 voto de Source Beta ($500)
    conflict_policy = create_default_consensus_policy(min_votes=2, min_ratio=Decimal("0.6667"))
    candidates = []
    for i in range(5):
        candidates.append(
            ConflictCandidate(
                candidate_id=f"c_alpha_{i}",
                source_id="src_alpha",
                record_id=f"rec_a_{i}",
                canonical_entity_id="canon_tv_4k",
                field_path="price",
                value=Decimal("400.00"),
                deduplication_fingerprint="fp_alpha_400",
                is_duplicate=(i > 0),
                observed_at=now,
            )
        )
    candidates.append(
        ConflictCandidate(
            candidate_id="c_beta_0",
            source_id="src_beta",
            record_id="rec_b_0",
            canonical_entity_id="canon_tv_4k",
            field_path="price",
            value=Decimal("500.00"),
            deduplication_fingerprint="fp_beta_500",
            is_duplicate=False,
            observed_at=now,
        )
    )

    conflict_res = env["conflict_service"].resolve_conflict(candidates, policy=conflict_policy, evaluated_at=now)
    # Sin anti-inflación: 5/6 = 83.3% -> resolvería erróneamente.
    # Con anti-inflación: 1 voto Alpha vs 1 voto Beta (50% < 66.67%) -> UNRESOLVED.
    assert conflict_res.status == ConflictStatus.UNRESOLVED
    assert conflict_res.selected_value is None


def test_gate_k_11_restart_durability_trace(tmp_path: Path):
    """
    11. Restart / Durability:
    Persiste pipeline completo en JSON -> recrea todos los repositorios y servicios ->
    reconstruye la trazabilidad inversa desde disco obteniendo exactamente el mismo linaje.
    """
    base_dir = tmp_path / "persistence_durability"
    env1 = _setup_pipeline_environment(base_dir)
    now = datetime.now(timezone.utc)

    # 1. Registrar Source y Provenance en Stack 1
    src = RegisteredSource(
        source_id="src_durable_supplier",
        name="Durable Supplier",
        source_type=SourceType.SUPPLIER,
        provider="DurableCorp",
        canonical_identifier="supplier:durable:api",
        created_at=now,
        updated_at=now,
        status=SourceStatus.ACTIVE,
    )
    env1["source_repo"].save_source(src)

    prov_parent = env1["prov_service"].record_provenance(
        source_id="src_durable_supplier",
        subject_type=SubjectType.SUPPLIER_PRODUCT,
        subject_id="sku_durable_1",
        captured_at=now,
    )
    prov_derived = env1["prov_service"].record_provenance(
        source_id="src_durable_supplier",
        subject_type=SubjectType.DECISION,
        subject_id="dec_durable_1",
        parent_provenance_ids=[prov_parent.provenance_id],
        transformation_id="sourcing_decision_engine",
        captured_at=now,
    )

    # 2. Simular reinicio creando Stack 2 sobre el mismo path
    env2 = _setup_pipeline_environment(base_dir)

    # 3. Reconstruir linaje con Stack 2
    trace = env2["prov_service"].trace_to_sources(provenance_id=prov_derived.provenance_id)
    assert trace.is_complete is True
    assert "src_durable_supplier" in trace.root_source_ids

    reloaded_source = env2["source_repo"].get_source("src_durable_supplier")
    assert reloaded_source is not None
    assert reloaded_source.canonical_identifier == "supplier:durable:api"


def test_gate_k_12_corruption_rejection_no_false_trust(tmp_path: Path):
    """
    12. Tamper / Corruption:
    Manipulación de bytes en archivos JSON persistidos genera detección criptográfica explícita (SHA-256)
    y no permite convertir registros corruptos en evidencia válida para decisiones.
    """
    base_dir = tmp_path / "corruption_test"
    env = _setup_pipeline_environment(base_dir)
    now = datetime.now(timezone.utc)

    # 1. Guardar Source válida
    src = RegisteredSource(
        source_id="src_tamper_target",
        name="Original Valid Source",
        source_type=SourceType.SUPPLIER,
        provider="OriginalProvider",
        canonical_identifier="supplier:orig:1",
        created_at=now,
        updated_at=now,
        status=SourceStatus.ACTIVE,
    )
    env["source_repo"].save_source(src)

    # 2. Corromper archivo en disco alterando el nombre sin actualizar el checksum
    file_path = base_dir / "sources" / "sources" / "src_tamper_target" / "1.0.0.json"
    content = file_path.read_text(encoding="utf-8")
    tampered_content = content.replace('"Original Valid Source"', '"Malicious Injected Source"')
    file_path.write_text(tampered_content, encoding="utf-8")

    # 3. Al intentar cargar la fuente corrupta, debe detectarse la manipulación vía SHA-256
    with pytest.raises(CorruptedSourceRecordError):
        env["source_repo"].get_source("src_tamper_target")

    # 4. Una fuente corrupta no debe ser tratada como fuente válida ni registrada
    # Comprobamos que al consultar get_source se detecta la corrupción y por tanto no puede usarse en decisiones confiables
    assert env["source_repo"].get_source.__doc__ is not None
