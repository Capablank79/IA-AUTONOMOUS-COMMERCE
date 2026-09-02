"""
Tests de integración y E2E para Golden Datasets (Hito K.5).

Demuestra:
1. Creación e integración completa de EvaluationCases -> GoldenDataset -> Manifest -> Persist -> Reload -> Resolve -> K.4 run_batch.
2. E2E Governance & Policy baseline.
3. E2E UNKNOWN safety baseline.
4. E2E Idempotency baseline.
5. E2E Continuous Autonomy baseline.
6. E2E Versioning evolution (v1 -> v2 con conservación íntegra de versiones históricas).
7. E2E Restart & Deterministic Replay.
"""

from datetime import datetime, timezone
import pytest
from pathlib import Path

from src.domain.evaluation.models import (
    EvaluationCase,
    EvaluationType,
    EvaluationStatus,
    BatchEvaluationSummary,
)
from src.domain.golden_dataset.models import (
    GoldenDataset,
    GoldenDatasetStatus,
    GoldenDatasetProvenance,
    GoldenDatasetCurator,
    GoldenDatasetCuratorType,
)
from src.application.golden_dataset.dataset_service import GoldenDatasetService
from src.application.golden_dataset.dataset_validator import DeterministicGoldenDatasetValidator
from src.infrastructure.persistence.data.json.golden_dataset_repository import JsonGoldenDatasetRepository
from src.infrastructure.persistence.data.json.evaluation_repository import JsonEvaluationRepository
from src.application.evaluation.evaluation_harness_service import EvaluationHarnessService
from src.domain.golden_dataset.baseline_datasets import (
    get_governance_policy_baseline_cases,
    get_unknown_safety_baseline_cases,
    get_idempotency_baseline_cases,
    get_continuous_autonomy_baseline_cases,
    get_security_sanitization_baseline_cases,
)


@pytest.fixture
def clean_env(tmp_path):
    eval_dir = tmp_path / "k5_integration_eval"
    dataset_dir = tmp_path / "k5_integration_dataset"
    
    eval_repo = JsonEvaluationRepository(base_dir=eval_dir)
    dataset_repo = JsonGoldenDatasetRepository(base_dir=dataset_dir)
    harness = EvaluationHarnessService(repository=eval_repo)
    service = GoldenDatasetService(
        dataset_repository=dataset_repo,
        evaluation_repository=eval_repo,
        validator=DeterministicGoldenDatasetValidator(),
        harness_service=harness,
    )
    return {
        "eval_dir": eval_dir,
        "dataset_dir": dataset_dir,
        "eval_repo": eval_repo,
        "dataset_repo": dataset_repo,
        "harness": harness,
        "service": service,
    }


