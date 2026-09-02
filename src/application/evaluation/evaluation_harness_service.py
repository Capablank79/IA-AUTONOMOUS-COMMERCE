"""
Servicio de Aplicación para el Arnés de Evaluación (Evaluation Harness - Hito K.4).

Responsabilidades:
- Orquestar la ejecución declarativa de casos de evaluación unitarios o en batch.
- Despachar al evaluador determinista correspondiente según EvaluationType.
- Preservar semánticas de PASS, FAIL, UNKNOWN y ERROR con total aislamiento de fallos.
- Persistir inmutablemente casos y resultados en EvaluationRepositoryPort.
- Enlazar trazabilidad con K.1 Audit Trail, K.2 Agent Trace y K.3 Cost Tracking.
- Garantizar reproducibilidad e idempotencia de evaluación.
- Proveer resúmenes agregados BatchEvaluationSummary deterministas.
"""

from datetime import datetime, timezone
import logging
from typing import Optional, List, Dict, Any, Union, Sequence, Callable
import uuid

from src.domain.evaluation.models import (
    EvaluationCase,
    EvaluationResult,
    EvaluationMetric,
    EvaluationStatus,
    EvaluationType,
    BatchEvaluationSummary,
)
from src.domain.evaluation.ports import (
    EvaluatorPort,
    EvaluationTargetPort,
    EvaluationRepositoryPort,
)
from src.domain.evaluation.evaluators import EvaluatorRegistry
from src.domain.audit.models import AuditRecordType, AuditActor, AuditActorType
from src.domain.audit.ports import AuditRepositoryPort

logger = logging.getLogger(__name__)


class CallableTargetAdapter(EvaluationTargetPort):
    """
    Adaptador genérico y controlado que encapsula cualquier función/callable Python como EvaluationTargetPort.
    """

    def __init__(self, target_callable: Callable[[EvaluationCase], Any], name: str = "CallableTarget"):
        self._target_callable = target_callable
        self._name = name

    @property
    def component_name(self) -> str:
        return self._name

    def execute(self, case: EvaluationCase) -> Dict[str, Any]:
        output = self._target_callable(case)
        if isinstance(output, dict) and "output" in output:
            return output
        return {
            "output": output,
            "execution_id": f"exec-{uuid.uuid4()}",
            "trace_reference": None,
            "audit_reference": None,
            "cost_reference": None,
            "correlation_id": "",
            "metadata": {},
        }


