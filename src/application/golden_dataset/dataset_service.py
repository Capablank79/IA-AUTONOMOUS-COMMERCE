"""
Servicio de Aplicación para Golden Datasets (Hito K.5).

Responsabilidades:
- Gestionar el ciclo de vida, versionado, curación y persistencia de Golden Datasets.
- Resolver referencias a EvaluationCase K.4 contra EvaluationRepositoryPort.
- Proporcionar la interfaz canónica para alimentar a EvaluationHarnessService K.4.
- Preservar inmutabilidad de versiones históricas.
- Garantizar reproducibilidad e idempotencia de carga.
- NO evalúa directamente ni aplica Quality Gates de release blocking (responsabilidad de K.4 y K.6 respectivamente).
"""

from datetime import datetime, timezone
import logging
from typing import Optional, List, Dict, Any, Sequence, Tuple, Union, Callable

from src.domain.golden_dataset.models import (
    GoldenDataset,
    GoldenDatasetManifest,
    DatasetCaseReference,
    GoldenDatasetStatus,
    GoldenDatasetProvenance,
    GoldenDatasetCurator,
    GoldenDatasetCuratorType,
    compute_dataset_manifest_checksum,
    compute_case_criteria_hash,
    compute_case_fingerprint,
)
from src.domain.golden_dataset.ports import (
    GoldenDatasetRepositoryPort,
    GoldenDatasetValidatorPort,
)
from src.domain.evaluation.models import EvaluationCase, BatchEvaluationSummary
from src.domain.evaluation.ports import EvaluationRepositoryPort, EvaluationTargetPort
from src.application.evaluation.evaluation_harness_service import EvaluationHarnessService
from src.application.golden_dataset.dataset_validator import DeterministicGoldenDatasetValidator

logger = logging.getLogger(__name__)