def test_k5_e2e_full_lifecycle_and_restart(clean_env):
    """
    Escenario 1: Ciclo de vida completo:
    crear casos -> crear manifest -> validar -> persistir -> reiniciar servicio -> resolver -> evaluar K.4 -> verificar resultados idénticos.
    """
    service: GoldenDatasetService = clean_env["service"]
    dataset_dir = clean_env["dataset_dir"]
    eval_dir = clean_env["eval_dir"]

    cases = get_governance_policy_baseline_cases()
    ds = service.create_dataset_from_cases(
        dataset_id="governance_canonical",
        name="Governance Canonical Baseline",
        description="Canonical dataset for Policy and Governance verification",
        version="1.0.0",
        cases=cases,
        domain_scope="governance",
        tags=["policy", "governance", "p0"],
    )

    initial_checksum = ds.checksum
    assert ds.case_count == 3

    # Simular reinicio completo destruyendo dependencias en memoria
    reloaded_eval_repo = JsonEvaluationRepository(base_dir=eval_dir)
    reloaded_dataset_repo = JsonGoldenDatasetRepository(base_dir=dataset_dir)
    reloaded_harness = EvaluationHarnessService(repository=reloaded_eval_repo)
    reloaded_service = GoldenDatasetService(
        dataset_repository=reloaded_dataset_repo,
        evaluation_repository=reloaded_eval_repo,
        validator=DeterministicGoldenDatasetValidator(),
        harness_service=reloaded_harness,
    )

    loaded_ds = reloaded_service.get_dataset("governance_canonical", version="1.0.0")
    assert loaded_ds is not None
    assert loaded_ds.checksum == initial_checksum
    assert loaded_ds.case_ids == ds.case_ids

    # Evaluar con K.4
    def target_policy(case: EvaluationCase):
        inp = case.input_reference
        if inp.get("estimated_amount", 0) > inp.get("daily_budget_limit", 500):
            return {"output": {"decision_type": "DENY", "violations": ["BUDGET_EXCEEDED"]}}
        if not inp.get("supplier_verified", True):
            return {"output": {"decision_type": "DENY", "violations": ["UNVERIFIED_SUPPLIER"]}}
        return {"output": {"decision_type": "ALLOW", "violations": []}}

    summary = reloaded_service.evaluate_dataset(
        dataset_id="governance_canonical",
        target=target_policy,
        version="1.0.0",
    )

    assert summary.total_cases == 3
    assert summary.passed_count == 3
    assert summary.failed_count == 0


def test_k5_e2e_governance_cases(clean_env):
    """
    Escenario 2: Golden Dataset E2E Governance (ALLOW, DENY budget, DENY supplier).
    """
    service: GoldenDatasetService = clean_env["service"]
    cases = get_governance_policy_baseline_cases()

    ds = service.create_dataset_from_cases(
        dataset_id="gov_e2e",
        name="Gov E2E",
        description="E2E test",
        version="1.0.0",
        cases=cases,
    )

    def actual_governance_engine(case: EvaluationCase):
        inp = case.input_reference
        if inp["action_type"] == "PLACE_SUPPLIER_ORDER":
            if inp.get("estimated_amount", 0) > inp.get("daily_budget_limit", 0):
                return {"output": {"decision_type": "DENY", "violations": ["BUDGET_EXCEEDED"]}}
            if not inp.get("supplier_verified", False):
                return {"output": {"decision_type": "DENY", "violations": ["UNVERIFIED_SUPPLIER"]}}
            return {"output": {"decision_type": "ALLOW", "violations": []}}
        return {"output": {"decision_type": "DENY", "violations": ["UNKNOWN_ACTION"]}}

    summary = service.evaluate_dataset("gov_e2e", target=actual_governance_engine)
    assert summary.total_cases == 3
    assert summary.passed_count == 3


def test_k5_e2e_unknown_safety(clean_env):
    """
    Escenario 3: Golden Dataset E2E UNKNOWN safety (preservación de UNKNOWN ante falta de datos).
    """
    service: GoldenDatasetService = clean_env["service"]
    cases = get_unknown_safety_baseline_cases()

    ds = service.create_dataset_from_cases(
        dataset_id="unknown_safety_e2e",
        name="Unknown Safety E2E",
        description="E2E test",
        version="1.0.0",
        cases=cases,
    )

    def market_safety_target(case: EvaluationCase):
        inp = case.input_reference
        if case.case_id == "case_unk_missing_market_price":
            # Si falta el precio, no inventa 0.00 ni SUCCESS, retorna UNKNOWN
            return {
                "output": {
                    "price_status": "UNKNOWN",
                    "safety_check": "PASS",
                    "raw_price": inp.get("price"),
                }
            }
        elif case.case_id == "case_unk_corrupted_source_stream":
            return {"output": "UNKNOWN"}
        return {"output": "UNKNOWN"}

    summary = service.evaluate_dataset("unknown_safety_e2e", target=market_safety_target)
    assert summary.total_cases == 2
    assert summary.passed_count == 2


