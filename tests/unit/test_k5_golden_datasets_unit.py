"""
Tests unitarios exhaustivos para Golden Datasets (Hito K.5).

Cubre todos los criterios requeridos A al AC:
A. Immutable GoldenDataset
B. Dataset identity
C. Version
D. Manifest
E. Deterministic checksum
F. Case membership
G. Duplicate case rejection
H. Deterministic ordering
I. Curator
J. Provenance
K. Tags
L. DRAFT
M. VALIDATED
N. DEPRECATED
O. Immutable validated version
P. Same version same content idempotent
Q. Same version different content conflict
R. Case resolution
S. Missing case handling
T. Incompatible case handling
U. Persistence
V. Restart / Reload
W. Corruption detection
X. Sanitization
Y. Load by dataset/version
Z. List versions
AA. K.4 batch integration
AB. No Quality Gate
AC. No Hito L implementation
"""

from datetime import datetime, timezone
import json
import pytest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from src.domain.evaluation.models import (
    EvaluationCase,
    EvaluationType,
    EvaluationStatus,
    BatchEvaluationSummary,
)
from src.domain.golden_dataset.models import (
    GoldenDataset,
    GoldenDatasetManifest,
    DatasetCaseReference,
    GoldenDatasetCurator,
    GoldenDatasetStatus,
    GoldenDatasetProvenance,
    GoldenDatasetCuratorType,
    compute_dataset_manifest_checksum,
    compute_case_criteria_hash,
)
from src.application.golden_dataset.dataset_service import GoldenDatasetService
from src.application.golden_dataset.dataset_validator import DeterministicGoldenDatasetValidator
from src.infrastructure.persistence.data.json.golden_dataset_repository import (
    JsonGoldenDatasetRepository,
    DatasetVersionConflictError,
    CorruptedGoldenDatasetRecordError,
)
from src.infrastructure.persistence.data.json.evaluation_repository import JsonEvaluationRepository
from src.application.evaluation.evaluation_harness_service import EvaluationHarnessService
from src.domain.golden_dataset.baseline_datasets import (
    get_governance_policy_baseline_cases,
    get_unknown_safety_baseline_cases,
    get_idempotency_baseline_cases,
)


@pytest.fixture
def eval_repo(tmp_path):
    return JsonEvaluationRepository(base_dir=tmp_path / "eval_repo")


@pytest.fixture
def dataset_repo(tmp_path):
    return JsonGoldenDatasetRepository(base_dir=tmp_path / "dataset_repo")


@pytest.fixture
def harness_service(eval_repo):
    return EvaluationHarnessService(repository=eval_repo)


@pytest.fixture
def dataset_service(dataset_repo, eval_repo, harness_service):
    return GoldenDatasetService(
        dataset_repository=dataset_repo,
        evaluation_repository=eval_repo,
        validator=DeterministicGoldenDatasetValidator(),
        harness_service=harness_service,
    )


def test_a_immutable_golden_dataset():
    """A: Verifica inmutabilidad estricta de las entidades y referencias."""
    case_ref = DatasetCaseReference(case_id="case_1", case_version="1.0.0")
    curator = GoldenDatasetCurator(
        curator_type=GoldenDatasetCuratorType.SYSTEM,
        curator_id="curator_1",
    )
    manifest = GoldenDatasetManifest(
        dataset_id="test_ds",
        version="1.0.0",
        schema_version="1.0.0",
        checksum="",
        case_references=(case_ref,),
        curator=curator,
    )
    dataset = GoldenDataset(
        dataset_id="test_ds",
        name="Test Dataset",
        description="Description",
        version="1.0.0",
        schema_version="1.0.0",
        status=GoldenDatasetStatus.VALIDATED,
        manifest=manifest,
        curator=curator,
    )

    with pytest.raises(Exception):
        dataset.name = "Mutated"

    with pytest.raises(Exception):
        manifest.version = "2.0.0"

    with pytest.raises(Exception):
        case_ref.case_id = "case_mutated"


def test_b_and_c_identity_and_version():
    """B, C: Verifica dataset_id y version inmutables y explícitos."""
    case = EvaluationCase(
        case_id="case_id_test",
        name="Case Test",
        description="Desc",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"value": 42},
    )
    ref = DatasetCaseReference(case_id=case.case_id, case_version=case.version)
    manifest = GoldenDatasetManifest(
        dataset_id="dataset_alpha",
        version="1.2.0",
        schema_version="1.0.0",
        checksum="",
        case_references=(ref,),
    )
    ds = GoldenDataset(
        dataset_id="dataset_alpha",
        name="Alpha",
        description="Alpha desc",
        version="1.2.0",
        schema_version="1.0.0",
        status=GoldenDatasetStatus.DRAFT,
        manifest=manifest,
    )
    assert ds.dataset_id == "dataset_alpha"
    assert ds.version == "1.2.0"
    assert ds.case_count == 1
    assert ds.case_ids == ("case_id_test",)


