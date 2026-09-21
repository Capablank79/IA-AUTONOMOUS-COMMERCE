"""
Servicio de Aplicación para Self-monitoring (Hito R.7 — Advanced Autonomy).

Responsabilidades:
1. Observar el estado operacional de una misión agregando señales existentes (R.1–R.6, K.3, P.7, P.11, O.7, M.*, etc.).
2. Evaluar la salud de forma determinista (HEALTHY, DEGRADED, AT_RISK, BLOCKED, UNKNOWN).
3. Semántica estricta de incertidumbre: UNKNOWN != HEALTHY y UNKNOWN != 0.
4. Detectar anomalías operacionales:
   - Stale heartbeat / lease expirado (R.6)
   - Stalled mission evaluado con ClockPort sin sleeps
   - Fallos técnicos repetidos y failure rate (con denominador > 0)
   - Conflictos de coordinación (R.4)
   - Presión y límites de delegación (R.5) e inestabilidad de plan (R.1)
   - Fallo de sub-misiones obligatorias (R.2)
   - Indisponibilidad de agentes especialistas (R.3)
   - Presión y agotamiento de budget / cuotas / rate-limits
   - Cost spikes con datos homogéneos en Decimal (sin mezclar divisas)
5. Producir decisiones de control interno acotadas (NONE, CONTINUE_WITH_WARNING, PAUSE, BLOCK, REQUEST_REPLAN, REQUEST_DELEGATION, ESCALATE).
6. Evitar loops infinitos de replan/delegación respetando límites máximos de remediación.
7. NO permitir evasión de políticas (POLICY_DENIED / EMERGENCY_STOP nunca desencadenan replan ni delegación permisiva).
8. NO realizar mutaciones externas automáticas.
9. Aislamiento multi-tenant estricto vía CrossTenantGuard.
10. Auditoría inmutable de eventos K.1 / K.2 y anti-CoT.
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
import uuid
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List, Set

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.domain.audit.models import AuditRecordType
from src.domain.self_monitoring.models import (
    MissionHealthStatus,
    SignalType,
    SignalSource,
    SignalSeverity,
    SignalCompleteness,
    DegradationReasonCode,
    SelfMonitoringAction,
    HealthSignal,
    SelfMonitoringDecision,
    MissionHealthSnapshot,
    HealthAssessment,
    SelfMonitoringPolicy,
)
from src.domain.self_monitoring.ports import (
    SelfMonitoringRepositoryPort,
    SignalCollectorPort,
    SelfMonitoringAuditPort,
)

logger = logging.getLogger("SelfMonitoringService")


class InMemorySelfMonitoringRepository(SelfMonitoringRepositoryPort):
    """
    Implementación en memoria aislada por tenant para almacenamiento de snapshots y assessments.
    """

    def __init__(self):
        self._snapshots: Dict[str, List[MissionHealthSnapshot]] = {}
        self._assessments: Dict[str, List[HealthAssessment]] = {}

    def _get_tenant_key(self, tenant_id: str, mission_id: str) -> str:
        return f"{tenant_id}::{mission_id}"

    def save_snapshot(
        self,
        snapshot: MissionHealthSnapshot,
        context: TenantContext,
    ) -> None:
        CrossTenantGuard.assert_same_tenant(context, snapshot.tenant_id, "save_snapshot")
        key = self._get_tenant_key(context.tenant_id, snapshot.mission_id)
        if key not in self._snapshots:
            self._snapshots[key] = []
        self._snapshots[key].append(snapshot)

    def get_latest_snapshot(
        self,
        mission_id: str,
        context: TenantContext,
    ) -> Optional[MissionHealthSnapshot]:
        CrossTenantGuard.ensure_tenant_context(context)
        key = self._get_tenant_key(context.tenant_id, mission_id)
        items = self._snapshots.get(key, [])
        return items[-1] if items else None

    def save_assessment(
        self,
        assessment: HealthAssessment,
        context: TenantContext,
    ) -> None:
        CrossTenantGuard.assert_same_tenant(context, assessment.tenant_id, "save_assessment")
        key = self._get_tenant_key(context.tenant_id, assessment.mission_id)
        if key not in self._assessments:
            self._assessments[key] = []
        self._assessments[key].append(assessment)

    def get_latest_assessment(
        self,
        mission_id: str,
        context: TenantContext,
    ) -> Optional[HealthAssessment]:
        CrossTenantGuard.ensure_tenant_context(context)
        key = self._get_tenant_key(context.tenant_id, mission_id)
        items = self._assessments.get(key, [])
        return items[-1] if items else None

    def list_assessments(
        self,
        mission_id: str,
        context: TenantContext,
        limit: int = 50,
    ) -> Sequence[HealthAssessment]:
        CrossTenantGuard.ensure_tenant_context(context)
        key = self._get_tenant_key(context.tenant_id, mission_id)
        items = self._assessments.get(key, [])
        return tuple(items[-limit:])


class SelfMonitoringService:
    """
    Servicio de orquestación de Auto-monitoreo de Misiones Autónomas (R.7).
    """

    def __init__(
        self,
        repository: SelfMonitoringRepositoryPort,
        clock: ClockPort,
        policy: Optional[SelfMonitoringPolicy] = None,
        audit_adapter: Optional[SelfMonitoringAuditPort] = None,
        signal_collector: Optional[SignalCollectorPort] = None,
    ):
        self._repository = repository
        self._clock = clock
        self._policy = policy or SelfMonitoringPolicy()
        self._audit_adapter = audit_adapter
        self._signal_collector = signal_collector

    def assess_mission_health(
        self,
        mission_id: str,
        context: TenantContext,
        signals: Optional[Sequence[HealthSignal]] = None,
    ) -> HealthAssessment:
        """
        Evalúa integralmente la salud de una misión autónoma a partir de señales provistas o recolectadas.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(mission_id, "mission_id")

        now = self._clock.now()

        # 1. Recolectar señales si no se entregaron explícitamente
        collected_signals: List[HealthSignal] = []
        if signals is not None:
            collected_signals.extend(signals)
        elif self._signal_collector is not None:
            collected_signals.extend(self._signal_collector.collect_signals(mission_id, context))

        for signal in collected_signals:
            if signal.tenant_id is not None:
                CrossTenantGuard.assert_same_tenant(context, signal.tenant_id, "assess_mission_health.signal")
            if signal.mission_id is not None and signal.mission_id != mission_id:
                raise ValueError("Signal mission mismatch: HealthSignal mission_id does not match assessed mission")

        # 2. Obtener evaluación previa para historial y remediation bounds
        prev_assessment = self._repository.get_latest_assessment(mission_id, context)
        prev_status = prev_assessment.status if prev_assessment else None
        prev_remediations = prev_assessment.remediation_counter if prev_assessment else 0

        # 3. Evaluar señales de forma determinista
        reason_codes: List[DegradationReasonCode] = []
        status: MissionHealthStatus = MissionHealthStatus.HEALTHY
        requires_escalation = False
        escalation_severity: Optional[str] = None

        is_stalled = False
        is_stale_heartbeat = False
        is_budget_exceeded = False
        is_emergency_stopped = False
        is_policy_denied = False
        active_worker_id: Optional[str] = None
        progress_percentage: Optional[float] = None

        # Si no hay señales en absoluto -> UNKNOWN
        if not collected_signals:
            status = MissionHealthStatus.UNKNOWN
            reason_codes.append(DegradationReasonCode.INSUFFICIENT_SIGNALS_UNKNOWN)
        else:
            # Procesar cada señal
            has_unknown_signal = False
            has_critical = False
            has_high = False
            has_warning = False
            complete_signal_types: Set[SignalType] = set()

            for sig in collected_signals:
                scoped = sig.tenant_id == context.tenant_id and sig.mission_id == mission_id
                complete = sig.completeness == SignalCompleteness.COMPLETE and not sig.missing_fields
                if scoped and complete:
                    complete_signal_types.add(sig.signal_type)
                if not scoped or not complete or sig.severity == SignalSeverity.UNKNOWN or sig.confidence < 0.5:
                    has_unknown_signal = True
                    if sig.completeness == SignalCompleteness.PARTIAL or sig.missing_fields:
                        if DegradationReasonCode.INCOMPLETE_SIGNAL_DATA not in reason_codes:
                            reason_codes.append(DegradationReasonCode.INCOMPLETE_SIGNAL_DATA)

                # Analizar tipos específicos
                if sig.signal_type == SignalType.EMERGENCY_STOP_STATUS:
                    if sig.value is True or str(sig.value).upper() == "TRIGGERED":
                        is_emergency_stopped = True
                        reason_codes.append(DegradationReasonCode.EMERGENCY_STOP_TRIGGERED)
                        has_critical = True

                elif sig.signal_type == SignalType.POLICY_COMPLIANCE:
                    if sig.value is False or str(sig.value).upper() in ("DENIED", "POLICY_DENIED", "VIOLATION"):
                        is_policy_denied = True
                        reason_codes.append(DegradationReasonCode.POLICY_DENIED_VIOLATION)
                        has_critical = True

                elif sig.signal_type == SignalType.HEARTBEAT_LIVENESS:
                    # Verificar si es stale
                    if isinstance(sig.value, dict) and "last_seen_at" in sig.value:
                        last_seen = sig.value["last_seen_at"]
                        if isinstance(last_seen, datetime):
                            elapsed = (now - last_seen).total_seconds()
                            if elapsed > self._policy.stale_heartbeat_threshold_seconds:
                                is_stale_heartbeat = True
                                reason_codes.append(DegradationReasonCode.STALE_HEARTBEAT)
                                has_high = True
                    elif sig.severity in (SignalSeverity.HIGH, SignalSeverity.CRITICAL):
                        is_stale_heartbeat = True
                        reason_codes.append(DegradationReasonCode.STALE_HEARTBEAT)
                        has_high = True

                elif sig.signal_type == SignalType.LEASE_VALIDITY:
                    if sig.value is False or str(sig.value).upper() == "EXPIRED":
                        reason_codes.append(DegradationReasonCode.LEASE_EXPIRED)
                        has_high = True

                elif sig.signal_type == SignalType.STALL_TEMPORAL:
                    if isinstance(sig.value, Mapping):
                        last_progress_at = sig.value.get("last_progress_at")
                        active_work = sig.value.get("active_nonterminal_work")
                        if not isinstance(last_progress_at, datetime) or last_progress_at.tzinfo is None or not isinstance(active_work, bool):
                            has_unknown_signal = True
                        elif active_work:
                            elapsed = (now - last_progress_at).total_seconds()
                            if elapsed >= self._policy.stall_timeout_seconds:
                                is_stalled = True
                                reason_codes.append(DegradationReasonCode.MISSION_STALLED)
                                has_high = True
                    else:
                        has_unknown_signal = True

                elif sig.signal_type == SignalType.TECHNICAL_FAILURE_RATE:
                    if isinstance(sig.value, dict):
                        failures = sig.value.get("failures", 0)
                        total = sig.value.get("total", 0)
                        if total <= 0 or total < self._policy.min_failure_samples:
                            # Denominador insuficiente o cero -> UNKNOWN semántico
                            has_unknown_signal = True
                        else:
                            rate = failures / total
                            if rate >= self._policy.failure_rate_threshold:
                                reason_codes.append(DegradationReasonCode.REPEATED_TECHNICAL_FAILURES)
                                has_high = True
                    elif sig.severity in (SignalSeverity.HIGH, SignalSeverity.CRITICAL):
                        reason_codes.append(DegradationReasonCode.REPEATED_TECHNICAL_FAILURES)
                        has_high = True

                elif sig.signal_type == SignalType.COORDINATION_CONFLICT:
                    if sig.value is True or sig.severity in (SignalSeverity.WARNING, SignalSeverity.HIGH, SignalSeverity.CRITICAL):
                        reason_codes.append(DegradationReasonCode.COORDINATION_CONFLICT_DETECTED)
                        has_high = True

                elif sig.signal_type == SignalType.DELEGATION_PRESSURE:
                    count = sig.value if isinstance(sig.value, int) else sig.metadata.get("count", 0)
                    if count >= self._policy.max_delegation_requests:
                        reason_codes.append(DegradationReasonCode.REPEATED_DELEGATION_NEAR_BOUND)
                        has_high = True
                    elif sig.severity == SignalSeverity.WARNING:
                        has_warning = True

                elif sig.signal_type == SignalType.REPLAN_PRESSURE:
                    count = sig.value if isinstance(sig.value, int) else sig.metadata.get("count", 0)
                    if count >= self._policy.max_replan_requests:
                        reason_codes.append(DegradationReasonCode.REPEATED_REPLAN_NEAR_BOUND)
                        has_high = True
                    elif sig.severity == SignalSeverity.WARNING:
                        has_warning = True

                elif sig.signal_type == SignalType.CHILD_MISSION_HEALTH:
                    if sig.value in ("FAILED", "BLOCKED") or sig.severity == SignalSeverity.CRITICAL:
                        is_required = sig.metadata.get("required", True)
                        if is_required:
                            reason_codes.append(DegradationReasonCode.REQUIRED_CHILD_MISSION_FAILED)
                            has_critical = True
                        else:
                            has_warning = True

                elif sig.signal_type == SignalType.AGENT_AVAILABILITY:
                    if sig.value is False or str(sig.value).upper() in ("UNAVAILABLE", "DEGRADED", "OFFLINE"):
                        reason_codes.append(DegradationReasonCode.SPECIALIST_AGENT_UNAVAILABLE)
                        has_high = True
                    elif sig.severity == SignalSeverity.UNKNOWN:
                        has_unknown_signal = True

                elif sig.signal_type == SignalType.BUDGET_PRESSURE:
                    if isinstance(sig.value, dict):
                        consumed = sig.value.get("consumed", Decimal("0"))
                        allocated = sig.value.get("allocated", Decimal("0"))
                        if allocated > Decimal("0"):
                            ratio = float(consumed / allocated)
                            if ratio >= 1.0:
                                is_budget_exceeded = True
                                reason_codes.append(DegradationReasonCode.BUDGET_HARD_LIMIT_REACHED)
                                has_critical = True
                            elif ratio >= self._policy.budget_warning_threshold_ratio:
                                reason_codes.append(DegradationReasonCode.BUDGET_PRESSURE_HIGH)
                                has_warning = True
                    elif sig.severity == SignalSeverity.CRITICAL:
                        is_budget_exceeded = True
                        reason_codes.append(DegradationReasonCode.BUDGET_HARD_LIMIT_REACHED)
                        has_critical = True
                    elif sig.severity == SignalSeverity.WARNING:
                        has_warning = True

                elif sig.signal_type == SignalType.COST_ANOMALY:
                    required_cost_fields = {
                        "baseline_cost", "observed_cost", "baseline_currency", "observed_currency"
                    }
                    if sig.source != SignalSource.K3_COST_TRACKING or not isinstance(sig.value, Mapping):
                        has_unknown_signal = True
                        if DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN not in reason_codes:
                            reason_codes.append(DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN)
                    elif not required_cost_fields.issubset(sig.value.keys()):
                        has_unknown_signal = True
                        if DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN not in reason_codes:
                            reason_codes.append(DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN)
                    else:
                        base_curr = sig.value["baseline_currency"]
                        obs_curr = sig.value["observed_currency"]
                        baseline = sig.value["baseline_cost"]
                        observed = sig.value["observed_cost"]
                        if not isinstance(base_curr, str) or not base_curr.strip() or not isinstance(obs_curr, str) or not obs_curr.strip():
                            has_unknown_signal = True
                            if DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN not in reason_codes:
                                reason_codes.append(DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN)
                        elif base_curr != obs_curr:
                            reason_codes.append(DegradationReasonCode.MIXED_CURRENCY_DATA)
                            has_unknown_signal = True
                        elif not isinstance(baseline, Decimal) or not isinstance(observed, Decimal):
                            has_unknown_signal = True
                            if DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN not in reason_codes:
                                reason_codes.append(DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN)
                        elif baseline < Decimal("0") or observed < Decimal("0"):
                            has_unknown_signal = True
                            if DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN not in reason_codes:
                                reason_codes.append(DegradationReasonCode.COST_DATA_INVALID_OR_UNKNOWN)
                        elif baseline == Decimal("0") and observed > Decimal("0"):
                            reason_codes.append(DegradationReasonCode.COST_SPIKE_DETECTED)
                            has_high = True
                        elif baseline > Decimal("0") and observed >= (baseline * Decimal(str(self._policy.cost_spike_multiplier_threshold))):
                            reason_codes.append(DegradationReasonCode.COST_SPIKE_DETECTED)
                            has_high = True

                elif sig.signal_type == SignalType.QUOTA_SATURATION:
                    if sig.value is True or str(sig.value).upper() == "EXHAUSTED" or sig.severity == SignalSeverity.CRITICAL:
                        reason_codes.append(DegradationReasonCode.QUOTA_EXHAUSTED)
                        has_critical = True

                elif sig.signal_type == SignalType.RATE_LIMIT_PRESSURE:
                    if sig.value is True or str(sig.value).upper() == "SATURATED" or sig.severity in (SignalSeverity.HIGH, SignalSeverity.CRITICAL):
                        reason_codes.append(DegradationReasonCode.RATE_LIMIT_SATURATED)
                        has_high = True

                elif sig.signal_type == SignalType.LATENCY_ANOMALY:
                    if sig.severity in (SignalSeverity.HIGH, SignalSeverity.CRITICAL):
                        reason_codes.append(DegradationReasonCode.LATENCY_SPIKE)
                        has_warning = True

                elif sig.signal_type == SignalType.EXECUTION_PROGRESS:
                    if isinstance(sig.value, (int, float)):
                        progress_percentage = float(sig.value)

            missing_required = set(self._policy.required_signal_types) - complete_signal_types
            if missing_required:
                has_unknown_signal = True
                if DegradationReasonCode.MISSING_REQUIRED_SIGNALS not in reason_codes:
                    reason_codes.append(DegradationReasonCode.MISSING_REQUIRED_SIGNALS)
            if has_unknown_signal and DegradationReasonCode.INSUFFICIENT_SIGNALS_UNKNOWN not in reason_codes:
                reason_codes.append(DegradationReasonCode.INSUFFICIENT_SIGNALS_UNKNOWN)

            # Clasificar estado global. Las barreras duras mantienen prioridad sobre incertidumbre.
            if is_emergency_stopped or is_policy_denied or is_budget_exceeded or has_critical:
                status = MissionHealthStatus.BLOCKED
            elif has_high or is_stalled or is_stale_heartbeat:
                status = MissionHealthStatus.AT_RISK
            elif has_warning:
                status = MissionHealthStatus.DEGRADED
            elif has_unknown_signal:
                status = MissionHealthStatus.UNKNOWN
            else:
                status = MissionHealthStatus.HEALTHY
                if not reason_codes:
                    reason_codes.append(DegradationReasonCode.NONE_HEALTHY)

        # 4. Determinar acción de control interno permitida (Bounded Safe Response)
        remediation_counter = prev_remediations
        remediation_limit_reached = remediation_counter >= self._policy.max_remediation_actions_per_mission

        decision_action = SelfMonitoringAction.NONE
        rationale = ""
        suggested_target: Optional[str] = None
        suggested_payload: Dict[str, Any] = {}

        if status == MissionHealthStatus.HEALTHY:
            decision_action = SelfMonitoringAction.NONE
            rationale = "Execution health is normal; no intervention required."

        elif status == MissionHealthStatus.UNKNOWN:
            decision_action = SelfMonitoringAction.CONTINUE_WITH_WARNING
            rationale = "Insufficient or ambiguous health signals; continuing under surveillance."

        elif status == MissionHealthStatus.BLOCKED:
            decision_action = SelfMonitoringAction.BLOCK
            rationale = "Critical unrecoverable condition or safety violation detected; execution must be blocked."
            requires_escalation = True
            escalation_severity = "CRITICAL"

        elif status == MissionHealthStatus.AT_RISK:
            if remediation_limit_reached:
                decision_action = SelfMonitoringAction.ESCALATE
                rationale = "Remediation action limit reached; escalating to avoid endless loops."
                requires_escalation = True
                escalation_severity = "HIGH"
            elif DegradationReasonCode.SPECIALIST_AGENT_UNAVAILABLE in reason_codes:
                decision_action = SelfMonitoringAction.REQUEST_DELEGATION
                rationale = "Assigned specialist agent is unavailable; requesting dynamic delegation under R.5."
                remediation_counter += 1
                suggested_target = "specialist_delegation"
            elif DegradationReasonCode.REPEATED_TECHNICAL_FAILURES in reason_codes:
                decision_action = SelfMonitoringAction.REQUEST_REPLAN
                rationale = "Repeated technical step failures detected; requesting DAG replan under R.1."
                remediation_counter += 1
                suggested_target = "dag_replan"
            elif DegradationReasonCode.STALE_HEARTBEAT in reason_codes or DegradationReasonCode.MISSION_STALLED in reason_codes:
                decision_action = SelfMonitoringAction.PAUSE
                rationale = "Worker liveness stale or progress stalled; initiating recoverable pause and checkpoint under R.6."
                remediation_counter += 1
            else:
                decision_action = SelfMonitoringAction.PAUSE
                rationale = "High execution risk detected; pausing for investigation."
                remediation_counter += 1

        elif status == MissionHealthStatus.DEGRADED:
            decision_action = SelfMonitoringAction.CONTINUE_WITH_WARNING
            rationale = "Moderate degradation observed; continuing with enhanced warning monitoring."

        decision = SelfMonitoringDecision(
            action=decision_action,
            health_status=status,
            reason_codes=tuple(reason_codes),
            rationale=rationale,
            evaluated_at=now,
            suggested_target=suggested_target,
            suggested_payload=suggested_payload,
            requires_escalation=requires_escalation,
            escalation_severity=escalation_severity,
            is_bounded=not remediation_limit_reached,
        )

        snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"
        snapshot = MissionHealthSnapshot(
            snapshot_id=snapshot_id,
            mission_id=mission_id,
            tenant_id=context.tenant_id,
            health_status=status,
            signals=tuple(collected_signals),
            reason_codes=tuple(reason_codes),
            captured_at=now,
            is_stalled=is_stalled,
            is_stale_heartbeat=is_stale_heartbeat,
            is_budget_exceeded=is_budget_exceeded,
            is_emergency_stopped=is_emergency_stopped,
            is_policy_denied=is_policy_denied,
            active_worker_id=active_worker_id,
            progress_percentage=progress_percentage,
        )

        assessment_id = f"assess-{uuid.uuid4().hex[:12]}"
        assessment = HealthAssessment(
            assessment_id=assessment_id,
            mission_id=mission_id,
            tenant_id=context.tenant_id,
            status=status,
            snapshot=snapshot,
            decision=decision,
            evaluated_at=now,
            previous_status=prev_status,
            remediation_counter=remediation_counter,
            remediation_limit_reached=remediation_limit_reached,
        )

        # 5. Persistir en repositorio
        self._repository.save_snapshot(snapshot, context)
        self._repository.save_assessment(assessment, context)

        # 6. Auditoría y Trazabilidad (K.1 / K.2 / P.8)
        if self._audit_adapter is not None:
            self._audit_adapter.record_assessment_audited(assessment, context)
            if decision.action not in (SelfMonitoringAction.NONE, SelfMonitoringAction.CONTINUE_WITH_WARNING):
                self._audit_adapter.record_action_requested(assessment, decision, context)
            if requires_escalation:
                self._audit_adapter.record_escalation(assessment, decision, context)
            if prev_status in (MissionHealthStatus.DEGRADED, MissionHealthStatus.AT_RISK) and status == MissionHealthStatus.HEALTHY:
                self._audit_adapter.record_recovery(assessment, prev_status, context)

        return assessment