class EvaluationHarnessService:
    """
    Servicio principal del Arnés de Evaluación (K.4).
    """

    def __init__(
        self,
        repository: EvaluationRepositoryPort,
        evaluator_registry: Optional[EvaluatorRegistry] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        isolate_failures: bool = True,
    ):
        self.repository = repository
        self.registry = evaluator_registry or EvaluatorRegistry()
        self.audit_repository = audit_repository
        self.isolate_failures = isolate_failures

    def run_case(
        self,
        case: EvaluationCase,
        target: Union[EvaluationTargetPort, Callable[[EvaluationCase], Any]],
        persist_case: bool = True,
    ) -> EvaluationResult:
        """
        Ejecuta un caso de evaluación individual contra el target especificado.
        """
        if persist_case:
            try:
                self.repository.save_case(case)
            except Exception as e:
                logger.warning(f"Could not persist EvaluationCase {case.case_id}: {e}")

        # Adaptar target si es un callable
        if isinstance(target, EvaluationTargetPort):
            target_port = target
        elif callable(target):
            target_port = CallableTargetAdapter(target)
        else:
            raise TypeError(f"Target must be EvaluationTargetPort or callable, got {type(target)}")

        started_at = datetime.now(timezone.utc)
        execution_id = f"eval-exec-{uuid.uuid4()}"
        evaluated_component = target_port.component_name

        try:
            # 1. Ejecutar el sistema bajo evaluación
            target_resp = target_port.execute(case)
            actual_output = target_resp.get("output") if isinstance(target_resp, dict) else target_resp
            exec_id = target_resp.get("execution_id", execution_id) if isinstance(target_resp, dict) else execution_id
            trace_ref = target_resp.get("trace_reference") if isinstance(target_resp, dict) else None
            audit_ref = target_resp.get("audit_reference") if isinstance(target_resp, dict) else None
            cost_ref = target_resp.get("cost_reference") if isinstance(target_resp, dict) else None
            correlation_id = target_resp.get("correlation_id", "") if isinstance(target_resp, dict) else ""
            causation_id = target_resp.get("causation_id") if isinstance(target_resp, dict) else None

            # 2. Obtener el evaluador determinista
            evaluator = self.registry.get_evaluator(case.evaluation_type)
            if not evaluator:
                raise ValueError(f"No registered evaluator for type {case.evaluation_type}")

            # 3. Evaluar
            result = evaluator.evaluate(
                case=case,
                actual_output=actual_output,
                execution_id=exec_id,
                evaluated_component=evaluated_component,
                trace_reference=trace_ref,
                audit_reference=audit_ref,
                cost_reference=cost_ref,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )

        except Exception as exc:
            if not self.isolate_failures:
                raise
            logger.error(f"Error executing evaluation case {case.case_id}: {exc}", exc_info=True)
            completed_at = datetime.now(timezone.utc)
            result = EvaluationResult(
                result_id=f"eval-res-err-{uuid.uuid4()}",
                case_id=case.case_id,
                execution_id=execution_id,
                evaluated_component=evaluated_component,
                started_at=started_at,
                completed_at=completed_at,
                status=EvaluationStatus.ERROR,
                metrics=(
                    EvaluationMetric(
                        metric_name="execution_error",
                        metric_value=str(exc),
                        status=EvaluationStatus.ERROR,
                        evidence={"error_type": type(exc).__name__, "message": str(exc)},
                    ),
                ),
                expected_reference=dict(case.expected_criteria),
                actual_reference={"error": str(exc)},
                evidence={"exception": str(exc)},
                evaluator_version="1.0.0",
            )

        # Persistir resultado
        try:
            self.repository.save_result(result)
        except Exception as e:
            logger.error(f"Could not persist EvaluationResult {result.result_id}: {e}")

        # Enlace opcional con AuditTrail K.1
        if self.audit_repository:
            try:
                from src.domain.audit.models import AuditRecord, AuditActor, AuditActorType, AuditRecordType
                audit_record = AuditRecord(
                    audit_id=f"audit-eval-{uuid.uuid4()}",
                    record_type=AuditRecordType.DECISION_CREATED,
                    occurred_at=datetime.now(timezone.utc),
                    actor=AuditActor(
                        actor_type=AuditActorType.SYSTEM,
                        actor_id="evaluation_harness_service",
                        details={"service": "EvaluationHarnessService"},
                    ),
                    subject_type="EVALUATION_RESULT",
                    subject_id=result.result_id,
                    action_or_operation="EXECUTE_EVALUATION_CASE",
                    status=result.status.value,
                    correlation_id=result.correlation_id or execution_id,
                    provenance="EVALUATION_HARNESS",
                    evidence_reference=case.case_id,
                )
                self.audit_repository.append(audit_record)
            except Exception as e:
                logger.warning(f"Could not emit audit record for evaluation {result.result_id}: {e}")

        return result

    def run_batch(
        self,
        cases: Sequence[EvaluationCase],
        target: Union[EvaluationTargetPort, Callable[[EvaluationCase], Any]],
        persist_cases: bool = True,
    ) -> BatchEvaluationSummary:
        """
        Ejecuta un lote (batch) de casos de forma secuencial y ordenada, aislando errores.
        """
        started_at = datetime.now(timezone.utc)
        results = []
        passed = 0
        failed = 0
        unknown = 0
        errors = 0

        for c in cases:
            res = self.run_case(case=c, target=target, persist_case=persist_cases)
            results.append(res)
            if res.status == EvaluationStatus.PASS:
                passed += 1
            elif res.status == EvaluationStatus.FAIL:
                failed += 1
            elif res.status == EvaluationStatus.UNKNOWN:
                unknown += 1
            elif res.status == EvaluationStatus.ERROR:
                errors += 1

        completed_at = datetime.now(timezone.utc)
        return BatchEvaluationSummary(
            total_cases=len(cases),
            passed_count=passed,
            failed_count=failed,
            unknown_count=unknown,
            error_count=errors,
            results=tuple(results),
            started_at=started_at,
            completed_at=completed_at,
        )

    def get_result(self, result_id: str) -> Optional[EvaluationResult]:
        """Recupera un resultado por ID."""
        return self.repository.get_result(result_id)

    def list_results(
        self,
        case_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        evaluated_component: Optional[str] = None,
        status: Optional[EvaluationStatus] = None,
        limit: int = 100,
    ) -> List[EvaluationResult]:
        """Lista resultados con filtros."""
        return self.repository.list_results(
            case_id=case_id,
            execution_id=execution_id,
            evaluated_component=evaluated_component,
            status=status,
            limit=limit,
        )