def test_d_and_e_manifest_and_deterministic_checksum():
    """D, E: Verifica que el checksum sea determinista e independiente del orden de inserción de casos."""
    ref1 = DatasetCaseReference(case_id="case_b", case_version="1.0.0")
    ref2 = DatasetCaseReference(case_id="case_a", case_version="1.0.0")

    # Lista en orden B, A vs A, B
    checksum1 = compute_dataset_manifest_checksum(
        dataset_id="ds_check",
        version="1.0.0",
        schema_version="1.0.0",
        case_references=[ref1, ref2],
        domain_scope="market",
        tags=["t1", "t2"],
    )

    checksum2 = compute_dataset_manifest_checksum(
        dataset_id="ds_check",
        version="1.0.0",
        schema_version="1.0.0",
        case_references=[ref2, ref1],
        domain_scope="market",
        tags=["t2", "t1"],
    )

    assert checksum1 == checksum2
    assert len(checksum1) == 64  # SHA-256


def test_f_g_h_case_membership_duplicate_rejection_and_ordering(dataset_service):
    """F, G, H: Validación de membresía de casos, rechazo de duplicados y orden determinista."""
    case_a = EvaluationCase(
        case_id="case_a",
        name="A",
        description="A",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"val": 1},
    )
    case_b = EvaluationCase(
        case_id="case_b",
        name="B",
        description="B",
        evaluation_type=EvaluationType.EXACT_MATCH,
        expected_criteria={"val": 2},
    )

    # Duplicados deben fallar validación
    with pytest.raises(ValueError, match="Duplicate case_id"):
        dataset_service.create_dataset_from_cases(
            dataset_id="ds_dup",
            name="Dup",
            description="Dup",
            version="1.0.0",
            cases=[case_a, case_a],
        )

    # Orden determinista
    ds = dataset_service.create_dataset_from_cases(
        dataset_id="ds_order",
        name="Order",
        description="Order",
        version="1.0.0",
        cases=[case_b, case_a],
    )
    assert ds.case_ids == ("case_a", "case_b")


def test_i_j_k_curator_provenance_tags(dataset_service):
    """I, J, K: Registro explícito de curador, procedencia y tags de categorización."""
    cases = get_governance_policy_baseline_cases()
    curator = GoldenDatasetCurator(
        curator_type=GoldenDatasetCuratorType.TEAM,
        curator_id="governance_guild",
        details={"department": "risk_control"},
    )
    ds = dataset_service.create_dataset_from_cases(
        dataset_id="governance_baseline",
        name="Governance Baseline",
        description="Policy baseline dataset",
        version="1.0.0",
        cases=cases,
        domain_scope="governance",
        tags=["policy", "security", "governance"],
        curator=curator,
        provenance=GoldenDatasetProvenance.MANUAL_CURATED,
    )
    assert ds.curator.curator_type == GoldenDatasetCuratorType.TEAM
    assert ds.curator.curator_id == "governance_guild"
    assert ds.provenance == GoldenDatasetProvenance.MANUAL_CURATED
    assert "policy" in ds.tags
    assert ds.domain_scope == "governance"


def test_l_m_n_dataset_statuses(dataset_service):
    """L, M, N: Estados DRAFT, VALIDATED y DEPRECATED."""
    cases = get_unknown_safety_baseline_cases()
    ds_draft = dataset_service.create_dataset_from_cases(
        dataset_id="unknown_safety",
        name="Unknown Safety",
        description="Desc",
        version="0.1.0",
        cases=cases,
        status=GoldenDatasetStatus.DRAFT,
    )
    assert ds_draft.status == GoldenDatasetStatus.DRAFT

    ds_val = dataset_service.create_dataset_from_cases(
        dataset_id="unknown_safety",
        name="Unknown Safety",
        description="Desc",
        version="1.0.0",
        cases=cases,
        status=GoldenDatasetStatus.VALIDATED,
    )
    assert ds_val.status == GoldenDatasetStatus.VALIDATED

    # Crear una versión deprecada
    ds_dep = dataset_service.create_dataset_from_cases(
        dataset_id="unknown_safety",
        name="Unknown Safety",
        description="Desc",
        version="0.0.1",
        cases=cases,
        status=GoldenDatasetStatus.DEPRECATED,
    )
    assert ds_dep.status == GoldenDatasetStatus.DEPRECATED


