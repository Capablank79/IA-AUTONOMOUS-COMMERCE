"""
Unit tests exhaustivos para Confidence Model (Hito L.4 - Transversal Data Quality / Governance).

Cubre todos los requerimientos mandatorios:
1. ConfidenceFactor: creación, inmutabilidad, validación de score/weight Decimal [0, 1], sanitización de detalles y to_dict.
2. ConfidencePolicy: creación, inmutabilidad, validación SemVer, umbrales Decimal, pesos Decimal, derived_aggregation, checksum SHA-256.
3. ConfidenceAssessment: creación, inmutabilidad, timestamps UTC, score Decimal, factores observables, checksum SHA-256, helper properties (is_high, is_usable).
4. Precedencia determinista de resolución de políticas: field_path (16) > subject_type (8) > source_id (4) > source_type (2) > default.
5. Evaluación de confianza completa con evidencia directa (L.1 activo + L.2 directo + L.3 FRESH + evidencia presente -> HIGH).
6. Impacto de frescura degradada (L.3 STALE -> MEDIUM/LOW, L.3 EXPIRED -> LOW).
7. Preservación estricta de UNKNOWN: missing evidence, missing source, missing provenance, missing freshness -> UNKNOWN (UNKNOWN != LOW != 0.00).
8. Preservación estricta de ERROR ante inputs erróneos (L.3 ERROR o parent ERROR -> ERROR).
9. Estrategias de agregación de datos derivados (MIN, WEIGHTED, REQUIRED_ALL).
10. Aislamiento de padres críticos: un padre LOW o UNKNOWN no puede ser enmascarado silenciosamente por padres HIGH.
11. Repositorio JsonConfidencePolicyRepository: persistencia atómica, idempotencia por checksum, detección de conflictos y detección de archivos corruptos.
12. Repositorio JsonConfidenceAssessmentRepository: persistencia atómica, idempotencia por checksum, consultas por subject/campo, detección de conflictos y corrupción.
13. Flag de persistencia opcional en ConfidenceService (persist=True / False).
14. Prevención rigurosa de Path Traversal en todos los identificadores.
15. Aritmética Decimal pura sin uso de floats para decisiones sensibles.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import tempfile
import pytest

from src.domain.confidence.models import (
    ConfidenceLevel,
    DerivedAggregationStrategy,
    ConfidenceFactor,
    ConfidencePolicy,
    ConfidenceAssessment,
    compute_policy_checksum,
    compute_assessment_checksum,
)
from src.domain.confidence.ports import (
    ConfidencePolicyRepositoryPort,
    ConfidenceAssessmentRepositoryPort,
)
from src.infrastructure.persistence.data.json.confidence_repository import (
    JsonConfidencePolicyRepository,
    JsonConfidenceAssessmentRepository,
    ConfidenceConflictError,
    CorruptedConfidenceRecordError,
    CorruptedConfidencePolicyError,
    CorruptedConfidenceAssessmentError,
)
from src.application.confidence.service import (
    ConfidenceService,
    ConfidenceServiceError,
    ConfidencePolicyNotFoundError,
)
from src.domain.source_registry.models import (
    RegisteredSource,
    SourceType,
    SourceStatus,
)
from src.domain.data_provenance.models import (
    ProvenanceRecord,
    SubjectType,
    generate_deterministic_provenance_id,
)
from src.domain.freshness.models import (
    FreshnessAssessment,
    FreshnessStatus,
    FreshnessPolicy,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository
from src.infrastructure.persistence.data.json.data_provenance_repository import JsonProvenanceRepository
from src.infrastructure.persistence.data.json.freshness_repository import (
    JsonFreshnessPolicyRepository,
    JsonFreshnessAssessmentRepository,
)


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
def temp_dir():
    d = tempfile.mkdtemp(prefix="test_confidence_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def virtual_clock():
    base_time = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=base_time)


@pytest.fixture
def policy_repo(temp_dir):
    return JsonConfidencePolicyRepository(temp_dir)


@pytest.fixture
def assessment_repo(temp_dir):
    return JsonConfidenceAssessmentRepository(temp_dir)


@pytest.fixture
def source_repo(temp_dir):
    return JsonSourceRegistryRepository(temp_dir)


@pytest.fixture
def provenance_repo(temp_dir):
    return JsonProvenanceRepository(temp_dir)


@pytest.fixture
def freshness_assess_repo(temp_dir):
    return JsonFreshnessAssessmentRepository(temp_dir)


# ---------------------------------------------------------------------------
# 1. ConfidenceFactor: Creación, inmutabilidad, tipos Decimal y validaciones
# ---------------------------------------------------------------------------

def test_confidence_factor_valid_and_immutable():
    factor = ConfidenceFactor(
        factor_name="source_identity",
        factor_type="SOURCE_IDENTITY",
        score=Decimal("0.95"),
        weight=Decimal("0.30"),
        impact="POSITIVE",
        details={"provider": "mercadolibre", "api_key": "secret-token-123"},
    )
    assert factor.factor_name == "source_identity"
    assert factor.factor_type == "SOURCE_IDENTITY"
    assert factor.score == Decimal("0.95")
    assert factor.weight == Decimal("0.30")
    assert factor.impact == "POSITIVE"
    # Redacción automática de secretos en details
    assert factor.details["api_key"] == "[REDACTED]"
    assert factor.details["provider"] == "mercadolibre"

    # Inmutabilidad
    with pytest.raises(Exception):
        factor.score = Decimal("1.00")  # type: ignore

    # to_dict serializa Decimals a strings
    d = factor.to_dict()
    assert d["score"] == "0.95"
    assert d["weight"] == "0.30"
    assert d["impact"] == "POSITIVE"


def test_confidence_factor_invalid_inputs():
    # Empty name
    with pytest.raises(ValueError, match="factor_name"):
        ConfidenceFactor(factor_name="", factor_type="TEST")

    # Empty type
    with pytest.raises(ValueError, match="factor_type"):
        ConfidenceFactor(factor_name="test", factor_type="")

    # Float instead of Decimal for score
    with pytest.raises(ValueError, match="must be a Decimal"):
        ConfidenceFactor(factor_name="test", factor_type="TEST", score=0.8)  # type: ignore

    # Score out of bounds [0, 1]
    with pytest.raises(ValueError, match="between 0 and 1"):
        ConfidenceFactor(factor_name="test", factor_type="TEST", score=Decimal("1.05"))

    with pytest.raises(ValueError, match="between 0 and 1"):
        ConfidenceFactor(factor_name="test", factor_type="TEST", score=Decimal("-0.01"))

    # Float instead of Decimal for weight
    with pytest.raises(ValueError, match="must be a Decimal"):
        ConfidenceFactor(factor_name="test", factor_type="TEST", weight=0.3)  # type: ignore

    # Weight out of bounds [0, 1]
    with pytest.raises(ValueError, match="between 0 and 1"):
        ConfidenceFactor(factor_name="test", factor_type="TEST", weight=Decimal("1.50"))


# ---------------------------------------------------------------------------
# 2. ConfidencePolicy: Creación, umbrales Decimal, pesos y Checksum SHA-256
# ---------------------------------------------------------------------------

def test_confidence_policy_creation_and_checksum_verification():
    policy = ConfidencePolicy(
        policy_id="pol-market-default",
        name="Market Observation Confidence Policy",
        version="1.0.0",
        high_threshold=Decimal("0.85"),
        medium_threshold=Decimal("0.55"),
        weights={
            "source": Decimal("0.30"),
            "provenance": Decimal("0.30"),
            "freshness": Decimal("0.25"),
            "evidence": Decimal("0.15"),
        },
        factor_scores=COMPLETE_FACTOR_SCORES,
        require_provenance=True,
        require_freshness=True,
        derived_aggregation=DerivedAggregationStrategy.MIN,
        metadata={"environment": "production"},
    )
    assert policy.policy_id == "pol-market-default"
    assert policy.high_threshold == Decimal("0.85")
    assert policy.medium_threshold == Decimal("0.55")
    assert len(policy.checksum) == 64  # SHA-256 hex digest

    # Inmutabilidad
    with pytest.raises(Exception):
        policy.high_threshold = Decimal("0.90")  # type: ignore


def test_confidence_policy_invalid_thresholds_and_weights():
    # Medium threshold > High threshold
    with pytest.raises(ValueError, match="cannot be greater than high_threshold"):
        ConfidencePolicy(
            policy_id="pol-invalid",
            name="Invalid Policy",
            high_threshold=Decimal("0.50"),
            medium_threshold=Decimal("0.80"),
        )

    # Negative threshold
    with pytest.raises(ValueError, match="between 0 and 1"):
        ConfidencePolicy(
            policy_id="pol-invalid",
            name="Invalid Policy",
            high_threshold=Decimal("-0.10"),
        )

    # Float threshold rejected
    with pytest.raises(ValueError, match="must be a Decimal"):
        ConfidencePolicy(
            policy_id="pol-invalid",
            name="Invalid Policy",
            high_threshold=0.85,  # type: ignore
        )

    # Float weight rejected
    with pytest.raises(ValueError, match="must be a Decimal"):
        ConfidencePolicy(
            policy_id="pol-invalid",
            name="Invalid Policy",
            weights={"source": 0.30},  # type: ignore
        )

    # Invalid SemVer
    with pytest.raises(ValueError, match="version"):
        ConfidencePolicy(
            policy_id="pol-invalid",
            name="Invalid Policy",
            version="1.0-invalid",
        )

    # Checksum mismatch
    with pytest.raises(ValueError, match="Policy checksum mismatch"):
        ConfidencePolicy(
            policy_id="pol-mismatch",
            name="Mismatch Policy",
            checksum="badchecksum1234567890abcdef1234567890abcdef1234567890abcdef12345678",
        )


# ---------------------------------------------------------------------------
# 3. ConfidenceAssessment: Creación, validaciones, checksum y helpers
# ---------------------------------------------------------------------------

def test_confidence_assessment_creation_and_properties(virtual_clock):
    now = virtual_clock.now()
    factor = ConfidenceFactor(
        factor_name="source_identity",
        factor_type="SOURCE_IDENTITY",
        score=Decimal("1.00"),
        weight=Decimal("0.30"),
        impact="POSITIVE",
    )
    assessment = ConfidenceAssessment(
        assessment_id="assess-001",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-100",
        level=ConfidenceLevel.HIGH,
        reason="All factors verified",
        evaluated_at=now,
        policy_id="pol-market-default",
        score=Decimal("0.9000"),
        policy_version="1.0.0",
        factors=(factor,),
        correlation_id="corr-123",
    )
    assert assessment.is_high is True
    assert assessment.is_usable is True
    assert assessment.score == Decimal("0.9000")
    assert len(assessment.checksum) == 64

    # Naive datetime rejection
    with pytest.raises(ValueError, match="timezone-aware"):
        ConfidenceAssessment(
            assessment_id="assess-002",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-100",
            level=ConfidenceLevel.HIGH,
            reason="Test",
            evaluated_at=datetime(2026, 9, 2, 12, 0, 0),  # Naive
            policy_id="pol-default",
        )

    # Float score rejected
    with pytest.raises(ValueError, match="must be a Decimal"):
        ConfidenceAssessment(
            assessment_id="assess-003",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-100",
            level=ConfidenceLevel.HIGH,
            reason="Test",
            evaluated_at=now,
            policy_id="pol-default",
            score=0.90,  # type: ignore
        )


# ---------------------------------------------------------------------------
# 4. Precedencia de resolución de políticas determinista
# ---------------------------------------------------------------------------

def test_policy_resolution_precedence(policy_repo):
    p_generic = ConfidencePolicy(policy_id="pol-default", name="Default Global")
    p_source_type = ConfidencePolicy(
        policy_id="pol-source-type",
        name="Supplier Source Type Policy",
        source_type=SourceType.SUPPLIER,
    )
    p_source_id = ConfidencePolicy(
        policy_id="pol-source-id",
        name="Meli Source Specific Policy",
        source_id="src-meli",
    )
    p_subject = ConfidencePolicy(
        policy_id="pol-subject-type",
        name="Quote Subject Policy",
        subject_type=SubjectType.SUPPLIER_QUOTE,
    )
    p_field = ConfidencePolicy(
        policy_id="pol-field-level",
        name="Price Field Policy",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        field_path="pricing.net_cost",
    )

    for p in (p_generic, p_source_type, p_source_id, p_subject, p_field):
        policy_repo.save_policy(p)

    service = ConfidenceService(policy_repository=policy_repo)

    # 1. Field path tiene mayor precedencia (16 + 8 = 24)
    res_field = service.resolve_policy(
        subject_type=SubjectType.SUPPLIER_QUOTE,
        field_path="pricing.net_cost",
        source_id="src-meli",
    )
    assert res_field.policy_id == "pol-field-level"

    # 2. Subject type tiene precedencia sobre source_id y source_type (8 > 4 > 2)
    res_subject = service.resolve_policy(
        subject_type=SubjectType.SUPPLIER_QUOTE,
        source_id="src-meli",
    )
    assert res_subject.policy_id == "pol-subject-type"

    # 3. Source ID tiene precedencia sobre source_type (4 > 2)
    res_source_id = service.resolve_policy(
        subject_type=SubjectType.MARKET_OBSERVATION,
        source_id="src-meli",
        source_type=SourceType.MARKETPLACE_API,
    )
    assert res_source_id.policy_id == "pol-source-id"

    # 4. Source Type aplica cuando no hay source_id ni subject_type específico (2)
    res_source_type = service.resolve_policy(
        subject_type=SubjectType.GENERIC_FACT,
        source_type=SourceType.SUPPLIER,
    )
    assert res_source_type.policy_id == "pol-source-type"

    # 5. Global default cuando no hay match específico
    res_default = service.resolve_policy(
        subject_type=SubjectType.GENERIC_FACT,
        source_type=SourceType.INTERNAL_SYSTEM,
    )
    assert res_default.policy_id == "pol-default"


def test_policy_resolution_version_tie_breaking(policy_repo):
    p_v1 = ConfidencePolicy(policy_id="pol-quote", name="Quote Policy V1", version="1.0.0", subject_type=SubjectType.SUPPLIER_QUOTE)
    p_v2 = ConfidencePolicy(policy_id="pol-quote", name="Quote Policy V2", version="2.0.0", subject_type=SubjectType.SUPPLIER_QUOTE)
    policy_repo.save_policy(p_v1)
    policy_repo.save_policy(p_v2)

    service = ConfidenceService(policy_repository=policy_repo)
    resolved = service.resolve_policy(subject_type=SubjectType.SUPPLIER_QUOTE)
    assert resolved.version == "2.0.0"


def test_policy_resolution_not_found_raises(policy_repo):
    service = ConfidenceService(policy_repository=policy_repo, default_policy=None)
    with pytest.raises(ConfidencePolicyNotFoundError):
        service.resolve_policy(subject_type=SubjectType.DECISION)


# ---------------------------------------------------------------------------
# 5. Evaluación de Confianza: Evidencia Directa Completa -> HIGH
# ---------------------------------------------------------------------------

def test_assess_high_confidence_with_full_evidence(
    policy_repo, assessment_repo, source_repo, provenance_repo, freshness_assess_repo, virtual_clock
):
    now = virtual_clock.now()

    # 1. Registrar Fuente L.1
    src = RegisteredSource(
        source_id="src-meli",
        name="MercadoLibre API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)

    # 2. Registrar Provenance L.2
    prov = ProvenanceRecord(
        provenance_id="prov-101",
        source_id="src-meli",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-01",
        captured_at=now,
    )
    provenance_repo.save_provenance(prov)

    # 3. Registrar Freshness L.3 (FRESH)
    fresh_eval = FreshnessAssessment(
        assessment_id="fresh-101",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-01",
        status=FreshnessStatus.FRESH,
        reason="Observed within TTL",
        evaluated_at=now,
        ttl_seconds=3600.0,
        age_seconds=120.0,
        policy_id="pol-fresh-default",
    )
    freshness_assess_repo.save_assessment(fresh_eval)

    # 4. Registrar Policy L.4
    policy = ConfidencePolicy(
        policy_id="pol-default",
        name="Default Confidence Policy",
        high_threshold=Decimal("0.80"),
        medium_threshold=Decimal("0.50"),
        weights={
            "source": Decimal("0.30"),
            "provenance": Decimal("0.30"),
            "freshness": Decimal("0.25"),
            "evidence": Decimal("0.15"),
        },
        factor_scores=COMPLETE_FACTOR_SCORES,
    )
    policy_repo.save_policy(policy)

    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        provenance_repository=provenance_repo,
        freshness_repository=freshness_assess_repo,
        clock=virtual_clock,
    )

    assessment = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-01",
        source_id="src-meli",
        provenance_id="prov-101",
        freshness_assessment=fresh_eval,
        evidence_present=True,
    )

    assert assessment.level == ConfidenceLevel.HIGH
    assert assessment.is_high is True
    assert assessment.is_usable is True
    assert assessment.score == Decimal("1.0000")
    assert len(assessment.factors) == 4
    # Verificación de persistencia
    saved = assessment_repo.get_assessment(assessment.assessment_id)
    assert saved is not None
    assert saved.checksum == assessment.checksum


# ---------------------------------------------------------------------------
# 6. Impacto de frescura degradada (STALE / EXPIRED) en la confianza
# ---------------------------------------------------------------------------

def test_assess_stale_freshness_degrades_confidence(
    policy_repo, assessment_repo, source_repo, provenance_repo, virtual_clock
):
    now = virtual_clock.now()

    src = RegisteredSource(
        source_id="src-meli",
        name="MercadoLibre API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)

    prov = ProvenanceRecord(
        provenance_id="prov-102",
        source_id="src-meli",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-02",
        captured_at=now,
    )
    provenance_repo.save_provenance(prov)

    # Freshness STALE (score = 0.50)
    stale_eval = FreshnessAssessment(
        assessment_id="fresh-102",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-02",
        status=FreshnessStatus.STALE,
        reason="Age exceeded TTL",
        evaluated_at=now,
        ttl_seconds=3600.0,
        age_seconds=4000.0,
        policy_id="pol-fresh-default",
    )

    policy = ConfidencePolicy(
        policy_id="pol-default",
        name="Default Confidence Policy",
        high_threshold=Decimal("0.90"),
        medium_threshold=Decimal("0.60"),
        weights={
            "source": Decimal("0.30"),
            "provenance": Decimal("0.30"),
            "freshness": Decimal("0.25"),
            "evidence": Decimal("0.15"),
        },
        factor_scores=COMPLETE_FACTOR_SCORES,
    )
    policy_repo.save_policy(policy)

    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
    )

    assessment = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-02",
        source_id="src-meli",
        provenance_id="prov-102",
        freshness_assessment=stale_eval,
        evidence_present=True,
    )

    # Score esperado: (1.0*0.30 + 1.0*0.30 + 0.50*0.25 + 1.0*0.15) / 1.0 = 0.30 + 0.30 + 0.125 + 0.15 = 0.8750
    assert assessment.score == Decimal("0.8750")
    # 0.8750 está entre medium (0.60) y high (0.90) -> MEDIUM
    assert assessment.level == ConfidenceLevel.MEDIUM
    assert assessment.is_usable is True
    assert assessment.is_high is False


def test_assess_expired_freshness_drops_to_low(
    policy_repo, assessment_repo, source_repo, provenance_repo, virtual_clock
):
    now = virtual_clock.now()

    src = RegisteredSource(
        source_id="src-meli",
        name="MercadoLibre API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)

    prov = ProvenanceRecord(
        provenance_id="prov-103",
        source_id="src-meli",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-03",
        captured_at=now,
    )
    provenance_repo.save_provenance(prov)

    # Freshness EXPIRED (score = 0.10)
    expired_eval = FreshnessAssessment(
        assessment_id="fresh-103",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-03",
        status=FreshnessStatus.EXPIRED,
        reason="Age exceeded stale threshold",
        evaluated_at=now,
        ttl_seconds=3600.0,
        age_seconds=10000.0,
        policy_id="pol-fresh-default",
    )

    policy = ConfidencePolicy(
        policy_id="pol-default",
        name="Default Confidence Policy",
        high_threshold=Decimal("0.85"),
        medium_threshold=Decimal("0.80"),
        weights={
            "source": Decimal("0.30"),
            "provenance": Decimal("0.30"),
            "freshness": Decimal("0.25"),
            "evidence": Decimal("0.15"),
        },
        factor_scores=COMPLETE_FACTOR_SCORES,
    )
    policy_repo.save_policy(policy)

    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
    )

    assessment = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-03",
        source_id="src-meli",
        provenance_id="prov-103",
        freshness_assessment=expired_eval,
        evidence_present=True,
    )

    # Score esperado: (1.0*0.30 + 1.0*0.30 + 0.10*0.25 + 1.0*0.15) = 0.30 + 0.30 + 0.025 + 0.15 = 0.7750 < 0.80 -> LOW
    assert assessment.score == Decimal("0.7750")
    assert assessment.level == ConfidenceLevel.LOW
    assert assessment.is_usable is False


# ---------------------------------------------------------------------------
# 7. Preservación estricta de UNKNOWN (UNKNOWN != LOW != 0.00)
# ---------------------------------------------------------------------------

def test_missing_evidence_evaluates_to_unknown(policy_repo, assessment_repo, source_repo, provenance_repo, virtual_clock):
    now = virtual_clock.now()
    src = RegisteredSource(
        source_id="src-meli",
        name="MercadoLibre API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)
    prov = ProvenanceRecord(
        provenance_id="prov-104",
        source_id="src-meli",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-04",
        captured_at=now,
    )
    provenance_repo.save_provenance(prov)
    policy_repo.save_policy(ConfidencePolicy(policy_id="pol-default", name="Default Policy"))

    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
    )

    # Evidence missing -> evidence_present=False
    assessment = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-meli-04",
        source_id="src-meli",
        provenance_id="prov-104",
        evidence_present=False,
    )

    assert assessment.level == ConfidenceLevel.UNKNOWN
    assert assessment.score is None  # Score must NOT be Decimal("0.00")
    assert assessment.is_usable is False
    assert "missing" in assessment.reason


def test_missing_source_registry_evaluates_to_unknown(policy_repo, assessment_repo, virtual_clock):
    policy_repo.save_policy(ConfidencePolicy(policy_id="pol-default", name="Default Policy"))
    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        clock=virtual_clock,
    )

    assessment = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-unregistered",
        source_id="src-unregistered",
        evidence_present=True,
    )

    assert assessment.level == ConfidenceLevel.UNKNOWN
    assert assessment.score is None


def test_missing_required_provenance_evaluates_to_unknown(policy_repo, assessment_repo, source_repo, virtual_clock):
    now = virtual_clock.now()
    src = RegisteredSource(
        source_id="src-meli",
        name="MercadoLibre API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)
    policy_repo.save_policy(ConfidencePolicy(
        policy_id="pol-default",
        name="Default Policy",
        require_provenance=True,
    ))
    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        clock=virtual_clock,
    )

    assessment = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-no-prov",
        source_id="src-meli",
        provenance_id=None,  # Missing provenance
        evidence_present=True,
    )

    assert assessment.level == ConfidenceLevel.UNKNOWN
    assert assessment.score is None


def test_missing_required_freshness_evaluates_to_unknown(
    policy_repo, assessment_repo, source_repo, provenance_repo, virtual_clock
):
    now = virtual_clock.now()
    src = RegisteredSource(
        source_id="src-meli",
        name="MercadoLibre API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)
    prov = ProvenanceRecord(
        provenance_id="prov-105",
        source_id="src-meli",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-105",
        captured_at=now,
    )
    provenance_repo.save_provenance(prov)

    # Policy requires freshness explicitly
    policy_repo.save_policy(ConfidencePolicy(
        policy_id="pol-default",
        name="Default Policy",
        require_freshness=True,
    ))

    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
    )

    assessment = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-105",
        source_id="src-meli",
        provenance_id="prov-105",
        freshness_assessment=None,  # Missing freshness assessment
        evidence_present=True,
    )

    assert assessment.level == ConfidenceLevel.UNKNOWN
    assert assessment.score is None


# ---------------------------------------------------------------------------
# 8. Preservación estricta de ERROR ante inputs erróneos
# ---------------------------------------------------------------------------

def test_freshness_error_propagates_confidence_error(
    policy_repo, assessment_repo, source_repo, provenance_repo, virtual_clock
):
    now = virtual_clock.now()
    src = RegisteredSource(
        source_id="src-meli",
        name="MercadoLibre API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)
    prov = ProvenanceRecord(
        provenance_id="prov-106",
        source_id="src-meli",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-106",
        captured_at=now,
    )
    provenance_repo.save_provenance(prov)

    # Freshness ERROR (e.g. invalid future timestamp)
    error_freshness = FreshnessAssessment(
        assessment_id="fresh-err-01",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-106",
        status=FreshnessStatus.ERROR,
        reason="Future timestamp violation",
        evaluated_at=now,
        ttl_seconds=3600.0,
        age_seconds=-1000.0,
        policy_id="pol-fresh",
    )

    policy_repo.save_policy(ConfidencePolicy(policy_id="pol-default", name="Default Policy"))

    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
    )

    assessment = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-106",
        source_id="src-meli",
        provenance_id="prov-106",
        freshness_assessment=error_freshness,
        evidence_present=True,
    )

    assert assessment.level == ConfidenceLevel.ERROR
    assert assessment.score is None
    assert "error" in assessment.reason.lower()


# ---------------------------------------------------------------------------
# 9. Estrategias de agregación de datos derivados (MIN, WEIGHTED, REQUIRED_ALL)
# ---------------------------------------------------------------------------

def test_derived_parent_aggregation_strategies(policy_repo, assessment_repo, virtual_clock):
    now = virtual_clock.now()

    parent_high = ConfidenceAssessment(
        assessment_id="assess-p1",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="parent-1",
        level=ConfidenceLevel.HIGH,
        reason="High evidence",
        evaluated_at=now,
        policy_id="pol-default",
        score=Decimal("0.9000"),
    )
    parent_med = ConfidenceAssessment(
        assessment_id="assess-p2",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="parent-2",
        level=ConfidenceLevel.MEDIUM,
        reason="Medium evidence",
        evaluated_at=now,
        policy_id="pol-default",
        score=Decimal("0.6000"),
    )

    # 1. Strategy MIN
    policy_min = ConfidencePolicy(
        policy_id="pol-min",
        name="MIN Strategy Policy",
        derived_aggregation=DerivedAggregationStrategy.MIN,
    )
    score_min = ConfidenceService._aggregate_parent_confidence([parent_high, parent_med], policy_min)
    assert score_min == Decimal("0.6000")

    # 2. Strategy WEIGHTED
    policy_weighted = ConfidencePolicy(
        policy_id="pol-weighted",
        name="WEIGHTED Strategy Policy",
        derived_aggregation=DerivedAggregationStrategy.WEIGHTED,
    )
    score_weighted = ConfidenceService._aggregate_parent_confidence([parent_high, parent_med], policy_weighted)
    # (0.9000 + 0.6000) / 2 = 0.7500
    assert score_weighted == Decimal("0.7500")

    # 3. Strategy REQUIRED_ALL
    policy_req_all = ConfidencePolicy(
        policy_id="pol-req-all",
        name="REQUIRED_ALL Strategy Policy",
        derived_aggregation=DerivedAggregationStrategy.REQUIRED_ALL,
    )
    score_req = ConfidenceService._aggregate_parent_confidence([parent_high, parent_med], policy_req_all)
    assert score_req == Decimal("0.6000")


def test_derived_parent_unknown_or_error_propagation(policy_repo, assessment_repo, source_repo, provenance_repo, virtual_clock):
    now = virtual_clock.now()
    src = RegisteredSource(
        source_id="src-derived",
        name="Internal Engine",
        source_type=SourceType.INTERNAL_SYSTEM,
        provider="system",
        canonical_identifier="internal:system:engine",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)
    prov = ProvenanceRecord(
        provenance_id="prov-derived-01",
        source_id="src-derived",
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="derived-fact-01",
        parent_provenance_ids=("prov-p1", "prov-p2"),
        captured_at=now,
    )
    provenance_repo.save_provenance(prov)

    policy = ConfidencePolicy(
        policy_id="pol-derived",
        name="Derived Policy",
        derived_aggregation=DerivedAggregationStrategy.MIN,
    )
    policy_repo.save_policy(policy)

    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
    )

    parent_high = ConfidenceAssessment(
        assessment_id="assess-p-high",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="p-high",
        level=ConfidenceLevel.HIGH,
        reason="High",
        evaluated_at=now,
        policy_id="pol-derived",
        score=Decimal("0.9500"),
    )
    parent_unknown = ConfidenceAssessment(
        assessment_id="assess-p-unknown",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="p-unknown",
        level=ConfidenceLevel.UNKNOWN,
        reason="Missing",
        evaluated_at=now,
        policy_id="pol-derived",
        score=None,
    )

    # When one parent is UNKNOWN, derived confidence must be UNKNOWN
    assessment = service.assess(
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="derived-fact-01",
        source_id="src-derived",
        provenance_id="prov-derived-01",
        parent_confidences=(parent_high, parent_unknown),
        evidence_present=True,
    )
    assert assessment.level == ConfidenceLevel.UNKNOWN
    assert assessment.score is None

    # When one parent is ERROR, derived confidence must be ERROR
    parent_error = ConfidenceAssessment(
        assessment_id="assess-p-error",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="p-error",
        level=ConfidenceLevel.ERROR,
        reason="Error",
        evaluated_at=now,
        policy_id="pol-derived",
        score=None,
    )
    assessment_err = service.assess(
        subject_type=SubjectType.DERIVED_FACT,
        subject_id="derived-fact-01",
        source_id="src-derived",
        provenance_id="prov-derived-01",
        parent_confidences=(parent_high, parent_error),
        evidence_present=True,
    )
    assert assessment_err.level == ConfidenceLevel.ERROR
    assert assessment_err.score is None


# ---------------------------------------------------------------------------
# 10. JsonConfidencePolicyRepository: Persistencia atómica, Idempotencia, Conflicto y Corrupción
# ---------------------------------------------------------------------------

def test_json_policy_repository_lifecycle_and_conflict(temp_dir):
    repo = JsonConfidencePolicyRepository(temp_dir)
    policy = ConfidencePolicy(
        policy_id="pol-prod",
        name="Production Policy",
        version="1.0.0",
        high_threshold=Decimal("0.85"),
        medium_threshold=Decimal("0.55"),
    )

    # 1. Save policy
    saved = repo.save_policy(policy)
    assert saved.checksum == policy.checksum

    # 2. Idempotent save returns identical instance
    again = repo.save_policy(policy)
    assert again.checksum == policy.checksum

    # 3. Get by policy_id and SemVer
    loaded = repo.get_policy("pol-prod", version="1.0.0")
    assert loaded is not None
    assert loaded.high_threshold == Decimal("0.85")

    # 4. Conflict detection: saving different content under same ID and version raises ConfidenceConflictError
    conflicting_policy = ConfidencePolicy(
        policy_id="pol-prod",
        name="Production Policy Modified",
        version="1.0.0",
        high_threshold=Decimal("0.95"),  # Changed
        medium_threshold=Decimal("0.55"),
    )
    with pytest.raises(ConfidenceConflictError):
        repo.save_policy(conflicting_policy)


def test_json_policy_repository_corrupted_file_detection(temp_dir):
    repo = JsonConfidencePolicyRepository(temp_dir)
    policy = ConfidencePolicy(policy_id="pol-valid", name="Valid Policy")
    repo.save_policy(policy)

    # Corromper físicamente el archivo JSON en disco
    target_file = temp_dir / "confidence" / "policies" / "pol-valid_v1.0.0.json"
    assert target_file.exists()
    target_file.write_text("{\"policy_id\": \"pol-valid\", \"high_threshold\": \"not-a-decimal\"}", encoding="utf-8")

    # Reinicializar repositorio debe detectar la corrupción y lanzar CorruptedConfidencePolicyError
    with pytest.raises(CorruptedConfidencePolicyError):
        JsonConfidencePolicyRepository(temp_dir)


# ---------------------------------------------------------------------------
# 11. JsonConfidenceAssessmentRepository: Persistencia atómica, Idempotencia, Consultas y Corrupción
# ---------------------------------------------------------------------------

def test_json_assessment_repository_lifecycle_and_queries(temp_dir, virtual_clock):
    repo = JsonConfidenceAssessmentRepository(temp_dir)
    now = virtual_clock.now()

    a1 = ConfidenceAssessment(
        assessment_id="assess-101",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="prod-123",
        field_path="price",
        level=ConfidenceLevel.HIGH,
        reason="Fresh and complete",
        evaluated_at=now,
        policy_id="pol-default",
        score=Decimal("0.9500"),
    )
    a2 = ConfidenceAssessment(
        assessment_id="assess-102",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="prod-123",
        field_path="price",
        level=ConfidenceLevel.MEDIUM,
        reason="Slightly stale",
        evaluated_at=now + timedelta(minutes=10),
        policy_id="pol-default",
        score=Decimal("0.7000"),
    )
    a3 = ConfidenceAssessment(
        assessment_id="assess-103",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="prod-999",
        level=ConfidenceLevel.LOW,
        reason="Expired",
        evaluated_at=now,
        policy_id="pol-default",
        score=Decimal("0.3000"),
    )

    repo.save_assessment(a1)
    repo.save_assessment(a2)
    repo.save_assessment(a3)

    # Idempotent save
    repo.save_assessment(a1)

    # Get by ID
    loaded_1 = repo.get_assessment("assess-101")
    assert loaded_1 is not None
    assert loaded_1.score == Decimal("0.9500")

    # Find by subject and field
    results = repo.find_by_subject(subject_id="prod-123", field_path="price")
    assert len(results) == 2
    # Ordenados por evaluated_at desc (a2 es más reciente que a1)
    assert results[0].assessment_id == "assess-102"
    assert results[1].assessment_id == "assess-101"

    # Get latest by subject
    latest = repo.get_latest_by_subject(subject_id="prod-123", field_path="price")
    assert latest is not None
    assert latest.assessment_id == "assess-102"

    # Conflict on same ID with different content
    conflicting_a1 = ConfidenceAssessment(
        assessment_id="assess-101",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="prod-123",
        field_path="price",
        level=ConfidenceLevel.LOW,  # Changed
        reason="Tampered",
        evaluated_at=now,
        policy_id="pol-default",
        score=Decimal("0.1000"),
    )
    with pytest.raises(ConfidenceConflictError):
        repo.save_assessment(conflicting_a1)


def test_json_assessment_repository_corrupted_file_detection(temp_dir, virtual_clock):
    repo = JsonConfidenceAssessmentRepository(temp_dir)
    now = virtual_clock.now()
    a = ConfidenceAssessment(
        assessment_id="assess-corrupt-target",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="prod-001",
        level=ConfidenceLevel.HIGH,
        reason="Initial",
        evaluated_at=now,
        policy_id="pol-default",
        score=Decimal("0.9000"),
    )
    repo.save_assessment(a)

    target_file = temp_dir / "confidence" / "assessments" / "assess-corrupt-target.json"
    assert target_file.exists()
    target_file.write_text("{\"assessment_id\": \"assess-corrupt-target\", \"level\": \"NOT_A_LEVEL\"}", encoding="utf-8")

    with pytest.raises(CorruptedConfidenceAssessmentError):
        JsonConfidenceAssessmentRepository(temp_dir)


# ---------------------------------------------------------------------------
# 12. Flag de persistencia opcional en ConfidenceService
# ---------------------------------------------------------------------------

def test_confidence_service_persist_flag(policy_repo, assessment_repo, source_repo, provenance_repo, virtual_clock):
    now = virtual_clock.now()
    src = RegisteredSource(
        source_id="src-meli",
        name="MercadoLibre API",
        source_type=SourceType.MARKETPLACE_API,
        provider="mercadolibre",
        canonical_identifier="marketplace_api:mercadolibre:live",
        created_at=now,
        updated_at=now,
    )
    source_repo.save_source(src)
    prov = ProvenanceRecord(
        provenance_id="prov-200",
        source_id="src-meli",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-200",
        captured_at=now,
    )
    provenance_repo.save_provenance(prov)
    policy_repo.save_policy(ConfidencePolicy(
        policy_id="pol-default",
        name="Default",
        factor_scores=COMPLETE_FACTOR_SCORES,
    ))

    service = ConfidenceService(
        policy_repository=policy_repo,
        assessment_repository=assessment_repo,
        source_registry=source_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
    )

    # persist=False -> assessment is evaluated but not written to repo
    eval_no_persist = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-200",
        source_id="src-meli",
        provenance_id="prov-200",
        persist=False,
    )
    assert eval_no_persist is not None
    assert assessment_repo.get_assessment(eval_no_persist.assessment_id) is None

    # persist=True -> assessment is saved
    eval_persist = service.assess(
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="obs-200",
        source_id="src-meli",
        provenance_id="prov-200",
        persist=True,
    )
    assert assessment_repo.get_assessment(eval_persist.assessment_id) is not None


# ---------------------------------------------------------------------------
# 13. Prevención de Path Traversal en Identificadores
# ---------------------------------------------------------------------------

def test_path_traversal_prevention_in_confidence():
    # Policy ID with traversal
    with pytest.raises(ValueError, match="path traversal"):
        ConfidencePolicy(policy_id="../../etc/passwd", name="Bad Policy")

    # Assessment ID with traversal
    with pytest.raises(ValueError, match="path traversal"):
        ConfidenceAssessment(
            assessment_id="../secret_file",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="obs-01",
            level=ConfidenceLevel.HIGH,
            reason="Test",
            evaluated_at=datetime.now(timezone.utc),
            policy_id="pol-default",
        )
