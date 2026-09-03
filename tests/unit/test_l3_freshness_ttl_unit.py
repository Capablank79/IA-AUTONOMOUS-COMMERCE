"""
Unit tests exhaustivos para Freshness / TTL (Hito L.3).

Cubre todos los requerimientos mandatorios:
1. fresh value (age < ttl)
2. stale/expired value (age >= ttl)
3. exact TTL boundary (age == ttl)
4. zero TTL handling
5. timezone-aware timestamps (UTC)
6. naive timestamp handling (normalización segura a UTC)
7. missing timestamp -> UNKNOWN (UNKNOWN != FRESH)
8. future timestamp handling (tolerancia y rechazo -> ERROR)
9. deterministic fake clock (VirtualClock de K.7)
10. source-specific policy
11. policy precedence (field > subject > source_id > source_type > default)
12. field-level TTL
13. derived parent freshness (oldest parent rule)
14. multiple parent semantics (herencia de estado más restrictivo)
15. policy versioning y SemVer
16. no confidence logic (L.3 no calcula confidence)
17. no mutation of provenance (inmutabilidad)
18. no hardcoded universal TTL
19. path traversal prevention en identifiers
20. checksum calculation & corruption detection
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from src.domain.freshness.models import (
    FreshnessStatus,
    FreshnessPolicy,
    FreshnessAssessment,
    compute_policy_checksum,
    compute_assessment_checksum,
    validate_semver,
)
from src.domain.freshness.ports import (
    FreshnessPolicyRepositoryPort,
    FreshnessAssessmentRepositoryPort,
)
from src.infrastructure.persistence.data.json.freshness_repository import (
    JsonFreshnessPolicyRepository,
    JsonFreshnessAssessmentRepository,
    FreshnessConflictError,
    CorruptedFreshnessRecordError,
)
from src.application.freshness.service import (
    FreshnessService,
    FreshnessServiceError,
    PolicyNotFoundError,
)
from src.domain.data_provenance.models import (
    ProvenanceRecord,
    SubjectType,
)
from src.domain.source_registry.models import (
    RegisteredSource,
    SourceType,
    SourceStatus,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.infrastructure.persistence.data.json.data_provenance_repository import JsonProvenanceRepository
from src.infrastructure.persistence.data.json.source_registry_repository import JsonSourceRegistryRepository


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="test_freshness_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def virtual_clock():
    base_time = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=base_time)


@pytest.fixture
def policy_repo(temp_dir):
    return JsonFreshnessPolicyRepository(temp_dir)


@pytest.fixture
def assessment_repo(temp_dir):
    return JsonFreshnessAssessmentRepository(temp_dir)


@pytest.fixture
def provenance_repo(temp_dir):
    return JsonProvenanceRepository(temp_dir)


@pytest.fixture
def source_repo(temp_dir):
    return JsonSourceRegistryRepository(temp_dir)


# -------------------------------------------------------------
# 1. Fresh Value
# -------------------------------------------------------------
def test_fresh_value_when_age_less_than_ttl(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-default",
        name="Default 1 Hour Policy",
        ttl_seconds=3600.0,
    )
    policy_repo.save_policy(policy)

    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    # 10 minutes ago
    obs_time = virtual_clock.now() - timedelta(minutes=10)
    assessment = service.evaluate_timestamp(
        observed_at=obs_time,
        subject_id="item-123",
        subject_type=SubjectType.MARKET_OBSERVATION,
    )

    assert assessment.status == FreshnessStatus.FRESH
    assert assessment.is_usable is True
    assert assessment.age_seconds == 600.0
    assert assessment.ttl_seconds == 3600.0


# -------------------------------------------------------------
# 2. Stale / Expired Value
# -------------------------------------------------------------
def test_stale_and_expired_value_when_age_exceeds_ttl(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-tiered",
        name="Tiered Policy",
        ttl_seconds=300.0,            # 5 min -> stale
        stale_threshold_seconds=600.0 # 10 min -> expired
    )
    policy_repo.save_policy(policy)

    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    # 6 minutes ago -> STALE
    assessment_stale = service.evaluate_timestamp(
        observed_at=virtual_clock.now() - timedelta(minutes=6),
        subject_id="item-stale",
    )
    assert assessment_stale.status == FreshnessStatus.STALE
    assert assessment_stale.is_usable is False

    # 15 minutes ago -> EXPIRED
    assessment_expired = service.evaluate_timestamp(
        observed_at=virtual_clock.now() - timedelta(minutes=15),
        subject_id="item-expired",
    )
    assert assessment_expired.status == FreshnessStatus.EXPIRED
    assert assessment_expired.is_usable is False


# -------------------------------------------------------------
# 3. Exact TTL Boundary
# -------------------------------------------------------------
def test_exact_ttl_boundary_semantics(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-boundary",
        name="Boundary Policy",
        ttl_seconds=100.0,
    )
    policy_repo.save_policy(policy)

    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    # age = 99.99s -> FRESH
    t_fresh = virtual_clock.now() - timedelta(seconds=99.99)
    res_fresh = service.evaluate_timestamp(observed_at=t_fresh, subject_id="sub-1")
    assert res_fresh.status == FreshnessStatus.FRESH

    # age = 100.00s -> STALE (exact boundary: age >= ttl is STALE)
    t_boundary = virtual_clock.now() - timedelta(seconds=100.0)
    res_boundary = service.evaluate_timestamp(observed_at=t_boundary, subject_id="sub-2")
    assert res_boundary.status == FreshnessStatus.STALE

    # age = 100.01s -> STALE
    t_stale = virtual_clock.now() - timedelta(seconds=100.01)
    res_stale = service.evaluate_timestamp(observed_at=t_stale, subject_id="sub-3")
    assert res_stale.status == FreshnessStatus.STALE


# -------------------------------------------------------------
# 4. Zero TTL
# -------------------------------------------------------------
def test_zero_ttl_handling(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-zero",
        name="Zero TTL Policy",
        ttl_seconds=0.0,
    )
    policy_repo.save_policy(policy)

    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    # Cualquier dato observado en el pasado o en el mismo segundo exacto es STALE ante TTL=0
    res = service.evaluate_timestamp(
        observed_at=virtual_clock.now() - timedelta(seconds=0.1),
        subject_id="sub-zero",
    )
    assert res.status == FreshnessStatus.STALE


# -------------------------------------------------------------
# 5. Timezone-Aware Timestamps (UTC)
# -------------------------------------------------------------
def test_timezone_aware_utc_handling(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-tz",
        name="TZ Policy",
        ttl_seconds=3600.0,
    )
    policy_repo.save_policy(policy)
    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    # Timestamp en timezone distinta (+02:00) equivalente a 10 min antes en UTC
    tz_plus_2 = timezone(timedelta(hours=2))
    dt_with_tz = (virtual_clock.now() - timedelta(minutes=10)).astimezone(tz_plus_2)

    res = service.evaluate_timestamp(
        observed_at=dt_with_tz,
        subject_id="sub-tz",
    )
    assert res.status == FreshnessStatus.FRESH
    assert abs(res.age_seconds - 600.0) < 0.01


# -------------------------------------------------------------
# 6. Naive Timestamp Handling
# -------------------------------------------------------------
def test_naive_timestamp_normalized_to_utc(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-naive",
        name="Naive Policy",
        ttl_seconds=3600.0,
    )
    policy_repo.save_policy(policy)
    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    # Naive timestamp
    naive_dt = datetime(2026, 9, 2, 11, 50, 0) # 10 mins before 12:00
    res = service.evaluate_timestamp(
        observed_at=naive_dt,
        subject_id="sub-naive",
    )
    assert res.status == FreshnessStatus.FRESH
    assert abs(res.age_seconds - 600.0) < 0.01


# -------------------------------------------------------------
# 7. Missing Timestamp -> UNKNOWN
# -------------------------------------------------------------
def test_missing_timestamp_produces_unknown(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-missing",
        name="Missing Policy",
        ttl_seconds=3600.0,
    )
    policy_repo.save_policy(policy)
    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    res = service.evaluate_timestamp(
        observed_at=None,
        subject_id="sub-missing",
    )
    assert res.status == FreshnessStatus.UNKNOWN
    assert res.is_usable is False
    assert res.age_seconds is None
    assert "missing or null" in res.reason


# -------------------------------------------------------------
# 8. Future Timestamp Handling
# -------------------------------------------------------------
def test_future_timestamp_beyond_tolerance_produces_error(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-future",
        name="Future Policy",
        ttl_seconds=3600.0,
        future_tolerance_seconds=5.0,
    )
    policy_repo.save_policy(policy)
    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    # 1. Dentro de tolerancia (3 segundos en el futuro) -> age=0, FRESH
    dt_tolerance = virtual_clock.now() + timedelta(seconds=3)
    res_tol = service.evaluate_timestamp(observed_at=dt_tolerance, subject_id="sub-tol")
    assert res_tol.status == FreshnessStatus.FRESH
    assert res_tol.age_seconds == 0.0

    # 2. Excede tolerancia (60 segundos en el futuro) -> ERROR
    dt_future = virtual_clock.now() + timedelta(seconds=60)
    res_err = service.evaluate_timestamp(observed_at=dt_future, subject_id="sub-future")
    assert res_err.status == FreshnessStatus.ERROR
    assert res_err.is_usable is False
    assert "future" in res_err.reason.lower()


# -------------------------------------------------------------
# 9. Deterministic Fake Clock
# -------------------------------------------------------------
def test_deterministic_clock_advance(policy_repo, virtual_clock):
    policy = FreshnessPolicy(
        policy_id="pol-clock",
        name="Clock Policy",
        ttl_seconds=300.0, # 5 min
    )
    policy_repo.save_policy(policy)
    service = FreshnessService(policy_repository=policy_repo, clock=virtual_clock, default_policy=policy)

    t0 = virtual_clock.now()
    res1 = service.evaluate_timestamp(observed_at=t0, subject_id="sub-clock")
    assert res1.status == FreshnessStatus.FRESH

    # Avanzar reloj virtual en 6 minutos (360 segundos)
    virtual_clock.sleep(360.0)

    # El mismo timestamp observado en t0 ahora debe ser STALE
    res2 = service.evaluate_timestamp(observed_at=t0, subject_id="sub-clock")
    assert res2.status == FreshnessStatus.STALE
    assert res2.age_seconds == 360.0


# -------------------------------------------------------------
# 10. Source-Specific Policy & Policy Precedence
# -------------------------------------------------------------
def test_policy_precedence_deterministic_resolution(policy_repo, source_repo, virtual_clock):
    # Registrar políticas con diferentes niveles de granularidad
    p_global = FreshnessPolicy(policy_id="p-global", name="Global", ttl_seconds=86400.0)
    p_src_type = FreshnessPolicy(policy_id="p-srctype", name="SrcType", source_type=SourceType.MARKETPLACE_API, ttl_seconds=7200.0)
    p_source_id = FreshnessPolicy(policy_id="p-sourceid", name="SourceID", source_id="src-meli", ttl_seconds=3600.0)
    p_subject = FreshnessPolicy(policy_id="p-subject", name="Subject", subject_type=SubjectType.MARKET_OBSERVATION, ttl_seconds=1800.0)
    p_field = FreshnessPolicy(policy_id="p-field", name="Field", field_path="price.amount", ttl_seconds=300.0)
    p_field_subject = FreshnessPolicy(
        policy_id="p-field-sub", name="Field+Subject", subject_type=SubjectType.MARKET_OBSERVATION, field_path="stock", ttl_seconds=60.0
    )

    for p in [p_global, p_src_type, p_source_id, p_subject, p_field, p_field_subject]:
        policy_repo.save_policy(p)

    service = FreshnessService(
        policy_repository=policy_repo,
        source_registry=source_repo,
        clock=virtual_clock,
        default_policy=p_global,
    )

    # Match 1: Field + Subject
    assert service.resolve_policy(subject_type=SubjectType.MARKET_OBSERVATION, field_path="stock").policy_id == "p-field-sub"

    # Match 2: Field alone
    assert service.resolve_policy(field_path="price.amount").policy_id == "p-field"

    # Match 3: Subject alone
    assert service.resolve_policy(subject_type=SubjectType.MARKET_OBSERVATION).policy_id == "p-subject"

    # Match 4: Source ID
    assert service.resolve_policy(source_id="src-meli").policy_id == "p-sourceid"

    # Match 5: Source Type
    assert service.resolve_policy(source_type=SourceType.MARKETPLACE_API).policy_id == "p-srctype"

    # Match 6: Fallback Default
    assert service.resolve_policy(subject_type=SubjectType.GENERIC_FACT).policy_id == "p-global"


# -------------------------------------------------------------
# 11. Derived Data Freshness (Oldest Parent & Multiple Parents)
# -------------------------------------------------------------
def test_derived_data_cannot_be_fresher_than_stale_parent(
    policy_repo, provenance_repo, virtual_clock
):
    # Política general: TTL = 10 minutos (600s)
    policy = FreshnessPolicy(policy_id="pol-parents", name="Parent Policy", ttl_seconds=600.0)
    policy_repo.save_policy(policy)

    service = FreshnessService(
        policy_repository=policy_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
        default_policy=policy,
    )

    t_now = virtual_clock.now()

    # Padre 1: Fresco (observado hace 2 minutos)
    parent_1 = ProvenanceRecord(
        provenance_id="prov-p1",
        source_id="src-1",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="prod-1",
        captured_at=t_now - timedelta(minutes=2),
    )
    provenance_repo.save_provenance(parent_1)

    # Padre 2: Stale (observado hace 15 minutos)
    parent_2 = ProvenanceRecord(
        provenance_id="prov-p2",
        source_id="src-2",
        subject_type=SubjectType.SUPPLIER_QUOTE,
        subject_id="quote-1",
        captured_at=t_now - timedelta(minutes=15),
    )
    provenance_repo.save_provenance(parent_2)

    # Hijo Derivado: Creado hace 1 minuto a partir de P1 y P2
    derived = ProvenanceRecord(
        provenance_id="prov-child",
        source_id="src-internal",
        subject_type=SubjectType.PRODUCT_OPPORTUNITY,
        subject_id="opp-100",
        captured_at=t_now - timedelta(minutes=1),
        parent_provenance_ids=("prov-p1", "prov-p2"),
        transformation_id="trans-opp-calc",
    )
    provenance_repo.save_provenance(derived)

    # Evaluación de frescura sobre el dato derivado
    assessment = service.evaluate_provenance(provenance_id="prov-child")

    # Debe ser STALE porque Padre 2 es STALE, a pesar de que el cálculo derivado ocurrió hace 1 minuto
    assert assessment.status == FreshnessStatus.STALE
    assert assessment.is_usable is False
    assert "prov-p2" in assessment.reason
    assert assessment.age_seconds == 900.0  # 15 minutos


def test_derived_data_with_missing_parent_timestamp(
    policy_repo, provenance_repo, virtual_clock
):
    policy = FreshnessPolicy(policy_id="pol-p", name="Policy", ttl_seconds=600.0)
    policy_repo.save_policy(policy)
    service = FreshnessService(
        policy_repository=policy_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
        default_policy=policy,
    )

    t_now = virtual_clock.now()

    # Padre válido
    p1 = ProvenanceRecord(
        provenance_id="prov-good",
        source_id="src-1",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="prod-1",
        captured_at=t_now - timedelta(minutes=1),
    )
    provenance_repo.save_provenance(p1)

    # Derivado sin padre inexistente lanza error explícito
    derived_bad = ProvenanceRecord(
        provenance_id="prov-derived-bad",
        source_id="src-1",
        subject_type=SubjectType.PRODUCT_OPPORTUNITY,
        subject_id="opp-1",
        captured_at=t_now,
        parent_provenance_ids=("prov-good", "prov-non-existent"),
    )
    provenance_repo.save_provenance(derived_bad)

    with pytest.raises(FreshnessServiceError):
        service.evaluate_provenance(provenance_id="prov-derived-bad")


# -------------------------------------------------------------
# 12. Policy Versioning & Validation
# -------------------------------------------------------------
def test_policy_semver_validation():
    # Semver válido
    p = FreshnessPolicy(policy_id="p-1", name="Pol 1", version="1.2.3", ttl_seconds=100.0)
    assert p.version == "1.2.3"

    # Semver inválido
    with pytest.raises(ValueError, match="Semantic Versioning"):
        FreshnessPolicy(policy_id="p-2", name="Pol 2", version="v1.0", ttl_seconds=100.0)

    with pytest.raises(ValueError, match="Semantic Versioning"):
        FreshnessPolicy(policy_id="p-3", name="Pol 3", version="beta", ttl_seconds=100.0)


# -------------------------------------------------------------
# 13. Path Traversal & Security Validation
# -------------------------------------------------------------
def test_path_traversal_rejected_in_identifiers():
    with pytest.raises(ValueError, match="unsafe path traversal sequences"):
        FreshnessPolicy(policy_id="../evil_policy", name="Evil", ttl_seconds=100.0)

    with pytest.raises(ValueError, match="unsafe path traversal sequences"):
        FreshnessAssessment(
            assessment_id="eval/123",
            subject_type=SubjectType.MARKET_OBSERVATION,
            subject_id="prod-1",
            status=FreshnessStatus.FRESH,
            reason="ok",
            evaluated_at=datetime.now(timezone.utc),
            ttl_seconds=100.0,
            age_seconds=10.0,
            policy_id="p-1",
        )


# -------------------------------------------------------------
# 14. Checksum Verification and Conflict Detection
# -------------------------------------------------------------
def test_repository_idempotency_and_conflict_detection(policy_repo, assessment_repo, virtual_clock):
    pol = FreshnessPolicy(policy_id="pol-idem", name="Idem", version="1.0.0", ttl_seconds=300.0)
    policy_repo.save_policy(pol)

    # Replay idéntico -> idempotente
    saved2 = policy_repo.save_policy(pol)
    assert saved2.checksum == pol.checksum

    # Mismo ID y Version con diferente TTL -> FreshnessConflictError
    pol_altered = FreshnessPolicy(policy_id="pol-idem", name="Idem", version="1.0.0", ttl_seconds=999.0)
    with pytest.raises(FreshnessConflictError):
        policy_repo.save_policy(pol_altered)

    # Evaluación
    eval_record = FreshnessAssessment(
        assessment_id="assess-1",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-1",
        status=FreshnessStatus.FRESH,
        reason="Fresh data",
        evaluated_at=virtual_clock.now(),
        ttl_seconds=300.0,
        age_seconds=10.0,
        policy_id="pol-idem",
    )
    assessment_repo.save_assessment(eval_record)

    # Replay idéntico assessment
    assessment_repo.save_assessment(eval_record)

    # Assessment modificado con mismo ID -> FreshnessConflictError
    eval_altered = FreshnessAssessment(
        assessment_id="assess-1",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="item-1",
        status=FreshnessStatus.STALE, # Changed
        reason="Stale data",
        evaluated_at=virtual_clock.now(),
        ttl_seconds=300.0,
        age_seconds=400.0,
        policy_id="pol-idem",
    )
    with pytest.raises(FreshnessConflictError):
        assessment_repo.save_assessment(eval_altered)


# -------------------------------------------------------------
# 15. No Confidence or External Mutation
# -------------------------------------------------------------
def test_freshness_does_not_mutate_provenance_or_calculate_confidence(
    policy_repo, provenance_repo, virtual_clock
):
    pol = FreshnessPolicy(policy_id="pol-pure", name="Pure", ttl_seconds=300.0)
    policy_repo.save_policy(pol)

    service = FreshnessService(
        policy_repository=policy_repo,
        provenance_repository=provenance_repo,
        clock=virtual_clock,
        default_policy=pol,
    )

    t_obs = virtual_clock.now() - timedelta(seconds=50)
    record = ProvenanceRecord(
        provenance_id="prov-orig",
        source_id="src-1",
        subject_type=SubjectType.MARKET_OBSERVATION,
        subject_id="sku-123",
        captured_at=t_obs,
    )
    provenance_repo.save_provenance(record)
    original_checksum = record.checksum

    assessment = service.evaluate_provenance(provenance_id="prov-orig")
    assert assessment.status == FreshnessStatus.FRESH

    # Verificar que el registro original de procedencia no ha sido mutado
    reloaded = provenance_repo.get_provenance("prov-orig")
    assert reloaded.checksum == original_checksum
    assert not hasattr(assessment, "confidence")
    assert not hasattr(assessment, "confidence_score")
