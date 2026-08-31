from datetime import datetime, timezone
from typing import Dict, Any, Optional, Sequence, Mapping
from types import MappingProxyType

from src.domain.mission.models import LoopDecision, LoopState
from src.domain.mission.ports import ActionExecutor
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyEvaluation,
)
from src.domain.policy.ports import PolicyEnginePort, PolicyAuditRepository
from src.domain.capital.models import CapitalBudget, AllocationDecision
from src.domain.supplier_intelligence.models import RiskLevel, EvidenceProvenanceType
from src.domain.market_intelligence.models import Confidence
from decimal import Decimal
from .policy_enforcement_service import PolicyEnforcementService


class PolicyGuardedActionExecutor(ActionExecutor):
    """
    Ejecutor de acciones decorador / guardián que impone la barrera de gobernanza (Hito E.3).
    
    Flujo de control:
    DECISION
    ↓
    POLICY ENGINE (PolicyEnforcementService)
    ↓
    [ALLOW] -> DELEGATE ACTION EXECUTOR -> PORT -> ADAPTER -> EXTERNAL SYSTEM
    [DENY] -> NO EXTERNAL EXECUTION (Retorna observación de bloqueo)
    [REQUIRE_APPROVAL] -> NO EXTERNAL EXECUTION (Retorna observación requiriendo aprobación)
    [DEFER] -> NO EXTERNAL EXECUTION (Retorna observación de diferimiento)
    [UNKNOWN] -> NO EXTERNAL EXECUTION (Retorna observación de incertidumbre sin inventar ALLOW)
    """

    def __init__(
        self,
        delegate_executor: ActionExecutor,
        policy_service: Optional[PolicyEnforcementService] = None,
        policy_engine: Optional[PolicyEnginePort] = None,
        audit_repository: Optional[PolicyAuditRepository] = None,
        actor_id: str = "autonomous_agent",
        default_allowed_actions: Sequence[str] = (),
        default_prohibited_actions: Sequence[str] = (),
        default_actions_requiring_approval: Sequence[str] = (),
        capital_budget: Optional[CapitalBudget] = None,
    ):
        if delegate_executor is None:
            raise ValueError("delegate_executor cannot be None")
        self.delegate_executor = delegate_executor
        self.policy_service = policy_service or PolicyEnforcementService(
            policy_engine=policy_engine,
            audit_repository=audit_repository,
        )
        self.actor_id = actor_id
        self.default_allowed_actions = tuple(default_allowed_actions)
        self.default_prohibited_actions = tuple(default_prohibited_actions)
        self.default_actions_requiring_approval = tuple(default_actions_requiring_approval)
        self.capital_budget = capital_budget

        self._latest_evaluation: Optional[PolicyEvaluation] = None
        self._executed_idempotency_keys: list[str] = []
        self._in_flight_idempotency_keys: list[str] = []

    @property
    def latest_evaluation(self) -> Optional[PolicyEvaluation]:
        return self._latest_evaluation

    @property
    def external_calls_count(self) -> int:
        if hasattr(self.delegate_executor, "external_calls_count"):
            return getattr(self.delegate_executor, "external_calls_count")
        return 0

    def execute(self, decision: LoopDecision, state: LoopState) -> Dict[str, Any]:
        """
        Evalúa las políticas de gobernanza antes de delegar la acción al ejecutor real.
        """
        # 1. Construir contexto para la decisión actual
        context = self.policy_service.build_context(
            decision=decision,
            state=state,
            actor_id=self.actor_id,
            capital_budget=self.capital_budget,
            executed_idempotency_keys=tuple(self._executed_idempotency_keys),
            in_flight_idempotency_keys=tuple(self._in_flight_idempotency_keys),
            allowed_actions=self.default_allowed_actions,
            prohibited_actions=self.default_prohibited_actions,
            actions_requiring_approval=self.default_actions_requiring_approval,
        )

        # 2. Evaluar políticas de forma determinista
        evaluation = self.policy_service.evaluate_decision(context)
        self._latest_evaluation = evaluation

        # 3. Gobernanza estricta: Bloquear si no es ALLOW
        if evaluation.decision == PolicyDecisionType.DENY:
            return {
                "status": "POLICY_DENIED",
                "action_executed": context.action_type,
                "is_allowed": False,
                "decision": evaluation.decision.value,
                "reasons": list(evaluation.reasons),
                "violations": [
                    {
                        "rule_name": v.rule_name,
                        "category": v.category.value,
                        "severity": v.severity.value,
                        "code": v.code,
                        "message": v.message,
                        "details": dict(v.details),
                    }
                    for v in evaluation.violations
                ],
                "evaluation_id": evaluation.evaluation_id,
                "correlation_id": evaluation.correlation_id,
            }

        if evaluation.decision == PolicyDecisionType.REQUIRE_APPROVAL:
            return {
                "status": "POLICY_APPROVAL_REQUIRED",
                "action_executed": context.action_type,
                "is_allowed": False,
                "requires_approval": True,
                "decision": evaluation.decision.value,
                "reasons": list(evaluation.reasons),
                "violations": [
                    {
                        "rule_name": v.rule_name,
                        "category": v.category.value,
                        "severity": v.severity.value,
                        "code": v.code,
                        "message": v.message,
                        "details": dict(v.details),
                    }
                    for v in evaluation.violations
                ],
                "evaluation_id": evaluation.evaluation_id,
                "correlation_id": evaluation.correlation_id,
                "instruction": "Action is blocked until explicit human approval is received.",
            }

        if evaluation.decision == PolicyDecisionType.DEFER:
            return {
                "status": "POLICY_DEFERRED",
                "action_executed": context.action_type,
                "is_allowed": False,
                "is_deferred": True,
                "decision": evaluation.decision.value,
                "reasons": list(evaluation.reasons),
                "violations": [
                    {
                        "rule_name": v.rule_name,
                        "category": v.category.value,
                        "code": v.code,
                        "message": v.message,
                    }
                    for v in evaluation.violations
                ],
                "evaluation_id": evaluation.evaluation_id,
                "correlation_id": evaluation.correlation_id,
            }

        if evaluation.decision == PolicyDecisionType.UNKNOWN:
            return {
                "status": "POLICY_UNKNOWN",
                "action_executed": context.action_type,
                "is_allowed": False,
                "is_unknown": True,
                "decision": evaluation.decision.value,
                "reasons": list(evaluation.reasons),
                "evidence_unknowns": list(evaluation.evidence_unknowns),
                "violations": [
                    {
                        "rule_name": v.rule_name,
                        "category": v.category.value,
                        "code": v.code,
                        "message": v.message,
                    }
                    for v in evaluation.violations
                ],
                "evaluation_id": evaluation.evaluation_id,
                "correlation_id": evaluation.correlation_id,
                "instruction": "Execution halted due to insufficient evidence or uncertain risk/budget.",
            }

        # 4. Caso ALLOW: Ejecutar con seguridad
        if context.idempotency_key:
            self._in_flight_idempotency_keys.append(context.idempotency_key)

        try:
            result = self.delegate_executor.execute(decision, state)
            if context.idempotency_key:
                self._executed_idempotency_keys.append(context.idempotency_key)
            
            # Enriquecer observación con metadatos de gobernanza
            if isinstance(result, dict):
                enriched = dict(result)
                enriched["policy_evaluation_id"] = evaluation.evaluation_id
                enriched["policy_decision"] = evaluation.decision.value
                return enriched
            return {
                "result": result,
                "policy_evaluation_id": evaluation.evaluation_id,
                "policy_decision": evaluation.decision.value,
            }
        finally:
            if context.idempotency_key and context.idempotency_key in self._in_flight_idempotency_keys:
                self._in_flight_idempotency_keys.remove(context.idempotency_key)
