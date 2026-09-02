"""
Implementación JSON persistente, atómica y determinista para Golden Datasets (Hito K.5).

Garantiza:
- Atomic write (.tmp -> os.replace) con fsync.
- Inmutabilidad estricta de versiones persistidas.
- Persistencia de manifiestos estructurados y resolución de datasets.
- Idempotencia estricta por (dataset_id, version).
- Detección de conflictos si se intenta guardar con mismo (dataset_id, version) pero checksum diferente.
- Verificación de integridad por SHA-256 en lectura.
- Sanitización recursiva de datos sensibles.
- Resiliencia ante reinicios y caídas de proceso.
- Manejo determinista de estados (DRAFT, VALIDATED, DEPRECATED).
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Tuple
from contextlib import contextmanager
import threading
import time

from src.domain.golden_dataset.models import (
    GoldenDataset,
    GoldenDatasetManifest,
    DatasetCaseReference,
    GoldenDatasetCurator,
    GoldenDatasetStatus,
    GoldenDatasetProvenance,
    GoldenDatasetCuratorType,
    compute_dataset_manifest_checksum,
    semantic_version_key,
)
from src.domain.golden_dataset.ports import GoldenDatasetRepositoryPort
from src.domain.evaluation.models import SENSITIVE_KEYS

logger = logging.getLogger(__name__)


class JsonGoldenDatasetRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de golden datasets."""
    pass


class DatasetVersionConflictError(JsonGoldenDatasetRepositoryError):
    """Se lanza cuando se intenta sobrescribir una versión existente con contenido diferente."""
    pass


class CorruptedGoldenDatasetRecordError(JsonGoldenDatasetRepositoryError):
    """Se lanza cuando un archivo o manifiesto de dataset está corrupto o tiene checksum inválido."""
    pass


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
    if hasattr(val, "value"):
        return val.value
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _encode_json_value(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_encode_json_value(v) for v in val]
    return val


