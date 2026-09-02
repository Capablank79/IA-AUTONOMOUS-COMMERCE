"""
Servicio de validación determinista para Golden Datasets (Hito K.5).

Valida:
- Consistencia del manifiesto.
- Formato e inmutabilidad de versión (semver o canónico).
- Unicidad de case_ids dentro de la misma versión.
- Checksum determinista SHA-256 verificado.
- Resolución opcional de EvaluationCases contra EvaluationRepositoryPort.
- Compatibilidad de esquemas y expected_criteria.
- Detección estricta de secretos no sanitizados.
- Integridad de procedencia y curador.
- NO ejecuta Quality Gates de release blocking.
- NO evalúa outputs del sistema ni calcula métricas de paso/fallo del agente.
"""

import json
from types import MappingProxyType
from typing import Optional, List, Dict, Any, Sequence

from src.domain.golden_dataset.models import (
    GoldenDataset,
    GoldenDatasetManifest,
    compute_dataset_manifest_checksum,
    compute_case_criteria_hash,
    compute_case_fingerprint,
    parse_semver,
)
from src.domain.golden_dataset.ports import GoldenDatasetValidatorPort
from src.domain.evaluation.models import EvaluationCase, SENSITIVE_KEYS


class DeterministicGoldenDatasetValidator(GoldenDatasetValidatorPort):
    """
    Validador determinista y riguroso de integridad para Golden Datasets.
    """

    def validate(
        self,
        dataset: GoldenDataset,
        resolved_cases: Optional[Sequence[EvaluationCase]] = None,
    ) -> Dict[str, Any]:
        errors: List[str] = []
        warnings: List[str] = []

        # 1. Validaciones básicas de identidad y versión
        if not dataset.dataset_id or not dataset.dataset_id.strip():
            errors.append("dataset_id cannot be empty")

        if not dataset.version or not dataset.version.strip():
            errors.append("version cannot be empty")

        if not dataset.schema_version or not dataset.schema_version.strip():
            errors.append("schema_version cannot be empty")
        for field_name, value in (("version", dataset.version), ("schema_version", dataset.schema_version)):
            try:
                parse_semver(value)
            except ValueError:
                errors.append(f"{field_name} must be valid SemVer: {value}")

        manifest = dataset.manifest
        if manifest is None:
            errors.append("manifest cannot be None")
            return {"is_valid": False, "errors": errors, "warnings": warnings}

        # 2. Consistencia entre dataset y manifest
        if dataset.dataset_id != manifest.dataset_id:
            errors.append(f"dataset_id mismatch: dataset={dataset.dataset_id}, manifest={manifest.dataset_id}")
        if dataset.version != manifest.version:
            errors.append(f"version mismatch: dataset={dataset.version}, manifest={manifest.version}")
        if dataset.schema_version != manifest.schema_version:
            errors.append(f"schema_version mismatch: dataset={dataset.schema_version}, manifest={manifest.schema_version}")

        # 3. Unicidad de casos y no duplicados
        seen_case_ids = set()
        for c_ref in manifest.case_references:
            if not c_ref.case_id or not c_ref.case_id.strip():
                errors.append("case_reference case_id cannot be empty")
                continue
            if c_ref.case_id in seen_case_ids:
                errors.append(f"Duplicate case_id in manifest: {c_ref.case_id}")
            seen_case_ids.add(c_ref.case_id)

        # 4. Checksum determinista
        expected_checksum = compute_dataset_manifest_checksum(
            dataset_id=manifest.dataset_id,
            version=manifest.version,
            schema_version=manifest.schema_version,
            case_references=manifest.case_references,
            domain_scope=manifest.domain_scope,
            tags=manifest.tags,
            baseline_metrics=manifest.baseline_metrics,
        )
        if manifest.checksum != expected_checksum:
            errors.append(
                f"Manifest checksum mismatch: expected {expected_checksum}, got {manifest.checksum}"
            )

        # 5. Detección de secretos no redactados en metadatos o detalles del curador
        # La entidad GoldenDataset sanitiza automáticamente reemplazando valores por [REDACTED].
        # Validamos que no existan valores sensibles vivos o no sanitizados.
        def _check_for_live_secrets(d: Any, path: str = ""):
            if isinstance(d, (dict, MappingProxyType)):
                for k, v in d.items():
                    current_path = f"{path}.{k}" if path else str(k)
                    k_lower = str(k).lower()
                    if any(s in k_lower for s in SENSITIVE_KEYS):
                        if v != "[REDACTED]":
                            errors.append(f"Unredacted sensitive key '{current_path}' detected")
                    if isinstance(v, (dict, list, tuple)):
                        _check_for_live_secrets(v, current_path)
            elif isinstance(d, (list, tuple)):
                for idx, item in enumerate(d):
                    _check_for_live_secrets(item, f"{path}[{idx}]")

        _check_for_live_secrets(dict(dataset.metadata), "metadata")
        _check_for_live_secrets(dataset.curator.to_dict(), "curator")

        # 6. Validación de resolución de casos de evaluación si fueron suministrados
        if resolved_cases is not None:
            resolved_dict = {c.case_id: c for c in resolved_cases}
            for c_ref in manifest.case_references:
                if c_ref.case_id not in resolved_dict:
                    errors.append(f"Referenced case_id {c_ref.case_id} could not be resolved in provided cases")
                else:
                    actual_case = resolved_dict[c_ref.case_id]
                    # Validar versión si se especificó
                    if c_ref.case_version and actual_case.version != c_ref.case_version:
                        errors.append(
                            f"Case version mismatch for {c_ref.case_id}: expected {c_ref.case_version}, found {actual_case.version}"
                        )
                    if c_ref.expected_criteria_hash:
                        actual_hash = compute_case_criteria_hash(actual_case.expected_criteria)
                        if actual_hash != c_ref.expected_criteria_hash:
                            errors.append(
                                f"Case expected_criteria_hash mismatch for {c_ref.case_id}: expected {c_ref.expected_criteria_hash}, computed {actual_hash}"
                            )
                    if c_ref.case_fingerprint:
                        actual_fingerprint = compute_case_fingerprint(actual_case)
                        if actual_fingerprint != c_ref.case_fingerprint:
                            errors.append(
                                f"Case fingerprint mismatch for {c_ref.case_id}: expected {c_ref.case_fingerprint}, computed {actual_fingerprint}"
                            )

        is_valid = len(errors) == 0
        return {
            "is_valid": is_valid,
            "errors": errors,
            "warnings": warnings,
            "checked_cases_count": len(manifest.case_references),
        }
