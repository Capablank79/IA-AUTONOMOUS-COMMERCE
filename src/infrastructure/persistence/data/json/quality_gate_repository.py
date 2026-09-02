"""
Implementación JSON persistente, atómica y determinista para Quality Gates (Hito K.6).

Garantiza:
- Atomic write (.tmp -> os.replace) con fsync.
- Inmutabilidad y versionado estricto para QualityGateDefinition.
- Inmutabilidad y registro idempotente de QualityGateDecision.
- Prevención de colisiones y detección de conflictos de versión (GateVersionConflictError) y decisión (GateDecisionConflictError).
- Sanitización recursiva de datos sensibles.
- Resiliencia ante caídas y recarga íntegra tras reinicio de proceso.
- Verificación estricta de integridad (load -> recompute -> compare) y visibilidad de corrupción (CorruptedQualityGateRecordError).
- Thread-safe mediante locks de concurrencia para la sección crítica completa (check -> write -> index).
- Reconstrucción y resiliencia de índices ante desincronización o archivos huérfanos.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Tuple

from src.domain.quality_gate.models import (
    QualityGateDefinition,
    QualityGateDecision,
    GateDecisionStatus,
    MissingCasePolicy,
    UnknownCasePolicy,
    ErrorCasePolicy,
    compute_gate_definition_checksum,
    compute_gate_decision_checksum,
    _sanitize_gate_data,
    quality_gate_version_key,
    SENSITIVE_KEYS,
)
from src.domain.quality_gate.ports import QualityGateRepositoryPort

logger = logging.getLogger(__name__)


class JsonQualityGateRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de Quality Gates."""
    pass


class GateVersionConflictError(JsonQualityGateRepositoryError):
    """Se lanza cuando se intenta sobrescribir una versión de Quality Gate con contenido diferente."""
    pass


class GateDecisionConflictError(JsonQualityGateRepositoryError):
    """Se lanza cuando una clave idempotente o ID identifica una decisión con contenido diferente."""
    pass


class CorruptedQualityGateRecordError(JsonQualityGateRepositoryError):
    """Se lanza cuando un registro en disco está corrupto o tiene checksum inválido."""
    pass


def _validate_safe_path_identifier(identifier: str, field_name: str) -> None:
    """Valida que un identificador no contenga caracteres de path traversal ni separadores."""
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    if "/" in identifier or "\\" in identifier or ".." in identifier or Path(identifier).name != identifier:
        raise ValueError(f"{field_name} '{identifier}' is not a safe path segment.")


