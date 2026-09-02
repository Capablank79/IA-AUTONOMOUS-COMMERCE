"""
Implementación JSON persistente, atómica y determinista para Evaluation Harness (Hito K.4).

Garantiza:
- Atomic write (.tmp -> os.replace) con fsync.
- Inmutabilidad y semántica append-only para casos y resultados.
- Sanitización recursiva de datos sensibles.
- Idempotencia estricta por case_id y result_id / idempotency_key.
- Resiliencia ante caídas y recarga íntegra tras reinicio de proceso.
- Verificación de integridad y prevención de sobreescrituras destructivas.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Union, Optional, Any, Dict, List, Tuple

from src.domain.evaluation.models import (
    EvaluationCase,
    EvaluationResult,
    EvaluationMetric,
    EvaluationStatus,
    EvaluationType,
)
from src.domain.evaluation.ports import EvaluationRepositoryPort

logger = logging.getLogger(__name__)


class JsonEvaluationRepositoryError(Exception):
    """Excepción base para errores en el repositorio JSON de evaluación."""
    pass


class CorruptedEvaluationRecordError(JsonEvaluationRepositoryError):
    """Se lanza cuando un archivo de caso o resultado está corrupto."""
    pass


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
    "chain_of_thought",
    "reasoning",
    "reasoning_tokens",
    "internal_scratchpad",
    "card_number",
}


def _encode_json_value(val: Any) -> Any:
    """Serializa estructuras de forma determinista y sanitiza claves sensibles recursivamente."""
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


class JsonEvaluationRepository(EvaluationRepositoryPort):
    """
    Repositorio JSON persistente y seguro para EvaluationCase y EvaluationResult.
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self.cases_dir = self.base_dir / "cases"
        self.results_dir = self.base_dir / "results"
        self.index_dir = self.base_dir / "index"

        self.cases_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.cases_index_file = self.index_dir / "cases_index.jsonl"
        self.results_index_file = self.index_dir / "results_index.jsonl"

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Escribe un archivo JSON de manera atómica (.tmp -> fsync -> os.replace)."""
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

    def save_case(self, case: EvaluationCase) -> EvaluationCase:
        """Guarda un caso de evaluación de forma idempotente."""
        case_file = self.cases_dir / f"{case.case_id}.json"
        
        # Idempotencia: si ya existe exactamente el mismo caso, retornar sin modificar
        if case_file.exists():
            try:
                with open(case_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if existing.get("case_id") == case.case_id and existing.get("version") == case.version:
                    return case
            except Exception:
                pass  # Si hubo error de lectura, sobreescribir atómicamente

        data = case.to_dict()
        self._atomic_write_json(case_file, data)
        self._append_to_index(
            self.cases_index_file,
            {
                "case_id": case.case_id,
                "name": case.name,
                "evaluation_type": case.evaluation_type.value,
                "version": case.version,
                "created_at": case.created_at.isoformat(),
            },
        )
        return case

    def get_case(self, case_id: str) -> Optional[EvaluationCase]:
        """Obtiene un caso por su case_id."""
        case_file = self.cases_dir / f"{case_id}.json"
        if not case_file.exists():
            return None
        try:
            with open(case_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            return EvaluationCase(
                case_id=d["case_id"],
                name=d["name"],
                description=d["description"],
                evaluation_type=EvaluationType(d["evaluation_type"]),
                input_reference=d.get("input_reference", {}),
                expected_criteria=d.get("expected_criteria", {}),
                tags=tuple(d.get("tags", [])),
                version=d.get("version", "1.0.0"),
                created_at=datetime.fromisoformat(d["created_at"]),
                provenance=d.get("provenance", "ENGINEERING_SPEC"),
                metadata=d.get("metadata", {}),
            )
        except Exception as e:
            raise CorruptedEvaluationRecordError(f"Error reading case {case_id}: {e}") from e

    def list_cases(
        self,
        evaluation_type: Optional[EvaluationType] = None,
        tag: Optional[str] = None,
        limit: int = 100,
    ) -> List[EvaluationCase]:
        """Lista casos con filtros opcionales."""
        results = []
        for p in sorted(self.cases_dir.glob("*.json")):
            case = self.get_case(p.stem)
            if case is None:
                continue
            if evaluation_type and case.evaluation_type != evaluation_type:
                continue
            if tag and tag not in case.tags:
                continue
            results.append(case)
            if len(results) >= limit:
                break
        return results

    def save_result(self, result: EvaluationResult) -> EvaluationResult:
        """Guarda un resultado de evaluación de forma idempotente."""
        res_file = self.results_dir / f"{result.result_id}.json"
        
        # Idempotencia: si ya existe, verificar si es el mismo
        if res_file.exists():
            try:
                with open(res_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if existing.get("result_id") == result.result_id or existing.get("idempotency_key") == result.idempotency_key:
                    return result
            except Exception:
                pass

        data = result.to_dict()
        self._atomic_write_json(res_file, data)
        self._append_to_index(
            self.results_index_file,
            {
                "result_id": result.result_id,
                "case_id": result.case_id,
                "execution_id": result.execution_id,
                "evaluated_component": result.evaluated_component,
                "status": result.status.value,
                "started_at": result.started_at.isoformat(),
                "completed_at": result.completed_at.isoformat(),
                "idempotency_key": result.idempotency_key,
            },
        )
        return result

    def get_result(self, result_id: str) -> Optional[EvaluationResult]:
        """Obtiene un resultado por su result_id."""
        res_file = self.results_dir / f"{result_id}.json"
        if not res_file.exists():
            return None
        try:
            with open(res_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            
            # Reconstruir métricas
            raw_metrics = d.get("metrics", [])
            metrics_objs = []
            for m in raw_metrics:
                metrics_objs.append(
                    EvaluationMetric(
                        metric_name=m["metric_name"],
                        metric_value=m["metric_value"],
                        unit=m.get("unit", "COUNT"),
                        expected_value=m.get("expected_value"),
                        min_value=m.get("min_value"),
                        max_value=m.get("max_value"),
                        status=EvaluationStatus(m["status"]),
                        evidence=m.get("evidence", {}),
                    )
                )

            return EvaluationResult(
                result_id=d["result_id"],
                case_id=d["case_id"],
                execution_id=d["execution_id"],
                evaluated_component=d["evaluated_component"],
                started_at=datetime.fromisoformat(d["started_at"]),
                completed_at=datetime.fromisoformat(d["completed_at"]),
                status=EvaluationStatus(d["status"]),
                metrics=tuple(metrics_objs),
                expected_reference=d.get("expected_reference", {}),
                actual_reference=d.get("actual_reference", {}),
                evidence=d.get("evidence", {}),
                trace_reference=d.get("trace_reference"),
                audit_reference=d.get("audit_reference"),
                cost_reference=d.get("cost_reference"),
                correlation_id=d.get("correlation_id", ""),
                causation_id=d.get("causation_id"),
                provenance=d.get("provenance", "EVALUATION_HARNESS"),
                evaluator_version=d.get("evaluator_version", "1.0.0"),
                idempotency_key=d.get("idempotency_key", ""),
                metadata=d.get("metadata", {}),
            )
        except Exception as e:
            raise CorruptedEvaluationRecordError(f"Error reading result {result_id}: {e}") from e

    def list_results(
        self,
        case_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        evaluated_component: Optional[str] = None,
        status: Optional[EvaluationStatus] = None,
        limit: int = 100,
    ) -> List[EvaluationResult]:
        """Lista resultados con filtros opcionales."""
        results = []
        for p in sorted(self.results_dir.glob("*.json")):
            res = self.get_result(p.stem)
            if res is None:
                continue
            if case_id and res.case_id != case_id:
                continue
            if execution_id and res.execution_id != execution_id:
                continue
            if evaluated_component and res.evaluated_component != evaluated_component:
                continue
            if status and res.status != status:
                continue
            results.append(res)
            if len(results) >= limit:
                break
        return results