def test_k5_e2e_idempotency(clean_env):
    """
    Escenario 4: Golden Dataset E2E Idempotency (replay de eventos duplicados).
    """
    service: GoldenDatasetService = clean_env["service"]
    cases = get_idempotency_baseline_cases()

    ds = service.create_dataset_from_cases(
        dataset_id="idemp_e2e",
        name="Idempotency E2E",
        description="E2E test",
        version="1.0.0",
        cases=cases,
    )

    # Simular procesador de eventos con deduplicación por ID produciendo efectos reproducibles
    def idempotent_event_processor(case: EvaluationCase):
        # Ambas invocaciones producen exactamente el mismo estado lógico
        run_1_effect = {"event_id": case.input_reference.get("event_id"), "processed": True, "state_version": 1}
        run_2_effect = {"event_id": case.input_reference.get("event_id"), "processed": True, "state_version": 1}

        return {
            "output": {
                "run_1": run_1_effect,
                "run_2": run_2_effect,
            }
        }

    summary = service.evaluate_dataset("idemp_e2e", target=idempotent_event_processor)
    assert summary.total_cases == 1
    assert summary.passed_count == 1


def test_k5_e2e_continuous_autonomy(clean_env):
    """
    Escenario 5: Golden Dataset E2E Continuous Autonomy (Gate I max cycles).
    """
    service: GoldenDatasetService = clean_env["service"]
    cases = get_continuous_autonomy_baseline_cases()

    ds = service.create_dataset_from_cases(
        dataset_id="autonomy_e2e",
        name="Autonomy E2E",
        description="E2E test",
        version="1.0.0",
        cases=cases,
    )

    def continuous_mission_target(case: EvaluationCase):
        inp = case.input_reference
        max_c = inp.get("max_cycles", 1)
        target_c = inp.get("target_cycles_to_run", 1)

        # Ejecutar hasta max_cycles
        executed = 0
        for _ in range(target_c):
            if executed >= max_c:
                break
            executed += 1

        return {
            "output": {
                "final_status": "COMPLETED",
                "executed_cycles": executed,
            }
        }

    summary = service.evaluate_dataset("autonomy_e2e", target=continuous_mission_target)
    assert summary.total_cases == 1
    assert summary.passed_count == 1


def test_k5_e2e_versioning_evolution(clean_env):
    """
    Escenario 6: Evolución de versiones v1.0.0 -> v2.0.0 (inmutabilidad de v1, nuevo checksum para v2, ambas resolvibles).
    """
    service: GoldenDatasetService = clean_env["service"]
    cases_v1 = get_governance_policy_baseline_cases()[:2]
    cases_v2 = get_governance_policy_baseline_cases()  # 3 casos

    # 1. Crear v1.0.0
    ds_v1 = service.create_dataset_from_cases(
        dataset_id="evolution_dataset",
        name="Evolution Dataset",
        description="Initial version",
        version="1.0.0",
        cases=cases_v1,
    )

    # 2. Crear v2.0.0 con más casos
    ds_v2 = service.create_dataset_from_cases(
        dataset_id="evolution_dataset",
        name="Evolution Dataset",
        description="Expanded version",
        version="2.0.0",
        cases=cases_v2,
    )

    assert ds_v1.checksum != ds_v2.checksum
    assert ds_v1.case_count == 2
    assert ds_v2.case_count == 3

    # Verificar que ambas versiones se pueden recuperar y evaluar independientemente
    loaded_v1 = service.get_dataset("evolution_dataset", version="1.0.0")
    loaded_v2 = service.get_dataset("evolution_dataset", version="2.0.0")

    assert loaded_v1 is not None
    assert loaded_v2 is not None
    assert loaded_v1.case_count == 2
    assert loaded_v2.case_count == 3

    # Listar versiones disponibles
    available_versions = service.list_versions("evolution_dataset")
    assert "1.0.0" in available_versions
    assert "2.0.0" in available_versions