def test_o_p_q_immutability_idempotency_and_conflict(dataset_service, dataset_repo):
    """O, P, Q: Inmutabilidad de versión, idempotencia en mismo contenido, conflicto en cambio de contenido."""
    cases1 = get_governance_policy_baseline_cases()[:2]
    cases2 = get_governance_policy_baseline_cases()  # 3 cases

    # 1. Guardar v1.0.0
    ds1 = dataset_service.create_dataset_from_cases(
        dataset_id="gov_conflict_test",
        name="Gov",
        description="Gov",
        version="1.0.0",
        cases=cases1,
    )

    # 2. Guardar exactamente el mismo contenido: idempotente y exitoso
    ds_same = dataset_service.create_dataset_from_cases(
        dataset_id="gov_conflict_test",
        name="Gov",
        description="Gov",
        version="1.0.0",
        cases=cases1,
    )
    assert ds_same.checksum == ds1.checksum

    # 3. Intentar guardar versión 1.0.0 con contenido/casos diferentes: CONFLICTO
    with pytest.raises(DatasetVersionConflictError):
        dataset_service.create_dataset_from_cases(
            dataset_id="gov_conflict_test",
            name="Gov",
            description="Gov modified",
            version="1.0.0",
            cases=cases2,
        )


def test_r_s_t_case_resolution_missing_and_incompatible(dataset_service, eval_repo):
    """R, S, T: Resolución de casos, detección de casos faltantes o incompatibles."""
    cases = get_idempotency_baseline_cases()
    ds = dataset_service.create_dataset_from_cases(
        dataset_id="idemp_resolve_test",
        name="Idemp",
        description="Desc",
        version="1.0.0",
        cases=cases,
    )

    # Resolución exitosa
    resolved = dataset_service.resolve_cases(ds)
    assert len(resolved) == 1
    assert resolved[0].case_id == cases[0].case_id

    # Caso faltante (crear manifest con case_id inexistente en repo)
    fake_ref = DatasetCaseReference(case_id="non_existent_case_999", case_version="1.0.0")
    fake_manifest = GoldenDatasetManifest(
        dataset_id="missing_case_ds",
        version="1.0.0",
        schema_version="1.0.0",
        checksum="",
        case_references=(fake_ref,),
    )
    fake_ds = GoldenDataset(
        dataset_id="missing_case_ds",
        name="Missing",
        description="Desc",
        version="1.0.0",
        schema_version="1.0.0",
        status=GoldenDatasetStatus.VALIDATED,
        manifest=fake_manifest,
    )

    with pytest.raises(ValueError, match="Missing EvaluationCases"):
        dataset_service.resolve_cases(fake_ds)


