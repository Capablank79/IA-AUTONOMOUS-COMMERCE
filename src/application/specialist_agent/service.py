from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
from typing import Any, Dict, Mapping, Optional

from src.domain.agent_trace.models import StepType, TraceStatus
from src.domain.audit.models import AuditActor, AuditActorType, AuditRecord, AuditRecordType
from src.domain.mission.models import LoopAction, LoopDecision, LoopState, Mission, MissionStatus
from src.domain.mission.ports import ActionExecutor
from src.domain.planning.models import PlanStep
from src.domain.security.models import sanitize_security_data
from src.domain.specialist_agent.models import (
    AgentAvailability, AgentExecutionContext, AgentExecutionFailureType,
    AgentExecutionResult, AgentExecutionStatus, AgentSelectionResult,
    AgentSelectionStatus, SpecialistAgentDefinition,
)
from src.domain.specialist_agent.registry import SpecialistAgentRegistry
from src.domain.sub_mission.models import SubMissionFailureType, SubMissionResultContract
from src.domain.tenant.models import TenantContext


class SpecialistAgentService:
    def __init__(
        self,
        registry: SpecialistAgentRegistry,
        executor_bindings: Mapping[str, ActionExecutor],
        audit_repository=None,
        trace_service=None,
        pre_execution_guard=None,
        usage_hook=None,
    ):
        if not isinstance(executor_bindings, Mapping) or not executor_bindings:
            raise ValueError("executor_bindings must be a non-empty mapping")
        checked = {}
        for key, executor in executor_bindings.items():
            if not isinstance(executor, ActionExecutor):
                raise ValueError("each executor binding must implement ActionExecutor")
            if not getattr(executor, "is_guarded_executor", False):
                raise ValueError("specialist executors must be guarded ActionExecutor boundaries")
            checked[str(key)] = executor
        self.registry = registry
        self.executor_bindings = checked
        self.audit_repository = audit_repository
        self.trace_service = trace_service
        self.pre_execution_guard = pre_execution_guard
        self.usage_hook = usage_hook
        self._results: Dict[tuple, tuple[str, AgentExecutionResult]] = {}
        self._lock = threading.RLock()

    def _evaluate(self, agent: SpecialistAgentDefinition, capability_id: str, action_type=None, tool_id=None, max_cost=None):
        capability = agent.get_capability(capability_id)
        if capability is None:
            return "CAPABILITY_UNAVAILABLE"
        if not agent.policy_eligible:
            return "POLICY_DENIED"
        if agent.availability not in (AgentAvailability.AVAILABLE, AgentAvailability.DEGRADED):
            return f"AVAILABILITY_{agent.availability.value}"
        if action_type and (action_type not in capability.action_types or action_type not in agent.allowed_action_types):
            return "ACTION_NOT_ALLOWED"
        if tool_id and (tool_id not in capability.tool_ids or tool_id not in agent.allowed_tool_ids):
            return "TOOL_NOT_ALLOWED"
        if agent.executor_key not in self.executor_bindings:
            return "EXECUTOR_BINDING_UNAVAILABLE"
        if max_cost is not None:
            if agent.estimated_cost is None:
                return "COST_UNKNOWN"
            if agent.estimated_cost > max_cost:
                return "BUDGET_EXHAUSTED"
        return None

    def select_agent(self, tenant_id, capability_id, action_type=None, tool_id=None, max_cost=None, candidate_agent_id=None):
        if candidate_agent_id is not None:
            agent = self.registry.get(tenant_id, candidate_agent_id)
            if agent is None:
                return AgentSelectionResult(AgentSelectionStatus.BLOCKED, capability_id, reason="AGENT_TENANT_MISMATCH")
            reason = self._evaluate(agent, capability_id, action_type, tool_id, max_cost)
            if reason:
                return AgentSelectionResult(AgentSelectionStatus.BLOCKED, capability_id, agent_id=candidate_agent_id, reason=reason)
            return self._selected(agent, capability_id, "ASSIGNED_AGENT_ELIGIBLE")

        candidates = self.registry.find_by_capability(tenant_id, capability_id)
        if not candidates:
            return AgentSelectionResult(AgentSelectionStatus.NO_MATCH, capability_id, reason="NO_CAPABILITY_MATCH")
        eligible, blocked = [], []
        for agent in candidates:
            reason = self._evaluate(agent, capability_id, action_type, tool_id, max_cost)
            if reason:
                blocked.append(reason)
                continue
            availability_rank = 0 if agent.availability == AgentAvailability.AVAILABLE else 1
            cost_rank = agent.estimated_cost if agent.estimated_cost is not None else Decimal("Infinity")
            eligible.append((availability_rank, agent.priority, cost_rank, agent.agent_id, agent))
        if not eligible:
            return AgentSelectionResult(AgentSelectionStatus.BLOCKED, capability_id, reason=",".join(sorted(set(blocked))) or "NO_ELIGIBLE_AGENT")
        return self._selected(min(eligible)[-1], capability_id, "CAPABILITY_FIRST_DETERMINISTIC_SELECTION")

    @staticmethod
    def _selected(agent, capability_id, reason):
        return AgentSelectionResult(
            AgentSelectionStatus.SELECTED, capability_id, agent_id=agent.agent_id,
            reason=reason, estimated_cost=agent.estimated_cost,
            is_cost_unknown=agent.estimated_cost is None,
            metadata={"availability": agent.availability.value, "priority": agent.priority, "executor_key": agent.executor_key},
        )

    def execute(self, context: AgentExecutionContext, selected_agent_id: Optional[str] = None) -> AgentExecutionResult:
        scope = self._scope(context)
        fingerprint = self._fingerprint(context, selected_agent_id)
        with self._lock:
            prior = self._results.get(scope)
            if prior:
                if prior[0] != fingerprint:
                    return self._failure(context, selected_agent_id, AgentExecutionStatus.BLOCKED, AgentExecutionFailureType.INVALID_INPUT, "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD")
                return prior[1]

            selection = self.select_agent(
                context.tenant_id, context.capability_id, context.action_type,
                context.tool_id, context.budget_remaining, selected_agent_id,
            )
            if selection.status != AgentSelectionStatus.SELECTED:
                result = self._failure(context, selected_agent_id, AgentExecutionStatus.BLOCKED, self._selection_failure(selection.reason), selection.reason)
                self._emit(context, result, AuditRecordType.SPECIALIST_BLOCKED)
                return self._store(scope, fingerprint, result)

            agent = self.registry.get(context.tenant_id, selection.agent_id)
            capability = agent.get_capability(context.capability_id)
            self._emit(context, None, AuditRecordType.SPECIALIST_SELECTED, agent.agent_id)
            valid, errors = capability.contract.input_contract.validate(context.inputs)
            if not valid:
                result = self._failure(context, agent.agent_id, AgentExecutionStatus.INSUFFICIENT_DATA, AgentExecutionFailureType.INVALID_INPUT, "; ".join(errors))
                self._emit(context, result, AuditRecordType.SPECIALIST_BLOCKED)
                return self._store(scope, fingerprint, result)

            if self.pre_execution_guard is not None:
                denial = self.pre_execution_guard(context, agent)
                if denial:
                    failure = denial if isinstance(denial, AgentExecutionFailureType) else AgentExecutionFailureType(str(denial))
                    result = self._failure(context, agent.agent_id, AgentExecutionStatus.BLOCKED, failure, failure.value)
                    self._emit(context, result, AuditRecordType.SPECIALIST_BLOCKED)
                    return self._store(scope, fingerprint, result)

            params = dict(context.inputs)
            params.update({"action_type": context.action_type, "capability": context.capability_id,
                           "specialist_agent_id": agent.agent_id, "tenant_id": context.tenant_id,
                           "idempotency_key": context.idempotency_key, "correlation_id": context.correlation_id})
            if context.tool_id:
                params["tool_id"] = context.tool_id
            decision = LoopDecision(LoopAction.CONTINUE, "SPECIALIST_CAPABILITY_EXECUTION", context.tool_id or context.action_type, params)
            state = LoopState(mission_id=context.mission_id, iteration=1, goal=context.capability_id)
            executor = self.executor_bindings[agent.executor_key]
            self._emit(context, None, AuditRecordType.SPECIALIST_EXECUTION_STARTED, agent.agent_id)
            self._trace(context, agent.agent_id, TraceStatus.STARTED, StepType.START)
            try:
                raw = executor.execute(decision, state)
                result = self._map_result(context, agent.agent_id, capability.contract.output_contract, raw)
            except Exception as exc:
                result = self._failure(context, agent.agent_id, AgentExecutionStatus.FAILED, AgentExecutionFailureType.TECHNICAL_FAILURE, self._safe_reason(exc))
            final_event = AuditRecordType.SPECIALIST_EXECUTION_COMPLETED if result.status in (AgentExecutionStatus.SUCCESS, AgentExecutionStatus.PARTIAL) else AuditRecordType.SPECIALIST_EXECUTION_FAILED
            self._emit(context, result, final_event)
            trace_status = TraceStatus.SUCCESS if result.status in (AgentExecutionStatus.SUCCESS, AgentExecutionStatus.PARTIAL) else TraceStatus.FAILED
            self._trace(context, agent.agent_id, trace_status, StepType.COMPLETE if trace_status == TraceStatus.SUCCESS else StepType.FAILURE)
            if self.usage_hook is not None:
                self.usage_hook(context, result)
            return self._store(scope, fingerprint, result)

    def execute_plan_step(self, step: PlanStep, tenant_context: TenantContext, mission_id: str, idempotency_key: str, budget_remaining: Optional[Decimal] = None):
        context = AgentExecutionContext(
            tenant_context.tenant_id, mission_id, step.assigned_capability or step.action_type,
            step.action_type, step.required_inputs, idempotency_key,
            tenant_context.correlation_id or idempotency_key, step.metadata.get("tool_id"),
            budget_remaining, {"plan_step_id": step.step_id},
        )
        return self.execute(context, step.assigned_agent)

    def execute_mission(self, mission: Mission, tenant_context: TenantContext, idempotency_key: str, budget_remaining: Optional[Decimal] = None):
        declared_tenant = mission.parameters.get("tenant_id")
        capability = str(mission.parameters.get("required_capability") or mission.parameters.get("capability") or mission.type.value)
        if declared_tenant and declared_tenant != tenant_context.tenant_id:
            return AgentExecutionResult(AgentExecutionStatus.BLOCKED, None, capability, failure_type=AgentExecutionFailureType.POLICY_DENIED, failure_reason="TENANT_MISMATCH", idempotency_key=idempotency_key, correlation_id=tenant_context.correlation_id or idempotency_key)
        context = AgentExecutionContext(
            tenant_context.tenant_id, mission.mission_id, capability,
            str(mission.parameters.get("action_type") or mission.type.value), mission.parameters.get("inputs", {}),
            idempotency_key, tenant_context.correlation_id or idempotency_key, mission.parameters.get("tool_id"),
            budget_remaining, {"parent_mission_id": mission.parent_mission_id},
        )
        return self.execute(context, mission.parameters.get("assigned_agent"))

    def propagate_to_sub_mission(self, result: AgentExecutionResult, mission: Mission, tenant_context: TenantContext, sub_mission_service) -> bool:
        if result.status == AgentExecutionStatus.PARTIAL:
            return False
        status = MissionStatus.COMPLETED if result.status == AgentExecutionStatus.SUCCESS else MissionStatus.BLOCKED if result.status == AgentExecutionStatus.BLOCKED else MissionStatus.FAILED
        failure_map = {
            AgentExecutionFailureType.TECHNICAL_FAILURE: SubMissionFailureType.TECHNICAL_FAILURE,
            AgentExecutionFailureType.INVALID_INPUT: SubMissionFailureType.INVALID_INPUT,
            AgentExecutionFailureType.POLICY_DENIED: SubMissionFailureType.POLICY_DENIED,
            AgentExecutionFailureType.BUDGET_EXHAUSTED: SubMissionFailureType.BUDGET_EXHAUSTED,
        }
        contract = SubMissionResultContract(
            mission_id=mission.mission_id, parent_mission_id=mission.parent_mission_id,
            tenant_id=tenant_context.tenant_id, status=status, outputs=result.outputs,
            failure_type=failure_map.get(result.failure_type, SubMissionFailureType.UNKNOWN) if result.failure_type else None,
            failure_reason=result.failure_reason, cost_spent=result.cost_used, tokens_spent=result.tokens_used,
            is_required=mission.is_required, metadata={"specialist_agent_id": result.agent_id, "capability_id": result.capability_id},
        )
        return sub_mission_service.propagate_result(tenant_context, contract)

    def _map_result(self, context, agent_id, output_contract, raw):
        if not isinstance(raw, Mapping):
            return self._failure(context, agent_id, AgentExecutionStatus.FAILED, AgentExecutionFailureType.TECHNICAL_FAILURE, "INVALID_EXECUTOR_RESULT")
        status_text = str(raw.get("status", "UNKNOWN")).upper()
        if status_text.startswith(("AUTHORIZATION_", "POLICY_", "APPROVAL_", "TOOL_", "FINANCIAL_", "SENSITIVE_DATA_", "EMERGENCY_STOP_")):
            return self._failure(context, agent_id, AgentExecutionStatus.BLOCKED, AgentExecutionFailureType.POLICY_DENIED, status_text)
        if status_text in ("RATE_LIMITED", "RATE_LIMIT_EXCEEDED"):
            return self._failure(context, agent_id, AgentExecutionStatus.BLOCKED, AgentExecutionFailureType.RATE_LIMITED, status_text)
        if status_text == "BUDGET_EXHAUSTED":
            return self._failure(context, agent_id, AgentExecutionStatus.BLOCKED, AgentExecutionFailureType.BUDGET_EXHAUSTED, status_text)
        if status_text == "INSUFFICIENT_DATA":
            return self._failure(context, agent_id, AgentExecutionStatus.INSUFFICIENT_DATA, AgentExecutionFailureType.INVALID_INPUT, self._safe_reason(raw.get("error", status_text)))
        outputs = raw.get("outputs", raw.get("output", {}))
        if not isinstance(outputs, Mapping):
            return self._failure(context, agent_id, AgentExecutionStatus.FAILED, AgentExecutionFailureType.TECHNICAL_FAILURE, "INVALID_OUTPUT_CONTRACT")
        valid, errors = output_contract.validate(outputs)
        if status_text in ("SUCCESS", "PARTIAL") and not valid:
            return self._failure(context, agent_id, AgentExecutionStatus.FAILED, AgentExecutionFailureType.TECHNICAL_FAILURE, "; ".join(errors))
        if status_text not in ("SUCCESS", "PARTIAL"):
            return self._failure(context, agent_id, AgentExecutionStatus.FAILED, AgentExecutionFailureType.TECHNICAL_FAILURE, self._safe_reason(raw.get("error") or raw.get("reason") or status_text))
        return AgentExecutionResult(
            AgentExecutionStatus(status_text), agent_id, context.capability_id, outputs=outputs,
            cost_used=self._decimal_or_none(raw.get("cost_used")), tokens_used=raw.get("tokens_used") if isinstance(raw.get("tokens_used"), int) else None,
            idempotency_key=context.idempotency_key, correlation_id=context.correlation_id,
            metadata={"tenant_id": context.tenant_id, "mission_id": context.mission_id, "usage_attributed_to": agent_id},
        )

    @staticmethod
    def _selection_failure(reason):
        if reason in ("ACTION_NOT_ALLOWED", "TOOL_NOT_ALLOWED", "POLICY_DENIED", "AGENT_TENANT_MISMATCH"):
            return AgentExecutionFailureType.POLICY_DENIED
        if "BUDGET" in reason or reason == "COST_UNKNOWN":
            return AgentExecutionFailureType.BUDGET_EXHAUSTED
        return AgentExecutionFailureType.CAPABILITY_UNAVAILABLE

    def _failure(self, context, agent_id, status, failure_type, reason):
        return AgentExecutionResult(status, agent_id, context.capability_id, failure_type=failure_type, failure_reason=self._safe_reason(reason), idempotency_key=context.idempotency_key, correlation_id=context.correlation_id, metadata={"tenant_id": context.tenant_id, "mission_id": context.mission_id})

    @staticmethod
    def _safe_reason(reason):
        text = str(reason or "UNKNOWN")[:500]
        return re.sub(r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)

    @staticmethod
    def _scope(context):
        return (context.tenant_id, context.mission_id, context.capability_id, context.action_type, context.tool_id or "", context.idempotency_key)

    @staticmethod
    def _fingerprint(context, selected_agent_id):
        payload = sanitize_security_data({"inputs": dict(context.inputs), "budget": str(context.budget_remaining), "agent": selected_agent_id, "metadata": dict(context.metadata)})
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def _store(self, scope, fingerprint, result):
        self._results[scope] = (fingerprint, result)
        return result

    def _emit(self, context, result, record_type, agent_id=None):
        if self.audit_repository is None:
            return
        agent_id = agent_id or (result.agent_id if result else None)
        sequence = record_type.value
        self.audit_repository.append(AuditRecord(
            audit_id=f"aud-r3-{hashlib.sha256((':'.join(map(str, self._scope(context))) + sequence).encode()).hexdigest()[:24]}",
            record_type=record_type, occurred_at=datetime.now(timezone.utc),
            actor=AuditActor(AuditActorType.AGENT, agent_id or "specialist-selector"),
            subject_type="SPECIALIST_AGENT", subject_id=agent_id or context.capability_id,
            action_or_operation=context.action_type, status=result.status.value if result else sequence,
            correlation_id=context.correlation_id, mission_id=context.mission_id,
            idempotency_key=f"r3:{':'.join(map(str, self._scope(context)))}:{sequence}",
            metadata=sanitize_security_data({"tenant_id": context.tenant_id, "capability": context.capability_id,
                "failure_type": result.failure_type.value if result and result.failure_type else None,
                "failure_reason": result.failure_reason if result else None,
                "cost_used": str(result.cost_used) if result and result.cost_used is not None else None,
                "tokens_used": result.tokens_used if result else None}),
        ))

    def _trace(self, context, agent_id, status, step_type):
        if self.trace_service is not None:
            self.trace_service.record_step(component_name="SpecialistAgentService", execution_id=context.idempotency_key,
                step_number=0 if step_type == StepType.START else 1, step_type=step_type, operation=context.action_type,
                status=status, tool_or_service=context.tool_id or agent_id, correlation_id=context.correlation_id,
                mission_id=context.mission_id, metadata=sanitize_security_data({"tenant_id": context.tenant_id, "capability": context.capability_id, "agent_id": agent_id}))

    @staticmethod
    def _decimal_or_none(value):
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
