"""
Implementación de evaluadores deterministas estándar para K.4 Evaluation Harness.

Incluye:
- ExactMatchEvaluator: Evaluación de igualdad exacta de campos o valores.
- StructuralEvaluator: Verificación de presencia de campos requeridos, tipos y esquemas estructurales mínimos.
- NumericToleranceEvaluator: Evaluación de valores numéricos dentro de rangos [min, max] o tolerancias absolutas/relativas.
- StatusEvaluator: Evaluación de estados (preservando UNKNOWN).
- PolicyEvaluator: Evaluación de decisiones y severidades de PolicyEngine.
- SafetyEvaluator: Evaluación de ausencia de secretos, PII y cumplimiento de guardrails.
- TraceEvaluator: Evaluación de secuencia y presencia de pasos operacionales en trazas (K.2).
- IdempotencyEvaluator: Evaluación de igualdad entre primera ejecución y repetición/replay.
- EvaluatorRegistry: Registro y despacho de evaluadores por EvaluationType.
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Optional, Dict, Any, List, Tuple, Sequence, Union
import uuid

from src.domain.evaluation.models import (
    EvaluationType,
    EvaluationStatus,
    EvaluationMetric,
    EvaluationCase,
    EvaluationResult,
)
from src.domain.evaluation.ports import EvaluatorPort

logger = logging.getLogger(__name__)


class ExactMatchEvaluator(EvaluatorPort):
    """
    Evaluador determinista de coincidencia exacta (EXACT_MATCH).
    Compara las claves/valores esperados contra los producidos en actual_output.
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.EXACT_MATCH

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        
        # Formatear actual
        actual_dict = actual_output if isinstance(actual_output, dict) else {"value": actual_output}
        
        metrics = []
        all_passed = True
        has_unknown = False

        for k, exp_val in expected.items():
            act_val = actual_dict.get(k)
            
            # Semántica de UNKNOWN
            if act_val is None and exp_val is not None and "allow_none" not in expected:
                status = EvaluationStatus.FAIL
                all_passed = False
            elif act_val == "UNKNOWN" or act_val is None:
                if exp_val == "UNKNOWN":
                    status = EvaluationStatus.PASS
                else:
                    status = EvaluationStatus.UNKNOWN
                    has_unknown = True
                    all_passed = False
            elif str(act_val) == str(exp_val) or act_val == exp_val:
                status = EvaluationStatus.PASS
            else:
                status = EvaluationStatus.FAIL
                all_passed = False

            metrics.append(
                EvaluationMetric(
                    metric_name=f"exact_match_{k}",
                    metric_value=act_val,
                    expected_value=exp_val,
                    status=status,
                    evidence={"field": k, "expected": exp_val, "actual": act_val},
                )
            )

        if not expected:
            overall_status = EvaluationStatus.PASS
        elif all_passed:
            overall_status = EvaluationStatus.PASS
        elif has_unknown:
            overall_status = EvaluationStatus.UNKNOWN
        else:
            overall_status = EvaluationStatus.FAIL

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=overall_status,
            metrics=tuple(metrics),
            expected_reference=expected,
            actual_reference=actual_dict,
            evidence={"metrics_count": len(metrics)},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class StructuralEvaluator(EvaluatorPort):
    """
    Evaluador determinista de estructura (STRUCTURAL).
    Verifica campos requeridos y tipos básicos en el resultado.
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.STRUCTURAL

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        required_fields = expected.get("required_fields", [])
        forbidden_fields = expected.get("forbidden_fields", [])
        
        actual_dict = actual_output if isinstance(actual_output, dict) else {}
        
        metrics = []
        all_passed = True
        has_unknown = False

        if not isinstance(actual_output, dict):
            metrics.append(
                EvaluationMetric(
                    metric_name="is_dict_structure",
                    metric_value=type(actual_output).__name__,
                    expected_value="dict",
                    status=EvaluationStatus.FAIL,
                )
            )
            all_passed = False
        else:
            for field_name in required_fields:
                present = field_name in actual_dict and actual_dict[field_name] is not None
                val = actual_dict.get(field_name)
                
                if val == "UNKNOWN":
                    status = EvaluationStatus.UNKNOWN
                    has_unknown = True
                    all_passed = False
                elif present:
                    status = EvaluationStatus.PASS
                else:
                    status = EvaluationStatus.FAIL
                    all_passed = False

                metrics.append(
                    EvaluationMetric(
                        metric_name=f"required_field_{field_name}",
                        metric_value=present,
                        expected_value=True,
                        status=status,
                        evidence={"field": field_name, "value": val},
                    )
                )

            for field_name in forbidden_fields:
                present = field_name in actual_dict
                status = EvaluationStatus.PASS if not present else EvaluationStatus.FAIL
                if present:
                    all_passed = False
                metrics.append(
                    EvaluationMetric(
                        metric_name=f"forbidden_field_{field_name}",
                        metric_value=present,
                        expected_value=False,
                        status=status,
                        evidence={"field": field_name, "forbidden": True},
                    )
                )

        if all_passed:
            overall_status = EvaluationStatus.PASS
        elif has_unknown:
            overall_status = EvaluationStatus.UNKNOWN
        else:
            overall_status = EvaluationStatus.FAIL

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=overall_status,
            metrics=tuple(metrics),
            expected_reference=expected,
            actual_reference=actual_dict if isinstance(actual_output, dict) else {"raw": str(actual_output)},
            evidence={"metrics_count": len(metrics)},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class NumericToleranceEvaluator(EvaluatorPort):
    """
    Evaluador determinista de valores numéricos (NUMERIC).
    Compara rangos [min, max] o valor esperado con tolerancia absoluta.
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.NUMERIC

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        actual_dict = actual_output if isinstance(actual_output, dict) else {"numeric_value": actual_output}
        
        metrics = []
        all_passed = True
        has_unknown = False

        for field_name, criteria in expected.items():
            act_val = actual_dict.get(field_name)
            if act_val is None or act_val == "UNKNOWN":
                status = EvaluationStatus.UNKNOWN
                has_unknown = True
                all_passed = False
                metrics.append(
                    EvaluationMetric(
                        metric_name=f"numeric_{field_name}",
                        metric_value=act_val,
                        status=status,
                        evidence={"field": field_name, "reason": "value_is_unknown_or_missing"},
                    )
                )
                continue

            try:
                dec_val = Decimal(str(act_val))
            except Exception:
                status = EvaluationStatus.FAIL
                all_passed = False
                metrics.append(
                    EvaluationMetric(
                        metric_name=f"numeric_{field_name}",
                        metric_value=act_val,
                        status=status,
                        evidence={"field": field_name, "error": "not_convertible_to_decimal"},
                    )
                )
                continue

            # Evaluar criteria
            field_passed = True
            min_val = None
            max_val = None
            exp_val = None

            if isinstance(criteria, dict):
                if "min" in criteria:
                    min_val = Decimal(str(criteria["min"]))
                    if dec_val < min_val:
                        field_passed = False
                if "max" in criteria:
                    max_val = Decimal(str(criteria["max"]))
                    if dec_val > max_val:
                        field_passed = False
                if "expected" in criteria:
                    exp_val = Decimal(str(criteria["expected"]))
                    tolerance = Decimal(str(criteria.get("tolerance", "0.0")))
                    if abs(dec_val - exp_val) > tolerance:
                        field_passed = False
            else:
                exp_val = Decimal(str(criteria))
                if dec_val != exp_val:
                    field_passed = False

            status = EvaluationStatus.PASS if field_passed else EvaluationStatus.FAIL
            if not field_passed:
                all_passed = False

            metrics.append(
                EvaluationMetric(
                    metric_name=f"numeric_{field_name}",
                    metric_value=dec_val,
                    expected_value=exp_val,
                    min_value=min_val,
                    max_value=max_val,
                    status=status,
                    evidence={"field": field_name, "criteria": criteria, "actual": str(dec_val)},
                )
            )

        if all_passed:
            overall_status = EvaluationStatus.PASS
        elif has_unknown:
            overall_status = EvaluationStatus.UNKNOWN
        else:
            overall_status = EvaluationStatus.FAIL

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=overall_status,
            metrics=tuple(metrics),
            expected_reference=expected,
            actual_reference=actual_dict,
            evidence={"metrics_count": len(metrics)},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class StatusEvaluator(EvaluatorPort):
    """
    Evaluador determinista de estados (STATUS).
    Permite validar estados terminales o intermedios esperados, preservando UNKNOWN.
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.STATUS

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        expected_status = expected.get("expected_status")
        allowed_statuses = expected.get("allowed_statuses", [])
        if expected_status and expected_status not in allowed_statuses:
            allowed_statuses = [expected_status] + list(allowed_statuses)

        actual_status = None
        if isinstance(actual_output, dict):
            actual_status = actual_output.get("status")
        elif hasattr(actual_output, "status"):
            st = getattr(actual_output, "status")
            actual_status = st.value if hasattr(st, "value") else str(st)
        elif hasattr(actual_output, "value"):
            actual_status = actual_output.value
        else:
            actual_status = str(actual_output)

        actual_status_str = str(actual_status) if actual_status is not None else "UNKNOWN"
        
        # Comparación
        if actual_status_str == "UNKNOWN" and "UNKNOWN" not in allowed_statuses:
            status = EvaluationStatus.UNKNOWN
        elif actual_status_str in allowed_statuses:
            status = EvaluationStatus.PASS
        else:
            status = EvaluationStatus.FAIL

        metric = EvaluationMetric(
            metric_name="status_validation",
            metric_value=actual_status_str,
            expected_value=allowed_statuses,
            status=status,
            evidence={"actual_status": actual_status_str, "allowed_statuses": allowed_statuses},
        )

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            metrics=(metric,),
            expected_reference=expected,
            actual_reference={"status": actual_status_str},
            evidence={"evaluated_status": actual_status_str},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class PolicyEvaluator(EvaluatorPort):
    """
    Evaluador determinista para decisiones de PolicyEngine (POLICY).
    Verifica que la decisión (ALLOW, DENY, REQUIRE_APPROVAL, UNKNOWN) y violaciones coincidan con el criterio.
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.POLICY

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        expected_decision = expected.get("expected_decision")
        expected_violations_count = expected.get("expected_violations_count")

        actual_dict = actual_output if isinstance(actual_output, dict) else {}
        actual_decision = actual_dict.get("decision_type")
        actual_violations = actual_dict.get("violations", [])

        metrics = []
        all_passed = True
        has_unknown = False

        # 1. Evaluar decision_type
        if actual_decision == "UNKNOWN" and expected_decision != "UNKNOWN":
            decision_st = EvaluationStatus.UNKNOWN
            has_unknown = True
            all_passed = False
        elif str(actual_decision) == str(expected_decision):
            decision_st = EvaluationStatus.PASS
        else:
            decision_st = EvaluationStatus.FAIL
            all_passed = False

        metrics.append(
            EvaluationMetric(
                metric_name="policy_decision_type",
                metric_value=actual_decision,
                expected_value=expected_decision,
                status=decision_st,
                evidence={"actual_decision": actual_decision, "expected_decision": expected_decision},
            )
        )

        # 2. Evaluar violations count si aplica
        if expected_violations_count is not None:
            actual_count = len(actual_violations)
            viol_passed = actual_count == expected_violations_count
            viol_st = EvaluationStatus.PASS if viol_passed else EvaluationStatus.FAIL
            if not viol_passed:
                all_passed = False
            metrics.append(
                EvaluationMetric(
                    metric_name="policy_violations_count",
                    metric_value=actual_count,
                    expected_value=expected_violations_count,
                    status=viol_st,
                    evidence={"actual_count": actual_count, "violations": actual_violations},
                )
            )

        if all_passed:
            overall_status = EvaluationStatus.PASS
        elif has_unknown:
            overall_status = EvaluationStatus.UNKNOWN
        else:
            overall_status = EvaluationStatus.FAIL

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=overall_status,
            metrics=tuple(metrics),
            expected_reference=expected,
            actual_reference=actual_dict,
            evidence={"metrics_count": len(metrics)},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class SafetyEvaluator(EvaluatorPort):
    """
    Evaluador determinista de seguridad y sanitización (SAFETY).
    Verifica que no existan secretos expuestos en la salida.
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.SAFETY

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        forbidden_substrings = expected.get("forbidden_substrings", [])
        
        # Serializar y escanear
        out_str = str(actual_output).lower()
        leaks_found = []
        for s in forbidden_substrings:
            if str(s).lower() in out_str:
                leaks_found.append(s)

        is_safe = len(leaks_found) == 0
        status = EvaluationStatus.PASS if is_safe else EvaluationStatus.FAIL

        metric = EvaluationMetric(
            metric_name="safety_leak_check",
            metric_value=len(leaks_found),
            expected_value=0,
            status=status,
            evidence={"leaks_count": len(leaks_found), "leaks": leaks_found},
        )

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            metrics=(metric,),
            expected_reference=expected,
            actual_reference={"raw_type": type(actual_output).__name__},
            evidence={"safe": is_safe, "leaks_found": len(leaks_found)},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class TraceEvaluator(EvaluatorPort):
    """
    Evaluador determinista de pasos de trazas de agentes (TRACE).
    Verifica la secuencia y presencia de tipos de pasos requeridos (K.2).
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.TRACE

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        required_steps = expected.get("required_step_types", [])
        expected_final_status = expected.get("expected_final_status")

        # actual_output puede ser una lista de dicts o un ExecutionTraceTimeline
        steps = []
        if isinstance(actual_output, list):
            steps = actual_output
        elif isinstance(actual_output, dict) and "steps" in actual_output:
            steps = actual_output["steps"]
        elif hasattr(actual_output, "steps"):
            steps = getattr(actual_output, "steps")

        actual_step_types = []
        for step in steps:
            if isinstance(step, dict):
                st_type = step.get("step_type")
            elif hasattr(step, "step_type"):
                val = getattr(step, "step_type")
                st_type = val.value if hasattr(val, "value") else str(val)
            else:
                st_type = str(step)
            actual_step_types.append(st_type)

        metrics = []
        all_passed = True
        has_unknown = False

        # Verificar pasos requeridos
        for req in required_steps:
            present = req in actual_step_types
            st = EvaluationStatus.PASS if present else EvaluationStatus.FAIL
            if not present:
                all_passed = False
            metrics.append(
                EvaluationMetric(
                    metric_name=f"trace_step_{req}",
                    metric_value=present,
                    expected_value=True,
                    status=st,
                    evidence={"step_type": req, "found": present},
                )
            )

        # Verificar estado final si se especifica
        if expected_final_status:
            last_step = steps[-1] if steps else None
            last_status = None
            if isinstance(last_step, dict):
                last_status = last_step.get("status")
            elif hasattr(last_step, "status"):
                val = getattr(last_step, "status")
                last_status = val.value if hasattr(val, "value") else str(val)
            
            if last_status == "UNKNOWN" and expected_final_status != "UNKNOWN":
                st = EvaluationStatus.UNKNOWN
                has_unknown = True
                all_passed = False
            elif str(last_status) == str(expected_final_status):
                st = EvaluationStatus.PASS
            else:
                st = EvaluationStatus.FAIL
                all_passed = False

            metrics.append(
                EvaluationMetric(
                    metric_name="trace_final_status",
                    metric_value=str(last_status),
                    expected_value=expected_final_status,
                    status=st,
                    evidence={"last_status": str(last_status), "expected": expected_final_status},
                )
            )

        if all_passed:
            overall_status = EvaluationStatus.PASS
        elif has_unknown:
            overall_status = EvaluationStatus.UNKNOWN
        else:
            overall_status = EvaluationStatus.FAIL

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=overall_status,
            metrics=tuple(metrics),
            expected_reference=expected,
            actual_reference={"steps_count": len(steps), "step_types": actual_step_types},
            evidence={"metrics_count": len(metrics)},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class IdempotencyEvaluator(EvaluatorPort):
    """
    Evaluador determinista de idempotencia (IDEMPOTENCY).
    Compara que dos ejecuciones repetidas produzcan exactamente el mismo resultado/huella lógica.
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.IDEMPOTENCY

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        
        # actual_output se espera como dict con run_1 y run_2 o similar
        run_1 = actual_output.get("run_1") if isinstance(actual_output, dict) else None
        run_2 = actual_output.get("run_2") if isinstance(actual_output, dict) else None

        is_identical = (run_1 == run_2) and (run_1 is not None)
        status = EvaluationStatus.PASS if is_identical else EvaluationStatus.FAIL

        metric = EvaluationMetric(
            metric_name="idempotent_runs_match",
            metric_value=is_identical,
            expected_value=True,
            status=status,
            evidence={"run_1": run_1, "run_2": run_2},
        )

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            metrics=(metric,),
            expected_reference=expected,
            actual_reference=actual_output if isinstance(actual_output, dict) else {"raw": str(actual_output)},
            evidence={"is_identical": is_identical},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class EndToEndEvaluator(EvaluatorPort):
    """
    Evaluador determinista End-to-End (END_TO_END / TEMPORAL).
    Verifica que se cumplan simultáneamente estado, traza y policy de una misión.
    """

    @property
    def evaluation_type(self) -> EvaluationType:
        return EvaluationType.END_TO_END

    @property
    def version(self) -> str:
        return "1.0.0"

    def evaluate(
        self,
        case: EvaluationCase,
        actual_output: Any,
        execution_id: str,
        evaluated_component: str,
        trace_reference: Optional[str] = None,
        audit_reference: Optional[str] = None,
        cost_reference: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> EvaluationResult:
        started_at = datetime.now(timezone.utc)
        expected = dict(case.expected_criteria)
        actual_dict = actual_output if isinstance(actual_output, dict) else {}

        metrics = []
        all_passed = True
        has_unknown = False

        # 1. Status check
        if "expected_status" in expected:
            act_st = actual_dict.get("status")
            exp_st = expected["expected_status"]
            if act_st == "UNKNOWN" and exp_st != "UNKNOWN":
                st = EvaluationStatus.UNKNOWN
                has_unknown = True
                all_passed = False
            elif str(act_st) == str(exp_st):
                st = EvaluationStatus.PASS
            else:
                st = EvaluationStatus.FAIL
                all_passed = False
            metrics.append(
                EvaluationMetric(
                    metric_name="e2e_status_check",
                    metric_value=str(act_st),
                    expected_value=exp_st,
                    status=st,
                )
            )

        # 2. Policy check
        if "expected_policy_decision" in expected:
            act_pol = actual_dict.get("policy_decision")
            exp_pol = expected["expected_policy_decision"]
            if act_pol == "UNKNOWN" and exp_pol != "UNKNOWN":
                st = EvaluationStatus.UNKNOWN
                has_unknown = True
                all_passed = False
            elif str(act_pol) == str(exp_pol):
                st = EvaluationStatus.PASS
            else:
                st = EvaluationStatus.FAIL
                all_passed = False
            metrics.append(
                EvaluationMetric(
                    metric_name="e2e_policy_check",
                    metric_value=str(act_pol),
                    expected_value=exp_pol,
                    status=st,
                )
            )

        # 3. Actions count check
        if "expected_actions_count" in expected:
            act_count = actual_dict.get("actions_count", 0)
            exp_count = expected["expected_actions_count"]
            st = EvaluationStatus.PASS if act_count == exp_count else EvaluationStatus.FAIL
            if act_count != exp_count:
                all_passed = False
            metrics.append(
                EvaluationMetric(
                    metric_name="e2e_actions_count",
                    metric_value=act_count,
                    expected_value=exp_count,
                    status=st,
                )
            )

        if all_passed:
            overall_status = EvaluationStatus.PASS
        elif has_unknown:
            overall_status = EvaluationStatus.UNKNOWN
        else:
            overall_status = EvaluationStatus.FAIL

        completed_at = datetime.now(timezone.utc)
        return EvaluationResult(
            result_id=f"eval-res-{uuid.uuid4()}",
            case_id=case.case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            started_at=started_at,
            completed_at=completed_at,
            status=overall_status,
            metrics=tuple(metrics),
            expected_reference=expected,
            actual_reference=actual_dict,
            evidence={"metrics_count": len(metrics)},
            trace_reference=trace_reference,
            audit_reference=audit_reference,
            cost_reference=cost_reference,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evaluator_version=self.version,
        )


class EvaluatorRegistry:
    """
    Registro y despacho determinista de evaluadores según EvaluationType.
    """

    def __init__(self, evaluators: Optional[Sequence[EvaluatorPort]] = None):
        self._evaluators: Dict[EvaluationType, EvaluatorPort] = {}
        if evaluators:
            for ev in evaluators:
                self.register(ev)
        else:
            # Registrar evaluadores por defecto
            self.register(ExactMatchEvaluator())
            self.register(StructuralEvaluator())
            self.register(NumericToleranceEvaluator())
            self.register(StatusEvaluator())
            self.register(PolicyEvaluator())
            self.register(SafetyEvaluator())
            self.register(TraceEvaluator())
            self.register(IdempotencyEvaluator())
            self.register(EndToEndEvaluator())

    def register(self, evaluator: EvaluatorPort) -> None:
        self._evaluators[evaluator.evaluation_type] = evaluator

    def get_evaluator(self, eval_type: EvaluationType) -> Optional[EvaluatorPort]:
        return self._evaluators.get(eval_type)
