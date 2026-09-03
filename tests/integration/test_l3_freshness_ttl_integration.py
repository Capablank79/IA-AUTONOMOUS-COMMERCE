"""
Tests de integración y E2E para Freshness / TTL (Hito L.3 - Transversal Data Quality / Governance).

Escenarios cubiertos:
A. RegisteredSource -> Provenance -> FreshnessPolicy -> FRESH.
B. Advance fake clock -> same fact becomes STALE.
C. Missing provenance timestamp -> UNKNOWN.
D. Supplier quote TTL evaluation.
E. Marketplace / price TTL evaluation.
F. Derived fact with stale parent -> derived freshness follows contract (oldest parent constraint).
G. Crash-safe persistence & restart -> policies and assessments safely reloaded.
H. Policy version change -> deterministic re-evaluation.
I. Business consumer boundary -> temporal acceptability pre-check before commercial consumption.
"""

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import pytest

from src.domain.source_registry.models import RegisteredSource, SourceType, SourceStatus
from src.domain.data_provenance.models import ProvenanceRecord, SubjectType, generate_deterministic_provenance_id
from src.domain.freshness.models import (
    FreshnessPolicy,
    FreshnessAssessment,
    FreshnessStatus,
)
from src.domain.reliability.ports import ClockPort
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository
from src.infrastructure.persistence.data.json.data_provenance_repository import JsonProvenanceRepository
from src.infrastructure.persistence.data.json.freshness_repository import (
    JsonFreshnessPolicyRepository,
    JsonFreshnessAssessmentRepository,
    CorruptedFreshnessPolicyError,
    CorruptedFreshnessAssessmentError,
)
from src.application.freshness.service import FreshnessService


@pytest.fixture
def integrated_env(tmp_path):
    sources_dir = tmp_path / "sources"
    prov_dir = tmp_path / "provenance"
    policy_dir = tmp_path / "freshness_policies"
    assess_dir = tmp_path / "freshness_assessments"

    source_repo = JsonSourceRegistryRepository(sources_dir)
    prov_repo = JsonProvenanceRepository(prov_dir)
    policy_repo = JsonFreshnessPolicyRepository(policy_dir)
    assess_repo = JsonFreshnessAssessmentRepository(assess_dir)

    base_time = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    clock = VirtualClock(initial_time=base_time)

    # 1. Registrar fuentes canónicas L.1
    meli_src = RegisteredSource(
        source_id="src-meli-api",
        name="MercadoLibre Official API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=base_time,
        updated_at=base_time,
    )
    supplier_src = RegisteredSource(
        source_id="src-supplier-csv",
        name="Distribuidor Mayorista CSV Feed",
        source_type=SourceType.SUPPLIER,
        provider="mayorista_feed",
        canonical_identifier="supplier:mayorista:feed_csv",
        created_at=base_time,
        updated_at=base_time,
    )
    source_repo.save_source(meli_src)
    source_repo.save_source(supplier_src)

    # 2. Configurar políticas L.3 por source_type y subject_type
    meli_policy = FreshnessPolicy(
        policy_id="pol-meli-marketplace",
        name="Marketplace Price TTL",
        ttl_seconds=3600.0,  # 1 hora
        stale_threshold_seconds=7200.0,  # 2 horas
        source_id="src-meli-api",
        subject_type=SubjectType.MARKET_OBSERVATION,
    )
    supplier_policy = FreshnessPolicy(
        policy_id="pol-supplier-feed",
        name="Supplier Feed TTL",
        ttl_seconds=86400.0,  # 24 horas
        stale_threshold_seconds=172800.0,  # 48 horas
        source_type=SourceType.SUPPLIER,
    )
    generic_policy = FreshnessPolicy(
        policy_id="pol-generic-default",
        name="Default System Freshness Policy",
        ttl_seconds=86400.0,
        stale_threshold_seconds=172800.0,
    )
    policy_repo.save_policy(meli_policy)
    policy_repo.save_policy(supplier_policy)
    policy_repo.save_policy(generic_policy)

    service = FreshnessService(
        policy_repository=policy_repo,
        assessment_repository=assess_repo,
        provenance_repository=prov_repo,
        source_registry=source_repo,
        clock=clock,
    )

    return {
        "service": service,
        "clock": clock,
        "source_repo": source_repo,
        "prov_repo": prov_repo,
        "policy_repo": policy_repo,
        "assess_repo": assess_repo,
        "base_time": base_time,
        "dirs": {
            "sources": sources_dir,
            "provenance": prov_dir,
            "policies": policy_dir,
            "assessments": assess_dir,
        },
    }