class JsonGoldenDatasetRepository(GoldenDatasetRepositoryPort):
    """
    Repositorio JSON persistente, atómico y versionado para Golden Datasets (K.5).
    Organización en disco:
      base_dir/
        manifests/
          {dataset_id}/
            {version}.json
        index/
          datasets_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.manifests_dir = self.base_dir / "manifests"
        self.index_dir = self.base_dir / "index"

        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.datasets_index_file = self.index_dir / "datasets_index.jsonl"
        self._thread_lock = threading.RLock()
        self._lock_file = self.base_dir / ".repository.lock"
        with self._exclusive_lock():
            self._recover_index_if_needed()

    def _recover_index_if_needed(self) -> None:
        valid_index = self.datasets_index_file.exists()
        if valid_index:
            try:
                with open(self.datasets_index_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            json.loads(line)
            except (OSError, json.JSONDecodeError):
                valid_index = False
        if valid_index:
            return

        entries = []
        for version_file in sorted(self.manifests_dir.glob("*/*.json")):
            dataset = self._load_dataset_file(version_file)
            entries.append({
                "dataset_id": dataset.dataset_id,
                "version": dataset.version,
                "name": dataset.name,
                "status": dataset.status.value,
                "checksum": dataset.checksum,
                "case_count": dataset.case_count,
                "created_at": dataset.created_at.isoformat(),
            })
        tmp_path = self.datasets_index_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.datasets_index_file)

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            deadline = time.monotonic() + 10.0
            fd = None
            while fd is None:
                try:
                    fd = os.open(self._lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    if time.monotonic() >= deadline:
                        raise JsonGoldenDatasetRepositoryError("Timed out acquiring repository lock")
                    time.sleep(0.01)
            try:
                yield
            finally:
                os.close(fd)
                try:
                    self._lock_file.unlink()
                except FileNotFoundError:
                    pass

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Escribe un archivo JSON de manera atómica (.tmp -> fsync -> os.replace)."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        payload = json.dumps(_encode_json_value(data), indent=2, sort_keys=True, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    def _append_to_index(self, index_file: Path, entry: Dict[str, Any]) -> None:
        """Agrega una línea de forma append-only a un archivo index JSONL con fsync."""
        tmp_line = json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n"
        with open(index_file, "a", encoding="utf-8") as f:
            f.write(tmp_line)
            f.flush()
            os.fsync(f.fileno())

    def save_dataset(self, dataset: GoldenDataset) -> GoldenDataset:
        """Guarda bajo exclusión mutua para que check-and-write sea atómico."""
        with self._exclusive_lock():
            return self._save_dataset_unlocked(dataset)

    def _save_dataset_unlocked(self, dataset: GoldenDataset) -> GoldenDataset:
        ds_dir = self.manifests_dir / dataset.dataset_id
        version_file = ds_dir / f"{dataset.version}.json"

        if version_file.exists():
            try:
                existing_dataset = self._load_dataset_file(version_file)
                existing_checksum = existing_dataset.checksum

                if existing_checksum == dataset.checksum:
                    if existing_dataset.status == dataset.status:
                        return existing_dataset
                else:
                    raise DatasetVersionConflictError(
                        f"Dataset {dataset.dataset_id} version {dataset.version} already exists with different checksum ({existing_checksum} vs {dataset.checksum})"
                    )
            except DatasetVersionConflictError:
                raise
            except Exception as e:
                raise CorruptedGoldenDatasetRecordError(
                    f"Cannot verify existing dataset {dataset.dataset_id} v{dataset.version}: {e}"
                ) from e

        data = dataset.to_dict()
        self._atomic_write_json(version_file, data)
        self._append_to_index(
            self.datasets_index_file,
            {
                "dataset_id": dataset.dataset_id,
                "version": dataset.version,
                "name": dataset.name,
                "status": dataset.status.value,
                "checksum": dataset.checksum,
                "case_count": dataset.case_count,
                "created_at": dataset.created_at.isoformat(),
            },
        )
        return dataset

    def get_dataset(self, dataset_id: str, version: Optional[str] = None) -> Optional[GoldenDataset]:
        """
        Obtiene un dataset por su ID y versión específica.
        Si version es None, retorna la versión más alta (orden semver/lexicográfico) disponible.
        """
        ds_dir = self.manifests_dir / dataset_id
        if not ds_dir.exists() or not ds_dir.is_dir():
            return None

        if version is not None:
            version_file = ds_dir / f"{version}.json"
            if not version_file.exists():
                return None
            return self._load_dataset_file(version_file)

        # Buscar la última versión
        v_files = sorted(
            list(ds_dir.glob("*.json")),
            key=lambda path: semantic_version_key(path.stem),
            reverse=True,
        )
        if not v_files:
            return None
        return self._load_dataset_file(v_files[0])

    def _load_dataset_file(self, file_path: Path) -> GoldenDataset:
        """Carga y valida la integridad de un archivo de dataset JSON."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                d = json.load(f)

            manifest_data = d["manifest"]

            # Reconstruir referencias de casos
            case_refs = []
            for cr in manifest_data.get("case_references", []):
                case_refs.append(
                    DatasetCaseReference(
                        case_id=cr["case_id"],
                        case_version=cr.get("case_version", "1.0.0"),
                        evaluation_type=cr.get("evaluation_type"),
                        tags=tuple(cr.get("tags", [])),
                        expected_criteria_hash=cr.get("expected_criteria_hash", ""),
                        case_fingerprint=cr.get("case_fingerprint", ""),
                    )
                )

            # Reconstruir curator
            raw_curator = manifest_data.get("curator") or d.get("curator", {})
            curator_obj = GoldenDatasetCurator(
                curator_type=GoldenDatasetCuratorType(raw_curator.get("curator_type", "SYSTEM")),
                curator_id=raw_curator.get("curator_id", "unknown_curator"),
                details=raw_curator.get("details", {}),
            )

            # Validar checksum
            recomputed_checksum = compute_dataset_manifest_checksum(
                dataset_id=manifest_data["dataset_id"],
                version=manifest_data["version"],
                schema_version=manifest_data["schema_version"],
                case_references=case_refs,
                domain_scope=manifest_data.get("domain_scope", ""),
                tags=tuple(manifest_data.get("tags", [])),
                baseline_metrics=manifest_data.get("baseline_metrics", {}),
            )

            manifest_checksum = manifest_data.get("checksum", "")
            if manifest_checksum != recomputed_checksum:
                raise CorruptedGoldenDatasetRecordError(
                    f"Checksum mismatch in {file_path.name}: manifest has {manifest_checksum}, recomputed {recomputed_checksum}"
                )

            manifest = GoldenDatasetManifest(
                dataset_id=manifest_data["dataset_id"],
                version=manifest_data["version"],
                schema_version=manifest_data["schema_version"],
                checksum=manifest_checksum,
                case_references=tuple(case_refs),
                domain_scope=manifest_data.get("domain_scope", ""),
                tags=tuple(manifest_data.get("tags", [])),
                baseline_metrics=manifest_data.get("baseline_metrics", {}),
                provenance=GoldenDatasetProvenance(manifest_data.get("provenance", "MANUAL_CURATED")),
                curator=curator_obj,
                created_at=datetime.fromisoformat(manifest_data["created_at"]),
                metadata=manifest_data.get("metadata", {}),
            )

            return GoldenDataset(
                dataset_id=d["dataset_id"],
                name=d["name"],
                description=d["description"],
                version=d["version"],
                schema_version=d["schema_version"],
                status=GoldenDatasetStatus(d["status"]),
                manifest=manifest,
                domain_scope=d.get("domain_scope", ""),
                tags=tuple(d.get("tags", [])),
                curator=curator_obj,
                provenance=GoldenDatasetProvenance(d.get("provenance", "MANUAL_CURATED")),
                created_at=datetime.fromisoformat(d["created_at"]),
                curated_at=datetime.fromisoformat(d["curated_at"]) if d.get("curated_at") else None,
                metadata=d.get("metadata", {}),
            )
        except CorruptedGoldenDatasetRecordError:
            raise
        except Exception as e:
            raise CorruptedGoldenDatasetRecordError(f"Failed to load dataset file {file_path}: {e}") from e

    def list_datasets(
        self,
        domain_scope: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[GoldenDatasetStatus] = None,
        limit: int = 100,
    ) -> List[GoldenDataset]:
        """Lista la versión más reciente de cada dataset que cumpla con los filtros."""
        results: List[GoldenDataset] = []
        if not self.manifests_dir.exists():
            return results

        for ds_dir in sorted(self.manifests_dir.iterdir()):
            if not ds_dir.is_dir():
                continue
            dataset = self.get_dataset(dataset_id=ds_dir.name)
            if dataset is None:
                continue
            if domain_scope and dataset.domain_scope != domain_scope:
                continue
            if tag and tag not in dataset.tags:
                continue
            if status and dataset.status != status:
                continue
            results.append(dataset)
            if len(results) >= limit:
                break
        return results

    def list_versions(self, dataset_id: str) -> List[str]:
        """Lista todas las versiones disponibles para un dataset_id dado en orden descendente."""
        ds_dir = self.manifests_dir / dataset_id
        if not ds_dir.exists() or not ds_dir.is_dir():
            return []
        version_paths = sorted(
            ds_dir.glob("*.json"),
            key=lambda path: semantic_version_key(path.stem),
            reverse=True,
        )
        return [path.stem for path in version_paths]
