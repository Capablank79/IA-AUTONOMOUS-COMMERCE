"""
Servicio de aplicación para Quality Gates (Hito K.6).

Responsabilidades:
- Evalúa resultados producidos por K.4 (Evaluation Harness) y K.5 (Golden Datasets) contra criterios declarativos K.6.
- Inmutabilidad estricta y determinismo.
- Detección de colisiones e inconsistencias de idempotencia (mismo run con inputs divergentes).
- Cálculo exacto de pass_rate mediante aritmética Decimal.
- Verificación de políticas explícitas (missing cases, unknown cases, error cases).
- Verificación de metadatos y manifest checksum de Golden Datasets (K.5).
- Desacoplamiento y emisión de auditoría real K.1 ante decisiones nuevas (sin duplicación en replay).
- Contrato de decisión de despliegue determinista (deployment_allowed).
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from typing import Optional, Sequence, Union, Dict, Any, List

from src.domain.audit.models import AuditRecord, AuditActor, AuditActorType, AuditRecordType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.evaluation.models import BatchEvaluationSummary, EvaluationResult, EvaluationStatus
from src.domain.quality_gate.models import (
    ErrorCasePolicy,
    GateDecisionStatus,
    MissingCasePolicy,
    QualityGateDecision,
    QualityGateDefinition,
    UnknownCasePolicy,
    compute_gate_decision_checksum,
)
from src.domain.quality_gate.ports import QualityGateRepositoryPort
from src.infrastructure.persistence.data.json.quality_gate_repository import GateDecisionConflictError


class QualityGateService:
    """Evalúa resultados ya producidos por K.4/K.5 contra criterios declarativos K.6."""

    def __init__(
        self,
        repository: QualityGateRepositoryPort,
        audit_repository: Optional[AuditRepositoryPort] = None,
    ):
        self.repository = repository
        self.audit_repository = audit_repository

    def register_definition(self, definition: QualityGateDefinition) -> QualityGateDefinition:
        return self.repository.save_definition(definition)

    def get_definition(self, gate_id: str, version: Optional[str] = None) -> Optional[QualityGateDefinition]:
        return self.repository.get_definition(gate_id, version)

    def _compute_input_fingerprint(
        self,
        definition: QualityGateDefinition,
        evaluation_run_id: str,
        results: Sequence[EvaluationResult],
        dataset_id: Optional[str],
        dataset_version: Optional[str],
        dataset_manifest_checksum: Optional[str],
    ) -> str:
        """Calcula una huella digital determinista de los inputs materiales de evaluación."""
        results_summary = [
            f"{r.case_id}:{r.status.value}:{r.evaluator_version}:{r.result_id}"
            for r in sorted(results, key=lambda r: r.case_id)
        ]
        payload = {
            "gate_id": definition.gate_id,
            "gate_version": definition.version,
            "definition_checksum": definition.checksum,
            "evaluation_run_id": str(evaluation_run_id).strip(),
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "dataset_manifest_checksum": dataset_manifest_checksum,
            "results": results_summary,
        }
        dumped = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(dumped.encode("utf-8")).hexdigest()

    def evaluate(
        self,
        definition: QualityGateDefinition,
        evaluation: Union[BatchEvaluationSummary, Sequence[EvaluationResult]],
        evaluation_run_id: str,
        dataset_id: Optional[str] = None,
        dataset_version: Optional[str] = None,
        dataset_manifest_checksum: Optional[str] = None,
        correlation_id: str = "",
        causation_id: Optional[str] = None,
    ) -> QualityGateDecision:
        if not isinstance(evaluation_run_id, str) or not evaluation_run_id.strip():
            raise ValueError("evaluation_run_id cannot be empty.")

        # Validar consistencia con dataset target si la definición lo exige
        if definition.target_dataset_id and dataset_id != definition.target_dataset_id:
            raise ValueError(
                f"Quality Gate target_dataset_id '{definition.target_dataset_id}' does not match the evaluated dataset '{dataset_id}'."
            )
        if definition.target_dataset_version and dataset_version != definition.target_dataset_version:
            raise ValueError(
                f"Quality Gate target_dataset_version '{definition.target_dataset_version}' does not match the evaluated dataset '{dataset_version}'."
            )
        if (
            definition.target_dataset_manifest_checksum
            and dataset_manifest_checksum
            and dataset_manifest_checksum != definition.target_dataset_manifest_checksum
        ):
            raise ValueError(
                f"Dataset manifest checksum '{dataset_manifest_checksum}' does not match expected definition manifest checksum '{definition.target_dataset_manifest_checksum}'."
            )

        # Extraer resultados
        results = tuple(evaluation.results if isinstance(evaluation, BatchEvaluationSummary) else evaluation)
        if not all(isinstance(result, EvaluationResult) for result in results):
            raise TypeError("evaluation must contain only EvaluationResult values.")

        if isinstance(evaluation, BatchEvaluationSummary):
            actual_counts = (
                len(results),
                sum(result.status == EvaluationStatus.PASS for result in results),
                sum(result.status == EvaluationStatus.FAIL for result in results),
                sum(result.status == EvaluationStatus.UNKNOWN for result in results),
                sum(result.status == EvaluationStatus.ERROR for result in results),
            )
            declared_counts = (
                evaluation.total_cases,
                evaluation.passed_count,
                evaluation.failed_count,
                evaluation.unknown_count,
                evaluation.error_count,
            )
            if actual_counts != declared_counts:
                raise ValueError("BatchEvaluationSummary counts do not match its results.")

        by_case = {result.case_id: result for result in results}
        if len(by_case) != len(results):
            raise ValueError("Evaluation results contain duplicate case_id values.")

        # Huella digital canónica de los inputs de evaluación
        input_fingerprint = self._compute_input_fingerprint(
            definition=definition,
            evaluation_run_id=evaluation_run_id,
            results=results,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_manifest_checksum=dataset_manifest_checksum,
        )

        idempotency_key = hashlib.sha256(
            f"{definition.gate_id}:{definition.version}:{evaluation_run_id}".encode("utf-8")
        ).hexdigest()

        # Comprobar idempotencia previa
        existing = self.repository.get_decision_by_idempotency_key(idempotency_key)
        if existing is not None:
            existing_fingerprint = existing.evidence.get("input_fingerprint")
            if existing_fingerprint and existing_fingerprint != input_fingerprint:
                raise GateDecisionConflictError(
                    f"Idempotency conflict: evaluation_run_id '{evaluation_run_id}' was already evaluated with different inputs."
                )
            return existing

        # Evaluar reglas y criterios
        passed = sorted(r.case_id for r in results if r.status == EvaluationStatus.PASS)
        failed = sorted(r.case_id for r in results if r.status == EvaluationStatus.FAIL)
        unknown = sorted(r.case_id for r in results if r.status == EvaluationStatus.UNKNOWN)
        errors = sorted(r.case_id for r in results if r.status == EvaluationStatus.ERROR)
        missing = sorted(set(definition.required_case_ids) - set(by_case))
        critical_failures = sorted(
            case_id for case_id in definition.critical_case_ids
            if case_id not in by_case or by_case[case_id].status != EvaluationStatus.PASS
        )
        incompatible_versions = sorted(
            r.case_id for r in results
            if definition.allowed_evaluator_versions
            and r.evaluator_version not in definition.allowed_evaluator_versions
        )

        # Si no se pasó manifest checksum pero la definición lo requería
        missing_manifest = (
            definition.target_dataset_manifest_checksum is not None
            and dataset_manifest_checksum is None
        )

        evaluated_count = len(results)
        pass_rate = (
            (Decimal(len(passed)) / Decimal(evaluated_count)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if evaluated_count else None
        )

        status = GateDecisionStatus.PASS
        reasons = []

        if missing_manifest:
            status = GateDecisionStatus.ERROR
            reasons.append("Missing required dataset manifest checksum.")

        if incompatible_versions:
            status = GateDecisionStatus.ERROR
            reasons.append(f"Disallowed evaluator versions for cases: {', '.join(incompatible_versions)}")

        if missing:
            reasons.append(f"Missing required cases: {', '.join(missing)}")
            missing_status = {
                MissingCasePolicy.FAIL: GateDecisionStatus.FAIL,
                MissingCasePolicy.UNKNOWN: GateDecisionStatus.UNKNOWN,
                MissingCasePolicy.ERROR: GateDecisionStatus.ERROR,
            }[definition.missing_case_policy]
            status = self._strongest(status, missing_status)

        if critical_failures:
            reasons.append(f"Critical cases did not pass: {', '.join(critical_failures)}")
            status = self._strongest(status, GateDecisionStatus.FAIL)

        if len(failed) > definition.max_failures:
            reasons.append(f"Failures {len(failed)} exceed maximum {definition.max_failures}")
            status = self._strongest(status, GateDecisionStatus.FAIL)

        if len(unknown) > definition.max_unknown:
            reasons.append(f"Unknown results {len(unknown)} exceed maximum {definition.max_unknown}")
            unknown_status = (
                GateDecisionStatus.FAIL
                if definition.unknown_case_policy == UnknownCasePolicy.FAIL
                else GateDecisionStatus.UNKNOWN
            )
            status = self._strongest(status, unknown_status)

        if len(errors) > definition.max_errors:
            reasons.append(f"Errors {len(errors)} exceed maximum {definition.max_errors}")
            error_status = (
                GateDecisionStatus.FAIL
                if definition.error_case_policy == ErrorCasePolicy.FAIL
                else GateDecisionStatus.ERROR
            )
            status = self._strongest(status, error_status)

        if definition.minimum_pass_rate is not None:
            if evaluated_count == 0:
                reasons.append(f"No evaluated cases to satisfy minimum pass rate {definition.minimum_pass_rate}")
                status = self._strongest(status, GateDecisionStatus.FAIL)
            else:
                exact_pass_rate = Decimal(len(passed)) / Decimal(evaluated_count)
                if exact_pass_rate < definition.minimum_pass_rate:
                    reasons.append(f"Pass rate {pass_rate} is below minimum {definition.minimum_pass_rate}")
                    status = self._strongest(status, GateDecisionStatus.FAIL)

        # Si no hay casos evaluados ni casos requeridos, no podemos declarar PASS ciegamente
        if evaluated_count == 0 and not definition.required_case_ids and not definition.critical_case_ids:
            status = GateDecisionStatus.UNKNOWN
            reasons.append("Empty evaluation with no configured cases.")

        decision_id = "gate-decision-" + idempotency_key[:24]
        evidence_dict = {
            "definition_checksum": definition.checksum,
            "input_fingerprint": input_fingerprint,
            "result_ids": sorted(r.result_id for r in results),
            "incompatible_evaluator_version_case_ids": incompatible_versions,
        }

        decision = QualityGateDecision(
            decision_id=decision_id,
            gate_id=definition.gate_id,
            gate_version=definition.version,
            status=status,
            evaluation_run_id=evaluation_run_id,
            total_cases=len(results) + len(missing),
            passed_count=len(passed),
            failed_count=len(failed),
            unknown_count=len(unknown),
            error_count=len(errors),
            evaluated_count=evaluated_count,
            pass_rate=pass_rate,
            failed_case_ids=tuple(failed),
            unknown_case_ids=tuple(unknown),
            error_case_ids=tuple(errors),
            missing_required_case_ids=tuple(missing),
            critical_case_failures=tuple(critical_failures),
            reasons=tuple(reasons),
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_manifest_checksum=dataset_manifest_checksum,
            evidence=evidence_dict,
            trace_reference=self._single_reference(results, "trace_reference"),
            audit_reference=self._single_reference(results, "audit_reference"),
            cost_reference=self._single_reference(results, "cost_reference"),
            correlation_id=correlation_id or evaluation_run_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )

        saved_decision = self.repository.save_decision(decision)

        # Emitir auditoría K.1 si el repositorio de auditoría está configurado
        if self.audit_repository is not None:
            try:
                audit_record = AuditRecord(
                    audit_id=f"audit-{saved_decision.decision_id}",
                    record_type=AuditRecordType.DECISION_CREATED,
                    occurred_at=saved_decision.decided_at,
                    actor=AuditActor(
                        actor_type=AuditActorType.POLICY_ENGINE,
                        actor_id="quality-gate-engine",
                    ),
                    subject_type="quality_gate",
                    subject_id=saved_decision.gate_id,
                    action_or_operation="EVALUATE_QUALITY_GATE",
                    status=saved_decision.status.value,
                    correlation_id=saved_decision.correlation_id or saved_decision.evaluation_run_id,
                    causation_id=saved_decision.causation_id,
                    entity_reference=f"quality_gate_decision:{saved_decision.decision_id}",
                    evidence_reference=saved_decision.checksum,
                    metadata={
                        "decision_id": saved_decision.decision_id,
                        "gate_version": saved_decision.gate_version,
                        "pass_rate": str(saved_decision.pass_rate) if saved_decision.pass_rate is not None else None,
                        "deployment_allowed": saved_decision.deployment_allowed,
                        "passed_count": saved_decision.passed_count,
                        "failed_count": saved_decision.failed_count,
                        "unknown_count": saved_decision.unknown_count,
                        "error_count": saved_decision.error_count,
                    },
                )
                self.audit_repository.append(audit_record)
            except Exception:
                # No bloquear la respuesta de dominio si falla el append de auditoría secundario
                pass

        return saved_decision

    def evaluate_registered(
        self,
        gate_id: str,
        evaluation: Union[BatchEvaluationSummary, Sequence[EvaluationResult]],
        evaluation_run_id: str,
        version: Optional[str] = None,
        **kwargs,
    ) -> QualityGateDecision:
        definition = self.repository.get_definition(gate_id, version)
        if definition is None:
            raise ValueError(f"QualityGateDefinition '{gate_id}' version '{version or 'latest'}' not found.")
        return self.evaluate(definition, evaluation, evaluation_run_id, **kwargs)

    @staticmethod
    def _strongest(current: GateDecisionStatus, candidate: GateDecisionStatus) -> GateDecisionStatus:
        precedence = {
            GateDecisionStatus.PASS: 0,
            GateDecisionStatus.UNKNOWN: 1,
            GateDecisionStatus.FAIL: 2,
            GateDecisionStatus.ERROR: 3,
        }
        return candidate if precedence[candidate] > precedence[current] else current

    @staticmethod
    def _single_reference(results: Sequence[EvaluationResult], attribute: str) -> Optional[str]:
        values = sorted({getattr(result, attribute) for result in results if getattr(result, attribute)})
        return values[0] if len(values) == 1 else None