def test_scenario_a_and_b_registered_source_provenance_fresh_then_stale(integrated_env):
    """Escenarios A & B: RegisteredSource -> Provenance -> Policy -> FRESH, luego avanzar reloj -> STALE."""
    service = integrated_env["service"]
    clock = integrated_env["clock"]
    prov_repo = integrated_env["prov_repo"]
    base_time = integrated_env["base_time"]

    # Registrar procedencia L.2 para una observación de mercado
    prov_id = generate_deterministic_provenance_id(
        source_id="src-meli-api",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-prod-100",
    )
    record = ProvenanceRecord(
        provenance_id=prov_id,
        source_id="src-meli-api",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-prod-100",
        captured_at=base_time,
    )
    prov_repo.save_provenance(record)

    # Evaluar a los 10 minutos (FRESH: age=600 < 3600)
    clock.advance(seconds=600)
    assessment = service.evaluate_provenance(prov_id, persist=True)

    assert assessment.status == FreshnessStatus.FRESH
    assert assessment.is_usable is True
    assert assessment.age_seconds == 600.0
    assert assessment.ttl_seconds == 3600.0
    assert assessment.policy_id == "pol-meli-marketplace"

    # Avanzar reloj 3500 segundos más (Total: 4100 segundos -> age >= ttl 3600 -> STALE)
    clock.advance(seconds=3500)
    assessment_stale = service.evaluate_provenance(prov_id, persist=True)

    assert assessment_stale.status == FreshnessStatus.STALE
    assert assessment_stale.is_usable is False
    assert assessment_stale.age_seconds == 4100.0

    # Avanzar a más de 7200 segundos (EXPIRED)
    clock.advance(seconds=4000)  # Total: 8100 segundos
    assessment_expired = service.evaluate_provenance(prov_id, persist=True)
    assert assessment_expired.status == FreshnessStatus.EXPIRED
    assert assessment_expired.is_usable is False


def test_scenario_c_missing_provenance_timestamp_unknown(integrated_env):
    """Escenario C: Timestamp ausente -> UNKNOWN (UNKNOWN != FRESH)."""
    service = integrated_env["service"]

    assessment = service.evaluate_timestamp(
        observed_at=None,
        subject_id="obs-unknown-time",
        subject_type=SubjectType.MARKET_OBSERVATION,
        source_id="src-meli-api",
        persist=True,
    )
    assert assessment.status == FreshnessStatus.UNKNOWN
    assert assessment.is_usable is False
    assert "missing" in assessment.reason.lower()
    assert assessment.age_seconds is None


def test_scenario_d_supplier_quote_ttl(integrated_env):
    """Escenario D: Evaluación de TTL para cotización de proveedor (Supplier quote)."""
    service = integrated_env["service"]
    clock = integrated_env["clock"]
    prov_repo = integrated_env["prov_repo"]
    base_time = integrated_env["base_time"]

    prov_id = "prov-supplier-quote-99"
    record = ProvenanceRecord(
        provenance_id=prov_id,
        source_id="src-supplier-csv",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="quote-batch-99",
        captured_at=base_time,
    )
    prov_repo.save_provenance(record)

    # 12 horas después -> Fresh (TTL es 24h = 86400s)
    clock.advance(seconds=43200)
    assess_12h = service.evaluate_provenance(prov_id)
    assert assess_12h.status == FreshnessStatus.FRESH
    assert assess_12h.age_seconds == 43200.0

    # 25 horas después -> Stale (age=90000 > 86400)
    clock.advance(seconds=46800)  # Total 90000s
    assess_25h = service.evaluate_provenance(prov_id)
    assert assess_25h.status == FreshnessStatus.STALE


