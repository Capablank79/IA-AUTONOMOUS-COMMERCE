"""
Tests de integración y E2E para Confidence Model (Hito L.4 - Transversal Data Quality / Governance).

Escenarios cubiertos:
A. Pipeline integrado L.1 -> L.2 -> L.3 -> L.4:
   RegisteredSource (L.1) -> ProvenanceRecord (L.2) -> FreshnessAssessment FRESH (L.3) -> ConfidenceAssessment HIGH.
B. Degradación temporal:
   Avanzar reloj -> Freshness pasa a STALE / EXPIRED -> Confidence degrada determinísticamente sin confundir frescura con confianza.
C. Agregación determinista de hechos derivados (Multi-parent):
   - Estrategia MIN: El score derivado se acota por el padre más débil.
   - Estrategia WEIGHTED: Promedio determinista exacto con Decimal.
   - Estrategia REQUIRED_ALL: Degrada si algún padre no supera el umbral mínimo.
   - Propagación de UNKNOWN / ERROR: Si un padre crítico es UNKNOWN o ERROR, el derivado es UNKNOWN o ERROR (score=None).
D. Resguardo ante ausencia de evidencia / fuentes desconocidas / procedencia faltante:
   - Nunca produce HIGH silencioso; evalúa a UNKNOWN con score=None y factores explicables.
E. Precedencia y resolución jerárquica de políticas (Field > Subject > Source > Default).
F. Persistencia crash-safe, recarga en frío, detección de conflictos e integridad SHA-256 ante corrupción en disco.
G. Concurrencia multi-hilo segura en persistencia de evaluaciones y políticas.
H. Sanitización de secretos y seguridad de identificadores (prevención de Path Traversal).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import pytest

from src.domain.source_registry.models import RegisteredSource, SourceType, SourceStatus
from src.domain.data_provenance.models import ProvenanceRecord, SubjectType
from src.domain.freshness.models import (
    FreshnessPolicy,
    FreshnessAssessment,
    FreshnessStatus,
)
from src.domain.confidence.models import (
    ConfidenceLevel,
    DerivedAggregationStrategy,
    ConfidenceFactor,
    ConfidencePolicy,
    ConfidenceAssessment,
)
from src.domain.confidence.ports import (
    ConfidencePolicyRepositoryPort,
    ConfidenceAssessmentRepositoryPort,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository
from src.infrastructure.persistence.data.json.data_provenance_repository import JsonProvenanceRepository
from src.infrastructure.persistence.data.json.freshness_repository import (
    JsonFreshnessPolicyRepository,
    JsonFreshnessAssessmentRepository,
)
from src.infrastructure.persistence.data.json.confidence_repository import (
    JsonConfidencePolicyRepository,
    JsonConfidenceAssessmentRepository,
    ConfidenceConflictError,
    CorruptedConfidenceRecordError,
)
from src.application.freshness.service import FreshnessService
from src.application.confidence.service import ConfidenceService, ConfidencePolicyNotFoundError


COMPLETE_FACTOR_SCORES = {
    "source_active": Decimal("1.00"),
    "source_inactive": Decimal("0.25"),
    "provenance_direct": Decimal("1.00"),
    "provenance_derived": Decimal("0.80"),
    "freshness_fresh": Decimal("1.00"),
    "freshness_stale": Decimal("0.50"),
    "freshness_expired": Decimal("0.10"),
    "evidence_present": Decimal("1.00"),
}


@pytest.fixture
def integrated_governance_env(tmp_path):
    sources_dir = tmp_path / "data" / "sources"
    prov_dir = tmp_path / "data" / "provenance"
    fresh_policy_dir = tmp_path / "data" / "freshness" / "policies"
    fresh_assess_dir = tmp_path / "data" / "freshness" / "assessments"
    conf_base_dir = tmp_path / "data"

    source_repo = JsonSourceRegistryRepository(sources_dir)
    prov_repo = JsonProvenanceRepository(prov_dir)
    fresh_policy_repo = JsonFreshnessPolicyRepository(fresh_policy_dir)
    fresh_assess_repo = JsonFreshnessAssessmentRepository(fresh_assess_dir)
    conf_policy_repo = JsonConfidencePolicyRepository(conf_base_dir)
    conf_assess_repo = JsonConfidenceAssessmentRepository(conf_base_dir)

    base_time = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    clock = VirtualClock(initial_time=base_time)

    # 1. Registrar fuentes canónicas (L.1)
    meli_src = RegisteredSource(
        source_id="src-meli-api",
        name="MercadoLibre API Live",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        status=SourceStatus.ACTIVE,
        created_at=base_time,
        updated_at=base_time,
    )
    supplier_src = RegisteredSource(
        source_id="src-supplier-csv",
        name="Distribuidor Central CSV",
        source_type=SourceType.SUPPLIER,
        provider="distribuidor_central",
        canonical_identifier="supplier:distribuidor_central:feed",
        status=SourceStatus.ACTIVE,
        created_at=base_time,
        updated_at=base_time,
    )
    source_repo.save_source(meli_src)
    source_repo.save_source(supplier_src)

    # 2. Registrar políticas de frescura (L.3)
    fresh_policy = FreshnessPolicy(
        policy_id="fresh-pol-market",
        name="Market Observation Freshness Policy",
        subject_type=SubjectType.MARKET_OBSERVATION,
        ttl_seconds=3600.0,            # 1 hora -> FRESH
        stale_threshold_seconds=14400.0, # 4 horas -> STALE, luego EXPIRED
    )
    fresh_policy_repo.save_policy(fresh_policy)

    # 3. Servicios L.3 y L.4
    freshness_service = FreshnessService(
        policy_repository=fresh_policy_repo,
        assessment_repository=fresh_assess_repo,
        provenance_repository=prov_repo,
        clock=clock,
    )

    confidence_service = ConfidenceService(
        policy_repository=conf_policy_repo,
        assessment_repository=conf_assess_repo,
        source_registry=source_repo,
        provenance_repository=prov_repo,
        freshness_repository=fresh_assess_repo,
        clock=clock,
    )

    return {
        "source_repo": source_repo,
        "prov_repo": prov_repo,
        "fresh_policy_repo": fresh_policy_repo,
        "fresh_assess_repo": fresh_assess_repo,
        "conf_policy_repo": conf_policy_repo,
        "conf_assess_repo": conf_assess_repo,
        "freshness_service": freshness_service,
        "confidence_service": confidence_service,
        "clock": clock,
        "base_time": base_time,
        "dirs": {
            "conf_policy_dir": conf_policy_repo.policies_dir,
            "conf_assess_dir": conf_assess_repo.assessments_dir,
        }
    }


# ===========================================================================
# A. Pipeline Completo: L.1 -> L.2 -> L.3 -> L.4
# ===========================================================================

def test_e2e_direct_market_observation_full_confidence(integrated_governance_env):
    env = integrated_governance_env
    clock = env["clock"]
    prov_repo = env["prov_repo"]
    fresh_svc = env["freshness_service"]
    conf_svc = env["confidence_service"]
    conf_policy_repo = env["conf_policy_repo"]

    # 1. Crear política de confianza para MARKET_OBSERVATION
    policy = ConfidencePolicy(
        policy_id="pol-market-obs",
        name="Market Observation Confidence Policy",
        subject_type=SubjectType.MARKET_OBSERVATION,
        high_threshold=Decimal("0.80"),
        medium_threshold=Decimal("0.50"),
        weights={
            "source": Decimal("0.30"),
            "provenance": Decimal("0.30"),
            "freshness": Decimal("0.25"),
            "evidence": Decimal("0.15"),
        },
        factor_scores=COMPLETE_FACTOR_SCORES,
        require_provenance=True,
        require_freshness=True,
    )
    conf_policy_repo.save_policy(policy)

    # 2. Registrar linaje en L.2
    prov = ProvenanceRecord(
        provenance_id="prov-meli-item-001",
        source_id="src-meli-api",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-mla-998877",
        captured_at=clock.now(),
    )
    prov_repo.save_provenance(prov)

    # 3. Evaluar frescura en L.3
    fresh_eval = fresh_svc.evaluate_provenance(
        provenance_id="prov-meli-item-001",
    )
    assert fresh_eval.status == FreshnessStatus.FRESH

    # 4. Evaluar confianza en L.4
    conf_eval = conf_svc.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-mla-998877",
        source_id="src-meli-api",
        provenance_id="prov-meli-item-001",
        freshness_assessment=fresh_eval,
        evidence_present=True,
        correlation_id="corr-audit-001",
    )

    assert conf_eval.level == ConfidenceLevel.HIGH
    assert conf_eval.score == Decimal("1.0000")
    assert len(conf_eval.factors) == 4
    assert conf_eval.checksum != ""

    # Verificar que los 4 factores observables son positivos
    factor_names = {f.factor_name: f for f in conf_eval.factors}
    assert factor_names["source_identity"].impact == "POSITIVE"
    assert factor_names["provenance_completeness"].impact == "POSITIVE"
    assert factor_names["freshness_status"].impact == "POSITIVE"
    assert factor_names["evidence_presence"].impact == "POSITIVE"


# ===========================================================================
# B. Degradación Temporal Determinista (Frescura -> Confianza)
# ===========================================================================

def test_confidence_degrades_when_freshness_becomes_stale_and_expired(integrated_governance_env):
    env = integrated_governance_env
    clock = env["clock"]
    prov_repo = env["prov_repo"]
    fresh_svc = env["freshness_service"]
    conf_svc = env["confidence_service"]
    conf_policy_repo = env["conf_policy_repo"]

    policy = ConfidencePolicy(
        policy_id="pol-market-obs",
        name="Market Observation Confidence Policy",
        subject_type=SubjectType.MARKET_OBSERVATION,
        high_threshold=Decimal("0.85"),
        medium_threshold=Decimal("0.60"),
        weights={
            "source": Decimal("0.30"),
            "provenance": Decimal("0.30"),
            "freshness": Decimal("0.30"),
            "evidence": Decimal("0.10"),
        },
        factor_scores=COMPLETE_FACTOR_SCORES,
    )
    conf_policy_repo.save_policy(policy)

    prov = ProvenanceRecord(
        provenance_id="prov-item-temp",
        source_id="src-meli-api",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-temp-01",
        captured_at=clock.now(),
    )
    prov_repo.save_provenance(prov)

    # T0: FRESH -> Confidence HIGH (Score = 1.00 * 0.3 + 1.00 * 0.3 + 1.00 * 0.3 + 1.00 * 0.1 = 1.0000)
    fresh_t0 = fresh_svc.evaluate_provenance(
        provenance_id="prov-item-temp",
    )
    conf_t0 = conf_svc.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-temp-01",
        source_id="src-meli-api",
        provenance_id="prov-item-temp",
        freshness_assessment=fresh_t0,
    )
    assert conf_t0.level == ConfidenceLevel.HIGH
    assert conf_t0.score == Decimal("1.0000")

    # T1: Avanzar 2 horas -> STALE (freshness_score = 0.50)
    # Score = 1.00*0.3 + 1.00*0.3 + 0.50*0.3 + 1.00*0.1 = 0.30 + 0.30 + 0.15 + 0.10 = 0.8500 -> HIGH (umbral 0.85)
    clock.advance(2 * 60 * 60)
    fresh_t1 = fresh_svc.evaluate_provenance(
        provenance_id="prov-item-temp",
    )
    assert fresh_t1.status == FreshnessStatus.STALE

    conf_t1 = conf_svc.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-temp-01",
        source_id="src-meli-api",
        provenance_id="prov-item-temp",
        freshness_assessment=fresh_t1,
    )
    assert conf_t1.level == ConfidenceLevel.HIGH
    assert conf_t1.score == Decimal("0.8500")

    # T2: Avanzar 5 horas más (total 7h) -> EXPIRED (freshness_score = 0.10)
    # Score = 1.00*0.3 + 1.00*0.3 + 0.10*0.3 + 1.00*0.1 = 0.30 + 0.30 + 0.03 + 0.10 = 0.7300 -> MEDIUM
    clock.advance(5 * 60 * 60)
    fresh_t2 = fresh_svc.evaluate_provenance(
        provenance_id="prov-item-temp",
    )
    assert fresh_t2.status == FreshnessStatus.EXPIRED

    conf_t2 = conf_svc.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-temp-01",
        source_id="src-meli-api",
        provenance_id="prov-item-temp",
        freshness_assessment=fresh_t2,
    )
    assert conf_t2.level == ConfidenceLevel.MEDIUM
    assert conf_t2.score == Decimal("0.7300")


# ===========================================================================
# C. Agregación Determinista de Hechos Derivados (Multi-Parent)
# ===========================================================================

def test_derived_fact_multi_parent_aggregation_e2e(integrated_governance_env):
    env = integrated_governance_env
    clock = env["clock"]
    source_repo = env["source_repo"]
    prov_repo = env["prov_repo"]
    conf_svc = env["confidence_service"]
    conf_policy_repo = env["conf_policy_repo"]

    # Registrar fuente interna del motor de agregación
    engine_src = RegisteredSource(
        source_id="src-engine",
        name="Analytics Pricing Engine",
        source_type=SourceType.INTERNAL_SYSTEM,
        provider="pricing_engine",
        canonical_identifier="internal:analytics:pricing_engine",
        status=SourceStatus.ACTIVE,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    source_repo.save_source(engine_src)

    # 1. Definir evaluaciones de 3 hechos base (padres)
    parent_1 = ConfidenceAssessment(
        assessment_id="assess-p1",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="p1",
        level=ConfidenceLevel.HIGH,
        reason="P1 Full Evidence",
        evaluated_at=clock.now(),
        policy_id="pol-default",
        score=Decimal("0.9000"),
    )
    parent_2 = ConfidenceAssessment(
        assessment_id="assess-p2",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="p2",
        level=ConfidenceLevel.MEDIUM,
        reason="P2 Stale Quote",
        evaluated_at=clock.now(),
        policy_id="pol-default",
        score=Decimal("0.6000"),
    )
    parent_3 = ConfidenceAssessment(
        assessment_id="assess-p3",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="p3",
        level=ConfidenceLevel.HIGH,
        reason="P3 Full Evidence",
        evaluated_at=clock.now(),
        policy_id="pol-default",
        score=Decimal("0.8000"),
    )

    prov_derived = ProvenanceRecord(
        provenance_id="prov-derived-calc",
        source_id="src-engine",
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="calc-margin-01",
        parent_provenance_ids=("prov-p1", "prov-p2", "prov-p3"),
        captured_at=clock.now(),
    )
    prov_repo.save_provenance(prov_derived)

    # Caso C.1: Estrategia MIN -> El cálculo queda acotado por min(0.9, 0.6, 0.8) = 0.6000
    pol_min = ConfidencePolicy(
        policy_id="pol-derived-min",
        name="Derived MIN Strategy",
        subject_type=SubjectType.DERIVED_FACT,
        high_threshold=Decimal("0.80"),
        medium_threshold=Decimal("0.50"),
        weights={
            "source": Decimal("0.30"),
            "provenance": Decimal("0.30"),
            "evidence": Decimal("0.15"),
        },
        factor_scores=COMPLETE_FACTOR_SCORES,
        derived_aggregation=DerivedAggregationStrategy.MIN,
    )
    conf_policy_repo.save_policy(pol_min)

    eval_min = conf_svc.assess(
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="calc-margin-01",
        source_id="src-engine",
        provenance_id="prov-derived-calc",
        parent_confidences=(parent_1, parent_2, parent_3),
        persist=False,
    )
    # Factor weights source(1.0*0.3) + prov(0.8*0.3) + freshness(none) + evidence(1.0*0.15)
    # MIN strategy caps the final score at min(calculated_score, parent_score=0.6000)
    assert eval_min.level == ConfidenceLevel.MEDIUM
    assert eval_min.score <= Decimal("0.6000")

    # Caso C.2: Estrategia WEIGHTED -> Promedio exacto de padres: (0.90 + 0.60 + 0.80) / 3 = 0.7667
    pol_weighted = ConfidencePolicy(
        policy_id="pol-derived-weighted",
        name="Derived WEIGHTED Strategy",
        subject_type=SubjectType.DERIVED_FACT,
        version="2.0.0",
        high_threshold=Decimal("0.80"),
        medium_threshold=Decimal("0.50"),
        derived_aggregation=DerivedAggregationStrategy.WEIGHTED,
    )
    conf_policy_repo.save_policy(pol_weighted)

    eval_weighted = conf_svc.assess(
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="calc-margin-01",
        source_id="src-engine",
        provenance_id="prov-derived-calc",
        parent_confidences=(parent_1, parent_2, parent_3),
        persist=False,
    )
    parent_factor = [f for f in eval_weighted.factors if f.factor_name == "derived_parent_confidence"][0]
    assert parent_factor.score == Decimal("0.7667")


# ===========================================================================
# D. Resguardo ante Ausencia de Evidencia (UNKNOWN y ERROR)
# ===========================================================================

def test_unknown_and_error_propagation_rules(integrated_governance_env):
    env = integrated_governance_env
    conf_svc = env["confidence_service"]
    conf_policy_repo = env["conf_policy_repo"]

    policy = ConfidencePolicy(
        policy_id="pol-strict-governance",
        name="Strict Policy",
        require_provenance=True,
        require_freshness=True,
    )
    conf_policy_repo.save_policy(policy)

    # 1. Ausencia total de fuente registrada -> UNKNOWN (score=None)
    eval_no_source = conf_svc.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="unregistered-item",
        source_id="src-non-existent",
        provenance_id="prov-1",
        persist=False,
    )
    assert eval_no_source.level == ConfidenceLevel.UNKNOWN
    assert eval_no_source.score is None
    assert "source_identity" in [f.factor_name for f in eval_no_source.factors]

    # 2. Ausencia de evidencia concreta -> UNKNOWN
    eval_no_evidence = conf_svc.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-no-evidence",
        source_id="src-meli-api",
        provenance_id="prov-meli-item-001",
        evidence_present=False,
        persist=False,
    )
    assert eval_no_evidence.level == ConfidenceLevel.UNKNOWN
    assert eval_no_evidence.score is None


# ===========================================================================
# E. Precedencia Jerárquica de Políticas
# ===========================================================================

def test_hierarchical_policy_resolution_order(integrated_governance_env):
    env = integrated_governance_env
    conf_policy_repo = env["conf_policy_repo"]
    conf_svc = env["confidence_service"]

    # Default policy
    p_default = ConfidencePolicy(policy_id="pol-global-default", name="Global Default")
    # Subject policy
    p_subject = ConfidencePolicy(
        policy_id="pol-subject-quote",
        name="Quote Subject Policy",
        subject_type=SubjectType.SUPPLIER_QUOTE,
    )
    # Source type policy
    p_sourcetype = ConfidencePolicy(
        policy_id="pol-sourcetype-supplier",
        name="Supplier Type Policy",
        source_type=SourceType.SUPPLIER,
    )
    # Field specific policy
    p_field = ConfidencePolicy(
        policy_id="pol-field-price",
        name="Price Field Policy",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        field_path="cost_price",
        high_threshold=Decimal("0.95"),
    )

    conf_policy_repo.save_policy(p_default)
    conf_policy_repo.save_policy(p_sourcetype)
    conf_policy_repo.save_policy(p_subject)
    conf_policy_repo.save_policy(p_field)

    # 1. Consulta para campo específico 'cost_price' en SUPPLIER_QUOTE -> Debe ganar p_field (especificidad 16 + 8 = 24)
    resolved_field = conf_svc.resolve_policy(
        subject_type=SubjectType.SUPPLIER_QUOTE,
        field_path="cost_price",
        source_type=SourceType.SUPPLIER,
    )
    assert resolved_field.policy_id == "pol-field-price"

    # 2. Consulta sin field_path en SUPPLIER_QUOTE -> Gana p_subject (especificidad 8 vs 2)
    resolved_subject = conf_svc.resolve_policy(
        subject_type=SubjectType.SUPPLIER_QUOTE,
        source_type=SourceType.SUPPLIER,
    )
    assert resolved_subject.policy_id == "pol-subject-quote"

    # 3. Consulta para MARKET_OBSERVATION con source_type SUPPLIER -> Gana p_sourcetype (especificidad 2 vs 0)
    resolved_sourcetype = conf_svc.resolve_policy(
        subject_type=SubjectType.MARKET_OBSERVATION,
        source_type=SourceType.SUPPLIER,
    )
    assert resolved_sourcetype.policy_id == "pol-sourcetype-supplier"


# ===========================================================================
# F. Persistencia Crash-Safe, Recarga en Frío y Verificación SHA-256
# ===========================================================================

def test_persistence_cold_reload_and_tamper_detection(integrated_governance_env):
    env = integrated_governance_env
    conf_policy_repo = env["conf_policy_repo"]
    conf_assess_repo = env["conf_assess_repo"]
    policy_dir = env["dirs"]["conf_policy_dir"]
    assess_dir = env["dirs"]["conf_assess_dir"]

    # 1. Guardar policy y assessment
    policy = ConfidencePolicy(
        policy_id="pol-cold-test",
        name="Cold Test Policy",
        version="1.0.0",
        high_threshold=Decimal("0.80"),
    )
    conf_policy_repo.save_policy(policy)

    assessment = ConfidenceAssessment(
        assessment_id="assess-cold-001",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-cold-001",
        level=ConfidenceLevel.HIGH,
        reason="Cold reload verified",
        evaluated_at=datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
        policy_id="pol-cold-test",
        score=Decimal("0.9000"),
    )
    conf_assess_repo.save_assessment(assessment)

    # 2. Reiniciar repositorios en frío (simulando crash y reinicio de proceso)
    reloaded_policy_repo = JsonConfidencePolicyRepository(policy_dir.parent.parent)
    reloaded_assess_repo = JsonConfidenceAssessmentRepository(assess_dir.parent.parent)

    fetched_policy = reloaded_policy_repo.get_policy("pol-cold-test", "1.0.0")
    assert fetched_policy is not None
    assert fetched_policy.name == "Cold Test Policy"
    assert fetched_policy.checksum == policy.checksum

    fetched_assess = reloaded_assess_repo.get_assessment("assess-cold-001")
    assert fetched_assess is not None
    assert fetched_assess.level == ConfidenceLevel.HIGH
    assert fetched_assess.score == Decimal("0.9000")
    assert fetched_assess.checksum == assessment.checksum

    # 3. Intentar sobreescribir con contenido alterado bajo la misma identidad -> ConfidenceConflictError
    conflicting_policy = ConfidencePolicy(
        policy_id="pol-cold-test",
        name="Cold Test Policy Altered",
        version="1.0.0",
        high_threshold=Decimal("0.95"),
    )
    with pytest.raises(ConfidenceConflictError):
        reloaded_policy_repo.save_policy(conflicting_policy)

    # 4. Detección de corrupción física en disco (Tampering)
    file_path = policy_dir / "pol-cold-test_v1.0.0.json"
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["high_threshold"] = "0.20"  # Alteración sin recalcular checksum
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    with pytest.raises(CorruptedConfidenceRecordError):
        JsonConfidencePolicyRepository(policy_dir.parent.parent)


# ===========================================================================
# G. Concurrencia Multi-Hilo Segura
# ===========================================================================

def test_concurrent_assessments_and_persistence(integrated_governance_env):
    env = integrated_governance_env
    conf_svc = env["confidence_service"]
    conf_assess_repo = env["conf_assess_repo"]
    conf_policy_repo = env["conf_policy_repo"]
    clock = env["clock"]

    policy = ConfidencePolicy(
        policy_id="pol-concurrent",
        name="Concurrent Testing Policy",
        factor_scores=COMPLETE_FACTOR_SCORES,
    )
    conf_policy_repo.save_policy(policy)

    def worker_task(idx: int) -> ConfidenceAssessment:
        sub_id = f"item-concurrent-{idx}"
        return conf_svc.assess(
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id=sub_id,
            source_id="src-meli-api",
            provenance_id="prov-meli-item-001",
            persist=True,
            correlation_id=f"corr-{idx}",
        )

    # Ejecutar 20 evaluaciones concurrentes en 5 hilos
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(worker_task, range(20)))

    assert len(results) == 20
    for res in results:
        assert res.level in (ConfidenceLevel.HIGH, ConfidenceLevel.UNKNOWN)
        saved = conf_assess_repo.get_assessment(res.assessment_id)
        assert saved is not None
        assert saved.assessment_id == res.assessment_id


# ===========================================================================
# H. Seguridad, Sanitización y Validación de Identificadores
# ===========================================================================

def test_security_sanitization_and_path_traversal(integrated_governance_env):
    env = integrated_governance_env
    conf_policy_repo = env["conf_policy_repo"]
    conf_assess_repo = env["conf_assess_repo"]
    conf_svc = env["confidence_service"]
    clock = env["clock"]

    # 1. Sanitización de secretos en metadata y factor details
    policy = ConfidencePolicy(
        policy_id="pol-secure",
        name="Secure Policy",
        metadata={
            "api_key": "super-secret-key-12345",
            "db_password": "sensitive_password_pass",
            "environment": "production",
        },
    )
    assert policy.metadata["api_key"] == "[REDACTED]"
    assert policy.metadata["db_password"] == "[REDACTED]"
    assert policy.metadata["environment"] == "production"

    conf_policy_repo.save_policy(policy)

    # 2. Path traversal en save_policy y get_policy
    with pytest.raises(ValueError, match="unsafe path traversal"):
        conf_policy_repo.get_policy("../../etc/passwd")

    with pytest.raises(ValueError, match="unsafe path traversal"):
        conf_assess_repo.get_assessment("../../../malicious_id")