class GoldenDatasetService:
    """
    Servicio principal de gestión de Golden Datasets (K.5).
    """

    def __init__(
        self,
        dataset_repository: GoldenDatasetRepositoryPort,
        evaluation_repository: EvaluationRepositoryPort,
        validator: Optional[GoldenDatasetValidatorPort] = None,
        harness_service: Optional[EvaluationHarnessService] = None,
    ):
        self.dataset_repository = dataset_repository
        self.evaluation_repository = evaluation_repository
        self.validator = validator or DeterministicGoldenDatasetValidator()
        self.harness_service = harness_service

    def create_dataset_from_cases(
        self,
        dataset_id: str,
        name: str,
        description: str,
        version: str,
        cases: Sequence[EvaluationCase],
        domain_scope: str = "",
        tags: Sequence[str] = (),
        curator: Optional[GoldenDatasetCurator] = None,
        provenance: GoldenDatasetProvenance = GoldenDatasetProvenance.MANUAL_CURATED,
        status: GoldenDatasetStatus = GoldenDatasetStatus.VALIDATED,
        schema_version: str = "1.0.0",
        metadata: Optional[Dict[str, Any]] = None,
        baseline_metrics: Optional[Dict[str, Any]] = None,
        persist_cases_in_eval_repo: bool = True,
    ) -> GoldenDataset:
        """
        Crea, valida y persiste un GoldenDataset a partir de una secuencia de EvaluationCase.
        """
        if persist_cases_in_eval_repo:
            for case in cases:
                self.evaluation_repository.save_case(case)

        # Construir referencias canónicas ordenadas deterministamente por case_id
        sorted_input_cases = sorted(cases, key=lambda c: (c.case_id, c.version))
        case_refs = []
        for c in sorted_input_cases:
            c_hash = compute_case_criteria_hash(c.expected_criteria)
            ref = DatasetCaseReference(
                case_id=c.case_id,
                case_version=c.version,
                evaluation_type=c.evaluation_type.value,
                tags=tuple(c.tags),
                expected_criteria_hash=c_hash,
                case_fingerprint=compute_case_fingerprint(c),
            )
            case_refs.append(ref)

        checksum = compute_dataset_manifest_checksum(
            dataset_id=dataset_id,
            version=version,
            schema_version=schema_version,
            case_references=case_refs,
            domain_scope=domain_scope,
            tags=tags,
            baseline_metrics=baseline_metrics,
        )

        curator_obj = curator or GoldenDatasetCurator(
            curator_type=GoldenDatasetCuratorType.SYSTEM,
            curator_id="golden_dataset_service",
        )

        manifest = GoldenDatasetManifest(
            dataset_id=dataset_id,
            version=version,
            schema_version=schema_version,
            checksum=checksum,
            case_references=tuple(case_refs),
            domain_scope=domain_scope,
            tags=tuple(tags),
            baseline_metrics=baseline_metrics or {},
            provenance=provenance,
            curator=curator_obj,
            created_at=datetime.now(timezone.utc),
            metadata=metadata or {},
        )

        dataset = GoldenDataset(
            dataset_id=dataset_id,
            name=name,
            description=description,
            version=version,
            schema_version=schema_version,
            status=status,
            manifest=manifest,
            domain_scope=domain_scope,
            tags=tuple(tags),
            curator=curator_obj,
            provenance=provenance,
            created_at=manifest.created_at,
            curated_at=manifest.created_at,
            metadata=metadata or {},
        )

        # Validar dataset deterministamente
        val_res = self.validator.validate(dataset, resolved_cases=cases)
        if not val_res["is_valid"]:
            raise ValueError(f"GoldenDataset validation failed: {val_res['errors']}")

        return self.dataset_repository.save_dataset(dataset)

    def get_dataset(self, dataset_id: str, version: Optional[str] = None) -> Optional[GoldenDataset]:
        """Obtiene un dataset persistido."""
        return self.dataset_repository.get_dataset(dataset_id=dataset_id, version=version)

    def list_datasets(
        self,
        domain_scope: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[GoldenDatasetStatus] = None,
        limit: int = 100,
    ) -> List[GoldenDataset]:
        """Lista datasets según filtros."""
        return self.dataset_repository.list_datasets(
            domain_scope=domain_scope,
            tag=tag,
            status=status,
            limit=limit,
        )

    def list_versions(self, dataset_id: str) -> List[str]:
        """Lista las versiones disponibles de un dataset."""
        return self.dataset_repository.list_versions(dataset_id)

    def resolve_cases(self, dataset: GoldenDataset) -> List[EvaluationCase]:
        """
        Resuelve y carga los EvaluationCase K.4 referenciados en el dataset de forma determinista y ordenada.
        """
        resolved_cases: List[EvaluationCase] = []
        missing_cases: List[str] = []

        for ref in dataset.manifest.case_references:
            case = self.evaluation_repository.get_case(ref.case_id)
            if case is None:
                missing_cases.append(ref.case_id)
            else:
                if case.version != ref.case_version:
                    raise ValueError(
                        f"Case version mismatch for {ref.case_id}: expected {ref.case_version}, found {case.version}"
                    )
                expected_fingerprint = ref.case_fingerprint or ref.expected_criteria_hash
                actual_fingerprint = (
                    compute_case_fingerprint(case)
                    if ref.case_fingerprint
                    else compute_case_criteria_hash(case.expected_criteria)
                )
                if expected_fingerprint and actual_fingerprint != expected_fingerprint:
                    raise ValueError(f"Case fingerprint mismatch for {ref.case_id}")
                resolved_cases.append(case)

        if missing_cases:
            raise ValueError(
                f"Missing EvaluationCases for dataset {dataset.dataset_id} v{dataset.version}: {missing_cases}"
            )

        return resolved_cases

    def evaluate_dataset(
        self,
        dataset_id: str,
        target: Union[EvaluationTargetPort, Callable[[EvaluationCase], Any]],
        version: Optional[str] = None,
        harness_service: Optional[EvaluationHarnessService] = None,
    ) -> BatchEvaluationSummary:
        """
        Carga el Golden Dataset canónico, resuelve sus EvaluationCases en orden determinista,
        y delega la ejecución del lote a K.4 EvaluationHarnessService.run_batch().
        """
        dataset = self.get_dataset(dataset_id=dataset_id, version=version)
        if dataset is None:
            ver_str = f" v{version}" if version else ""
            raise ValueError(f"GoldenDataset {dataset_id}{ver_str} not found")

        # Resolver casos
        cases = self.resolve_cases(dataset)

        # Usar harness service inyectado o local
        active_harness = harness_service or self.harness_service
        if not active_harness:
            active_harness = EvaluationHarnessService(repository=self.evaluation_repository)

        # Ejecutar lote en K.4 Evaluation Harness
        return active_harness.run_batch(cases=cases, target=target, persist_cases=False)