def test_scenario_e_marketplace_price_field_level_override(integrated_env):
    """Escenario E: TTL específico a nivel de campo (ej. spot_price con TTL ultracorto)."""
    service = integrated_env["service"]
    policy_repo = integrated_env["policy_repo"]
    clock = integrated_env["clock"]
    base_time = integrated_env["base_time"]

    # Política general de producto: 3600s, pero para el campo spot_price: 60s
    field_policy = FreshnessPolicy(
        policy_id="pol-spot-price-ultra-fresh",
        name="Ultra Fresh Spot Price",
        ttl_seconds=60.0,
        subject_type=SubjectType.MARKET_OBSERVATION,
        field_path="pricing.spot_price",
    )
    policy_repo.save_policy(field_policy)

    # Evaluación a nivel de campo spot_price vs otro campo
    assess_spot = service.evaluate_timestamp(
        observed_at=base_time,
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="quote-123",
        field_path="pricing.spot_price",
        source_id="src-meli-api",
    )
    assert assess_spot.policy_id == "pol-spot-price-ultra-fresh"
    assert assess_spot.ttl_seconds == 60.0

    # A los 70 segundos -> spot_price es STALE
    clock.advance(seconds=70)
    assess_spot_70s = service.evaluate_timestamp(
        observed_at=base_time,
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="quote-123",
        field_path="pricing.spot_price",
        source_id="src-meli-api",
    )
    assert assess_spot_70s.status == FreshnessStatus.STALE


def test_scenario_f_derived_fact_with_stale_parent(integrated_env):
    """Escenario F: Dato derivado con un padre STALE hereda la degradación (regla del padre más degradado)."""
    service = integrated_env["service"]
    clock = integrated_env["clock"]
    prov_repo = integrated_env["prov_repo"]
    base_time = integrated_env["base_time"]

    # Padre 1: Capturado en base_time (hace 5000s -> STALE bajo meli_policy de 3600s)
    parent_1 = ProvenanceRecord(
        provenance_id="prov-p1-meli",
        source_id="src-meli-api",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-competitor-p1",
        captured_at=base_time,
    )
    # Padre 2: Capturado en base_time + 4000s (hace 1000s -> FRESH bajo supplier_policy de 86400s)
    parent_2 = ProvenanceRecord(
        provenance_id="prov-p2-supp",
        source_id="src-supplier-csv",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="quote-supplier-p2",
        captured_at=base_time + timedelta(seconds=4000),
    )
    prov_repo.save_provenance(parent_1)
    prov_repo.save_provenance(parent_2)

    # Dato derivado generado a los 5000s
    clock.advance(seconds=5000)
    derived_record = ProvenanceRecord(
        provenance_id="prov-derived-aggregated-price",
        source_id="src-meli-api",
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="pricing-sku-100",
        captured_at=clock.now(),
        parent_provenance_ids=("prov-p1-meli", "prov-p2-supp"),
    )
    prov_repo.save_provenance(derived_record)

    derived_assessment = service.evaluate_derived_provenance(derived_record, persist=True)

    # El dato derivado NO puede ocultar la degradación de parent_1
    assert derived_assessment.status == FreshnessStatus.STALE
    assert derived_assessment.is_usable is False
    assert "degraded by parent" in derived_assessment.reason.lower()


def test_scenario_g_crash_safe_persistence_and_restart(integrated_env):
    """Escenario G: Persistencia crash-safe y recuperación íntegra tras reinicio de repositorios."""
    service = integrated_env["service"]
    dirs = integrated_env["dirs"]
    base_time = integrated_env["base_time"]

    # Crear y persistir evaluación
    assessment = service.evaluate_timestamp(
        observed_at=base_time,
        subject_type=SubjectType.GENERIC_FACT,
        subject_id="inv-item-55",
        source_id="src-meli-api",
        persist=True,
    )

    # Simular reinicio creando nuevas instancias de repositorios apuntando al mismo disco
    restarted_policy_repo = JsonFreshnessPolicyRepository(dirs["policies"])
    restarted_assess_repo = JsonFreshnessAssessmentRepository(dirs["assessments"])

    loaded_policy = restarted_policy_repo.get_policy("pol-meli-marketplace")
    assert loaded_policy is not None
    assert loaded_policy.ttl_seconds == 3600.0

    loaded_assess = restarted_assess_repo.get_assessment(assessment.assessment_id)
    assert loaded_assess is not None
    assert loaded_assess.assessment_id == assessment.assessment_id
    assert loaded_assess.status == FreshnessStatus.FRESH
    assert loaded_assess.checksum == assessment.checksum