def _encode_json_value(val: Any) -> Any:
    """Serializa valores de forma determinista y sanitiza claves sensibles recursivamente."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
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


class JsonQualityGateRepository(QualityGateRepositoryPort):
    """
    Repositorio JSON persistente, atómico y versionado para Quality Gates (K.6).
    Organización en disco:
      base_dir/
        definitions/
          {gate_id}/
            {version}.json
        decisions/
          {decision_id}.json
        index/
          definitions_index.jsonl
          decisions_index.jsonl
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.definitions_dir = self.base_dir / "definitions"
        self.decisions_dir = self.base_dir / "decisions"
        self.index_dir = self.base_dir / "index"

        self.definitions_dir.mkdir(parents=True, exist_ok=True)
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.definitions_index_file = self.index_dir / "definitions_index.jsonl"
        self.decisions_index_file = self.index_dir / "decisions_index.jsonl"
        self._lock = threading.Lock()

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Escribe un archivo JSON de manera atómica (.tmp -> fsync -> os.replace)."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(f".tmp_{os.getpid()}_{threading.get_ident()}_{id(data)}")
        payload = json.dumps(_encode_json_value(data), indent=2, sort_keys=True, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    def _append_to_index_locked(self, index_file: Path, entry: Dict[str, Any]) -> None:
        """Agrega una línea de forma append-only a un archivo index JSONL con fsync (asume lock adquirido)."""
        tmp_line = json.dumps(_encode_json_value(entry), sort_keys=True, ensure_ascii=False) + "\n"
        with open(index_file, "a", encoding="utf-8") as f:
            f.write(tmp_line)
            f.flush()
            os.fsync(f.fileno())

    def save_definition(self, definition: QualityGateDefinition) -> QualityGateDefinition:
        """
        Persiste una definición de Quality Gate de forma inmutable y atómica.
        Idempotente si ya existe con el mismo checksum.
        Lanza GateVersionConflictError si existe con distinto checksum.
        """
        _validate_safe_path_identifier(definition.gate_id, "gate_id")
        _validate_safe_path_identifier(definition.version, "version")

        gate_dir = self.definitions_dir / definition.gate_id
        version_file = gate_dir / f"{definition.version}.json"

        with self._lock:
            if version_file.exists():
                try:
                    existing = self._load_definition_file(version_file)
                    if existing.checksum == definition.checksum:
                        return existing
                    raise GateVersionConflictError(
                        f"QualityGate {definition.gate_id} version {definition.version} already exists with different checksum ({existing.checksum} vs {definition.checksum})"
                    )
                except GateVersionConflictError:
                    raise
                except Exception as e:
                    raise CorruptedQualityGateRecordError(
                        f"Cannot validate existing definition {definition.gate_id} v{definition.version}: {e}"
                    ) from e

            data = definition.to_dict()
            self._atomic_write_json(version_file, data)
            self._append_to_index_locked(
                self.definitions_index_file,
                {
                    "gate_id": definition.gate_id,
                    "version": definition.version,
                    "checksum": definition.checksum,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return definition

    def _load_definition_file(self, file_path: Path) -> QualityGateDefinition:
        """Carga y valida la integridad de un archivo de definición de Quality Gate."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise CorruptedQualityGateRecordError(f"Failed to read/parse definition file {file_path}: {exc}") from exc

        min_rate = Decimal(str(data["minimum_pass_rate"])) if data.get("minimum_pass_rate") is not None else None

        created_at_val = data.get("created_at")
        if isinstance(created_at_val, str):
            created_at = datetime.fromisoformat(created_at_val)
        else:
            created_at = datetime.now(timezone.utc)

        expected_checksum = data.get("checksum", "")
        recalculated_checksum = compute_gate_definition_checksum(
            gate_id=data["gate_id"],
            version=data["version"],
            required_case_ids=data.get("required_case_ids", []),
            critical_case_ids=data.get("critical_case_ids", []),
            minimum_pass_rate=min_rate,
            max_failures=data.get("max_failures", 0),
            max_unknown=data.get("max_unknown", 0),
            max_errors=data.get("max_errors", 0),
            target_dataset_id=data.get("target_dataset_id"),
            target_dataset_version=data.get("target_dataset_version"),
            target_dataset_manifest_checksum=data.get("target_dataset_manifest_checksum"),
            missing_case_policy=data.get("missing_case_policy", "FAIL"),
            unknown_case_policy=data.get("unknown_case_policy", "UNKNOWN"),
            error_case_policy=data.get("error_case_policy", "ERROR"),
            allowed_evaluator_versions=data.get("allowed_evaluator_versions", ()),
            provenance=data.get("provenance", "ENGINEERING_SPEC"),
            metadata=data.get("metadata", {}),
        )

        if not expected_checksum or expected_checksum != recalculated_checksum:
            raise CorruptedQualityGateRecordError(
                f"Checksum mismatch for gate definition in {file_path}: expected '{expected_checksum}', calculated '{recalculated_checksum}'"
            )

        return QualityGateDefinition(
            gate_id=data["gate_id"],
            name=data["name"],
            description=data["description"],
            version=data["version"],
            target_dataset_id=data.get("target_dataset_id"),
            target_dataset_version=data.get("target_dataset_version"),
            target_dataset_manifest_checksum=data.get("target_dataset_manifest_checksum"),
            required_case_ids=tuple(data.get("required_case_ids", ())),
            critical_case_ids=tuple(data.get("critical_case_ids", ())),
            minimum_pass_rate=min_rate,
            max_failures=data.get("max_failures", 0),
            max_unknown=data.get("max_unknown", 0),
            max_errors=data.get("max_errors", 0),
            missing_case_policy=MissingCasePolicy(data.get("missing_case_policy", "FAIL")),
            unknown_case_policy=UnknownCasePolicy(data.get("unknown_case_policy", "UNKNOWN")),
            error_case_policy=ErrorCasePolicy(data.get("error_case_policy", "ERROR")),
            allowed_evaluator_versions=tuple(data.get("allowed_evaluator_versions", ())),
            provenance=data.get("provenance", "ENGINEERING_SPEC"),
            created_at=created_at,
            checksum=expected_checksum,
            metadata=data.get("metadata", {}),
        )

    def get_definition(self, gate_id: str, version: Optional[str] = None) -> Optional[QualityGateDefinition]:
        """Recupera una definición de Quality Gate de disco."""
        _validate_safe_path_identifier(gate_id, "gate_id")
        if version is not None:
            _validate_safe_path_identifier(version, "version")

        gate_dir = self.definitions_dir / gate_id
        if not gate_dir.exists() or not gate_dir.is_dir():
            return None

        if version:
            version_file = gate_dir / f"{version}.json"
            if not version_file.exists():
                return None
            return self._load_definition_file(version_file)

        version_files = [f for f in gate_dir.glob("*.json") if not f.name.endswith(".tmp")]
        if not version_files:
            return None
        latest_file = max(version_files, key=lambda f: quality_gate_version_key(f.stem))
        return self._load_definition_file(latest_file)

    def list_definitions(self, limit: int = 100) -> List[QualityGateDefinition]:
        """Lista las definiciones de Quality Gate (última versión por gate_id)."""
        definitions: List[QualityGateDefinition] = []
        if not self.definitions_dir.exists():
            return definitions

        for gate_dir in sorted([d for d in self.definitions_dir.iterdir() if d.is_dir()], key=lambda d: d.name):
            try:
                d = self.get_definition(gate_dir.name)
                if d:
                    definitions.append(d)
                    if len(definitions) >= limit:
                        break
            except Exception as e:
                logger.warning(f"Skipping definition in {gate_dir.name} due to error: {e}")

        return definitions

    def list_definition_versions(self, gate_id: str) -> List[str]:
        """Lista todas las versiones disponibles para un gate_id ordenadas por SemVer descendente."""
        _validate_safe_path_identifier(gate_id, "gate_id")
        gate_dir = self.definitions_dir / gate_id
        if not gate_dir.exists() or not gate_dir.is_dir():
            return []
        version_files = [f for f in gate_dir.glob("*.json") if not f.name.endswith(".tmp")]
        return sorted(
            [f.stem for f in version_files],
            key=quality_gate_version_key,
            reverse=True,
        )

    def save_decision(self, decision: QualityGateDecision) -> QualityGateDecision:
        """
        Persiste una decisión de Quality Gate de manera inmutable, atómica e idempotente.
        Sección crítica sincronizada con lock de concurrencia.
        """
        _validate_safe_path_identifier(decision.decision_id, "decision_id")
        decision_file = self.decisions_dir / f"{decision.decision_id}.json"

        with self._lock:
            if decision_file.exists():
                try:
                    existing = self._load_decision_file(decision_file)
                except Exception as exc:
                    raise CorruptedQualityGateRecordError(
                        f"Cannot validate existing decision {decision.decision_id}: {exc}"
                    ) from exc
                if existing.checksum == decision.checksum:
                    return existing
                raise GateDecisionConflictError(
                    f"QualityGateDecision {decision.decision_id} already exists with different content (existing checksum: {existing.checksum}, new checksum: {decision.checksum})."
                )

            existing_by_key = self._find_decision_by_idempotency_key_locked(decision.idempotency_key)
            if existing_by_key is not None:
                if existing_by_key.checksum == decision.checksum:
                    return existing_by_key
                raise GateDecisionConflictError(
                    f"QualityGateDecision idempotency key {decision.idempotency_key} already exists with different content (existing checksum: {existing_by_key.checksum}, new checksum: {decision.checksum})."
                )

            data = decision.to_dict()
            self._atomic_write_json(decision_file, data)
            self._append_to_index_locked(
                self.decisions_index_file,
                {
                    "decision_id": decision.decision_id,
                    "gate_id": decision.gate_id,
                    "gate_version": decision.gate_version,
                    "evaluation_run_id": decision.evaluation_run_id,
                    "status": decision.status.value,
                    "idempotency_key": decision.idempotency_key,
                    "checksum": decision.checksum,
                    "decided_at": decision.decided_at.isoformat(),
                },
            )
            return decision

    def _load_decision_file(self, file_path: Path) -> QualityGateDecision:
        """Carga y valida una decisión de Quality Gate desde disco."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise CorruptedQualityGateRecordError(f"Failed to read/parse decision file {file_path}: {exc}") from exc

        decided_at_val = data.get("decided_at")
        if isinstance(decided_at_val, str):
            decided_at = datetime.fromisoformat(decided_at_val)
        else:
            decided_at = datetime.now(timezone.utc)

        pass_rate_val = Decimal(str(data["pass_rate"])) if data.get("pass_rate") is not None else None
        evidence = data.get("evidence", {})
        recalculated_checksum = compute_gate_decision_checksum(
            decision_id=data["decision_id"],
            gate_id=data["gate_id"],
            gate_version=data["gate_version"],
            evaluation_run_id=data["evaluation_run_id"],
            status=data["status"],
            passed_count=data.get("passed_count", 0),
            failed_count=data.get("failed_count", 0),
            unknown_count=data.get("unknown_count", 0),
            error_count=data.get("error_count", 0),
            total_cases=data.get("total_cases", 0),
            evaluated_count=data.get("evaluated_count", 0),
            pass_rate=pass_rate_val,
            failed_case_ids=data.get("failed_case_ids", ()),
            critical_case_failures=data.get("critical_case_failures", ()),
            missing_required_case_ids=data.get("missing_required_case_ids", ()),
            unknown_case_ids=data.get("unknown_case_ids", ()),
            error_case_ids=data.get("error_case_ids", ()),
            reasons=data.get("reasons", ()),
            dataset_id=data.get("dataset_id"),
            dataset_version=data.get("dataset_version"),
            dataset_manifest_checksum=data.get("dataset_manifest_checksum"),
            evidence=evidence,
            trace_reference=data.get("trace_reference"),
            audit_reference=data.get("audit_reference"),
            cost_reference=data.get("cost_reference"),
            correlation_id=data.get("correlation_id", ""),
            causation_id=data.get("causation_id"),
            provenance=data.get("provenance", "QUALITY_GATE_ENGINE"),
            idempotency_key=data.get("idempotency_key", ""),
            schema_version=data.get("schema_version", "1.0.0"),
            metadata=data.get("metadata", {}),
        )
        expected_checksum = data.get("checksum")
        if not expected_checksum or expected_checksum != recalculated_checksum:
            raise CorruptedQualityGateRecordError(
                f"Checksum mismatch for gate decision in {file_path}: expected '{expected_checksum}', calculated '{recalculated_checksum}'"
            )

        return QualityGateDecision(
            decision_id=data["decision_id"],
            gate_id=data["gate_id"],
            gate_version=data["gate_version"],
            status=GateDecisionStatus(data["status"]),
            evaluation_run_id=data["evaluation_run_id"],
            decided_at=decided_at,
            total_cases=data.get("total_cases", 0),
            passed_count=data.get("passed_count", 0),
            failed_count=data.get("failed_count", 0),
            unknown_count=data.get("unknown_count", 0),
            error_count=data.get("error_count", 0),
            evaluated_count=data.get("evaluated_count", 0),
            pass_rate=pass_rate_val,
            failed_case_ids=tuple(data.get("failed_case_ids", ())),
            unknown_case_ids=tuple(data.get("unknown_case_ids", ())),
            error_case_ids=tuple(data.get("error_case_ids", ())),
            missing_required_case_ids=tuple(data.get("missing_required_case_ids", ())),
            critical_case_failures=tuple(data.get("critical_case_failures", ())),
            reasons=tuple(data.get("reasons", ())),
            dataset_id=data.get("dataset_id"),
            dataset_version=data.get("dataset_version"),
            dataset_manifest_checksum=data.get("dataset_manifest_checksum"),
            evidence=evidence,
            trace_reference=data.get("trace_reference"),
            audit_reference=data.get("audit_reference"),
            cost_reference=data.get("cost_reference"),
            correlation_id=data.get("correlation_id", ""),
            causation_id=data.get("causation_id"),
            provenance=data.get("provenance", "QUALITY_GATE_ENGINE"),
            idempotency_key=data.get("idempotency_key", ""),
            checksum=data.get("checksum", ""),
            schema_version=data.get("schema_version", "1.0.0"),
            metadata=data.get("metadata", {}),
        )

    def get_decision(self, decision_id: str) -> Optional[QualityGateDecision]:
        """Recupera una decisión por su decision_id."""
        _validate_safe_path_identifier(decision_id, "decision_id")
        decision_file = self.decisions_dir / f"{decision_id}.json"
        if not decision_file.exists():
            return None
        return self._load_decision_file(decision_file)

    def _find_decision_by_idempotency_key_locked(self, idempotency_key: str) -> Optional[QualityGateDecision]:
        """Busca una decisión por clave de idempotencia usando el índice o fallback a escaneo (asume lock o lectura segura)."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be empty.")

        matched_decision_id: Optional[str] = None
        if self.decisions_index_file.exists():
            try:
                with open(self.decisions_index_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("idempotency_key") == idempotency_key:
                                matched_decision_id = entry.get("decision_id")
                        except Exception:
                            continue
            except Exception as e:
                logger.warning(f"Error reading decisions index: {e}")

        if matched_decision_id:
            try:
                return self.get_decision(matched_decision_id)
            except Exception as e:
                logger.warning(f"Error loading decision from index reference {matched_decision_id}: {e}")

        # Fallback de escaneo por si el índice no estaba actualizado o recuperado
        if self.decisions_dir.exists():
            for f in self.decisions_dir.glob("*.json"):
                if f.name.endswith(".tmp"):
                    continue
                try:
                    dec = self._load_decision_file(f)
                    if dec.idempotency_key == idempotency_key:
                        return dec
                except Exception:
                    continue

        return None

    def get_decision_by_idempotency_key(self, idempotency_key: str) -> Optional[QualityGateDecision]:
        """Recupera una decisión a partir de su clave de idempotencia."""
        with self._lock:
            return self._find_decision_by_idempotency_key_locked(idempotency_key)

    def recover_index(self) -> int:
        """Reconstruye los índices a partir de los archivos válidos presentes en disco."""
        with self._lock:
            recovered_count = 0
            if self.definitions_dir.exists():
                tmp_def_index = self.definitions_index_file.with_suffix(".tmp_recover")
                with open(tmp_def_index, "w", encoding="utf-8") as out:
                    for gate_dir in self.definitions_dir.iterdir():
                        if not gate_dir.is_dir():
                            continue
                        for v_file in gate_dir.glob("*.json"):
                            if v_file.name.endswith(".tmp"):
                                continue
                            try:
                                def_item = self._load_definition_file(v_file)
                                entry = {
                                    "gate_id": def_item.gate_id,
                                    "version": def_item.version,
                                    "checksum": def_item.checksum,
                                    "saved_at": def_item.created_at.isoformat(),
                                }
                                out.write(json.dumps(entry, sort_keys=True) + "\n")
                                recovered_count += 1
                            except Exception:
                                continue
                os.replace(tmp_def_index, self.definitions_index_file)

            if self.decisions_dir.exists():
                tmp_dec_index = self.decisions_index_file.with_suffix(".tmp_recover")
                with open(tmp_dec_index, "w", encoding="utf-8") as out:
                    for d_file in self.decisions_dir.glob("*.json"):
                        if d_file.name.endswith(".tmp"):
                            continue
                        try:
                            dec_item = self._load_decision_file(d_file)
                            entry = {
                                "decision_id": dec_item.decision_id,
                                "gate_id": dec_item.gate_id,
                                "gate_version": dec_item.gate_version,
                                "evaluation_run_id": dec_item.evaluation_run_id,
                                "status": dec_item.status.value,
                                "idempotency_key": dec_item.idempotency_key,
                                "checksum": dec_item.checksum,
                                "decided_at": dec_item.decided_at.isoformat(),
                            }
                            out.write(json.dumps(entry, sort_keys=True) + "\n")
                            recovered_count += 1
                        except Exception:
                            continue
                os.replace(tmp_dec_index, self.decisions_index_file)

            return recovered_count

    def list_decisions(
        self,
        gate_id: Optional[str] = None,
        gate_version: Optional[str] = None,
        evaluation_run_id: Optional[str] = None,
        status: Optional[GateDecisionStatus] = None,
        limit: int = 100,
    ) -> List[QualityGateDecision]:
        """Lista decisiones registradas en disco con filtros opcionales."""
        decisions: List[QualityGateDecision] = []
        if not self.decisions_dir.exists():
            return decisions

        for decision_file in sorted([f for f in self.decisions_dir.glob("*.json") if not f.name.endswith(".tmp")], key=lambda f: f.stem, reverse=True):
            try:
                dec = self._load_decision_file(decision_file)
                if gate_id and dec.gate_id != gate_id:
                    continue
                if gate_version and dec.gate_version != gate_version:
                    continue
                if evaluation_run_id and dec.evaluation_run_id != evaluation_run_id:
                    continue
                if status and dec.status != status:
                    continue
                decisions.append(dec)
                if len(decisions) >= limit:
                    break
            except Exception as e:
                logger.warning(f"Error reading decision file {decision_file}: {e}")

        return decisions