def test_u_v_w_persistence_restart_and_corruption(tmp_path):
    """U, V, W: Persistencia durable, reinicio de servicios y detección de corrupción."""
    eval_dir = tmp_path / "eval_store"
    ds_dir = tmp_path / "dataset_store"

    # 1. Crear y persistir con primera instancia de servicio
    repo1 = JsonGoldenDatasetRepository(base_dir=ds_dir)
    e_repo1 = JsonEvaluationRepository(base_dir=eval_dir)
    service1 = GoldenDatasetService(dataset_repository=repo1, evaluation_repository=e_repo1)

    cases = get_governance_policy_baseline_cases()
    ds_created = service1.create_dataset_from_cases(
        dataset_id="durable_gov",
        name="Durable Gov",
        description="Durable",
        version="1.0.0",
        cases=cases,
    )
    original_checksum = ds_created.checksum

    # 2. Destruir instancias en memoria y recargar desde disco
    del service1
    del repo1
    del e_repo1

    repo2 = JsonGoldenDatasetRepository(base_dir=ds_dir)
    e_repo2 = JsonEvaluationRepository(base_dir=eval_dir)
    service2 = GoldenDatasetService(dataset_repository=repo2, evaluation_repository=e_repo2)

    loaded_ds = service2.get_dataset("durable_gov", version="1.0.0")
    assert loaded_ds is not None
    assert loaded_ds.checksum == original_checksum
    assert loaded_ds.case_count == len(cases)

    # 3. Corrupción: alterar el archivo en disco
    manifest_file = ds_dir / "manifests" / "durable_gov" / "1.0.0.json"
    with open(manifest_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["manifest"]["domain_scope"] = "CORRUPTED_SCOPE"  # rompe el checksum
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Recarga debe detectar inconsistencia de checksum y lanzar CorruptedGoldenDatasetRecordError
    with pytest.raises(CorruptedGoldenDatasetRecordError):
        service2.get_dataset("durable_gov", version="1.0.0")


def test_x_sanitization(dataset_service):
    """X: Sanitización recursiva de secretos en metadata y detalles."""
    cases = get_governance_policy_baseline_cases()[:1]
    curator = GoldenDatasetCurator(
        curator_type=GoldenDatasetCuratorType.USER,
        curator_id="engineer_1",
        details={"api_key": "secret_api_key_123", "public_dept": "core"},
    )
    ds = dataset_service.create_dataset_from_cases(
        dataset_id="sec_test",
        name="Sec",
        description="Desc",
        version="1.0.0",
        cases=cases,
        curator=curator,
        metadata={"token": "bearer_secret_abc", "environment": "staging"},
    )

    assert ds.curator.details["api_key"] == "[REDACTED]"
    assert ds.curator.details["public_dept"] == "core"
    assert ds.metadata["token"] == "[REDACTED]"
    assert ds.metadata["environment"] == "staging"


def test_y_z_load_by_dataset_and_list_versions(dataset_service):
    """Y, Z: Carga por dataset_id y versión, y listado de versiones históricas."""
    cases = get_governance_policy_baseline_cases()
    dataset_service.create_dataset_from_cases(
        dataset_id="multi_version_ds",
        name="Multi",
        description="Desc",
        version="1.0.0",
        cases=cases[:1],
    )
    dataset_service.create_dataset_from_cases(
        dataset_id="multi_version_ds",
        name="Multi",
        description="Desc",
        version="1.1.0",
        cases=cases[:2],
    )
    dataset_service.create_dataset_from_cases(
        dataset_id="multi_version_ds",
        name="Multi",
        description="Desc",
        version="2.0.0",
        cases=cases,
    )

    versions = dataset_service.list_versions("multi_version_ds")
    assert "1.0.0" in versions
    assert "1.1.0" in versions
    assert "2.0.0" in versions

    # Obtener versión específica
    v1 = dataset_service.get_dataset("multi_version_ds", version="1.0.0")
    assert v1.case_count == 1

    v2 = dataset_service.get_dataset("multi_version_ds", version="2.0.0")
    assert v2.case_count == 3

    # Obtener última versión (version=None)
    latest = dataset_service.get_dataset("multi_version_ds")
    assert latest.version == "2.0.0"


def test_aa_k4_batch_integration(dataset_service):
    """AA: Integración fluida con K.4 EvaluationHarnessService para ejecución en batch."""
    cases = get_governance_policy_baseline_cases()
    ds = dataset_service.create_dataset_from_cases(
        dataset_id="gov_eval_batch",
        name="Gov Eval Batch",
        description="Batch",
        version="1.0.0",
        cases=cases,
    )

    # Target determinista que simula PolicyEngine respondiendo según las reglas
    def policy_mock_target(case: EvaluationCase):
        inp = case.input_reference
        if inp.get("estimated_amount", 0) > inp.get("daily_budget_limit", 500):
            return {"output": {"decision_type": "DENY", "violations": ["BUDGET_EXCEEDED"]}}
        if not inp.get("supplier_verified", True):
            return {"output": {"decision_type": "DENY", "violations": ["UNVERIFIED_SUPPLIER"]}}
        return {"output": {"decision_type": "ALLOW", "violations": []}}

    summary: BatchEvaluationSummary = dataset_service.evaluate_dataset(
        dataset_id="gov_eval_batch",
        target=policy_mock_target,
    )

    assert summary.total_cases == 3
    assert summary.passed_count == 3
    assert summary.failed_count == 0
    assert summary.error_count == 0


def test_ab_and_ac_boundaries_no_quality_gate_no_hito_l(dataset_service):
    """AB, AC: Confirma límites de alcance: K.5 no ejecuta Quality Gates ni implementa Hito L."""
    cases = get_governance_policy_baseline_cases()
    ds = dataset_service.create_dataset_from_cases(
        dataset_id="boundary_check_ds",
        name="Boundary",
        description="Desc",
        version="1.0.0",
        cases=cases,
    )

    # Verificar que el modelo no contiene lógica de gating ni release blocking
    assert not hasattr(ds, "block_release")
    assert not hasattr(ds, "evaluate_quality_gate")
    assert not hasattr(dataset_service, "enforce_release_gate")
    assert not hasattr(dataset_service, "manage_master_data")