def test_scenario_h_policy_version_change_deterministic_reevaluation(integrated_env):
    """Escenario H: Cambio de versión de política -> reevaluación determinista."""
    service = integrated_env["service"]
    policy_repo = integrated_env["policy_repo"]
    clock = integrated_env["clock"]
    base_time = integrated_env["base_time"]

    # A 500s de antigüedad con TTL de 3600s v1.0.0 -> FRESH
    clock.advance(seconds=500)
    assess_v1 = service.evaluate_timestamp(
        observed_at=base_time,
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-prod-999",
        source_id="src-meli-api",
    )
    assert assess_v1.status == FreshnessStatus.FRESH
    assert assess_v1.policy_version == "1.0.0"

    # Actualizar política a v2.0.0 con TTL hiper-estricto de 300s
    policy_v2 = FreshnessPolicy(
        policy_id="pol-meli-marketplace-v2",
        name="Marketplace Price TTL Strict",
        ttl_seconds=300.0,
        version="2.0.0",
        source_id="src-meli-api",
        subject_type=SubjectType.MARKET_OBSERVATION,
    )
    # Sobrescribir la anterior o guardar la nueva versión
    policy_repo.save_policy(policy_v2)

    # Reevaluar con custom_policy v2 o consultar con la versión más reciente
    assess_v2 = service.evaluate_timestamp(
        observed_at=base_time,
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-prod-999",
        source_id="src-meli-api",
        custom_policy=policy_v2,
    )
    assert assess_v2.status == FreshnessStatus.STALE
    assert assess_v2.policy_version == "2.0.0"
    assert assess_v2.ttl_seconds == 300.0


def test_scenario_i_business_consumer_temporal_precheck_boundary(integrated_env):
    """
    Escenario I (E2E Boundary): Un consumidor comercial consulta freshness ANTES de utilizar un precio.
    Demuestra rechazo temporal explícito sin invadir L.4 (Confidence) ni alterar datos comerciales.
    """
    service = integrated_env["service"]
    clock = integrated_env["clock"]
    prov_repo = integrated_env["prov_repo"]
    policy_repo = integrated_env["policy_repo"]
    base_time = integrated_env["base_time"]

    # Política para cotizaciones comerciales (TTL = 3600s = 1 hora)
    quote_policy = FreshnessPolicy(
        policy_id="pol-commercial-quote",
        name="Commercial Quote TTL Policy",
        ttl_seconds=3600.0,
        stale_threshold_seconds=7200.0,
        subject_type=SubjectType.SUPPLIER_QUOTE,
    )
    policy_repo.save_policy(quote_policy)

    # Registro de procedencia de una cotización
    prov_id = "prov-quote-for-pricing"
    quote_prov = ProvenanceRecord(
        provenance_id=prov_id,
        source_id="src-meli-api",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="quote-sku-prime",
        captured_at=base_time,
    )
    prov_repo.save_provenance(quote_prov)

    class CommercialPricingConsumer:
        def __init__(self, freshness_service: FreshnessService):
            self._freshness = freshness_service

        def is_quote_temporally_acceptable(self, provenance_id: str) -> bool:
            assessment = self._freshness.evaluate_provenance(provenance_id)
            return assessment.is_usable

    consumer = CommercialPricingConsumer(service)

    # 1. En t=0 (+10m), la cotización es aceptable temporalmente
    clock.advance(seconds=600)
    assert consumer.is_quote_temporally_acceptable(prov_id) is True

    # 2. En t=+2h (+7200s), la cotización ha expirado temporalmente (age = 7800s > 3600s)
    clock.advance(seconds=7200)
    assert consumer.is_quote_temporally_acceptable(prov_id) is False
