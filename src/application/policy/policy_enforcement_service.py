from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, Sequence, Tuple, Mapping
from types import MappingProxyType
import uuid

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, RiskLevel
from src.domain.capital.models import CapitalBudget, AllocationDecision
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyEvaluation,
)
from src.domain.policy.ports import PolicyEnginePort, PolicyAuditRepository
from src.domain.policy.engine import PolicyEngine


class PolicyEnforcementService:
    """
    Servicio de aplicación de gobernanza que construye el contexto y evalúa las políticas (Hito E.3).
    Actúa como barrera estricta entre la toma de decisiones y la ejecución de acciones.
    """

    def __init__(
        self,
        policy_engine: Optional[PolicyEnginePort] = None,
        audit_repository: Optional[PolicyAuditRepository] = None,
    ):
        self.policy_engine = policy_engine or PolicyEngine(audit_repository=audit_repository)
        self.audit_repository = audit_repository

    def build_context(
        self,
        decision: LoopDecision,
        state: LoopState,
        actor_id: Optional[str] = None,
        channel: Optional[str] = None,
        target_resource: Optional[str] = None,
        requested_budget: Optional[Decimal] = None,
        capital_budget: Optional[CapitalBudget] = None,
        capital_allocation_decision: Optional[AllocationDecision] = None,
        risk_level: Optional[RiskLevel] = None,
        confidence: Optional[Confidence] = None,
        provenance: Optional[EvidenceProvenanceType] = None,
        is_external_impact: bool = False,
        is_irreversible: bool = False,
        human_approved: bool = False,
        executed_idempotency_keys: Sequence[str] = (),
        in_flight_idempotency_keys: Sequence[str] = (),
        allowed_actions: Sequence[str] = (),
        prohibited_actions: Sequence[str] = (),
        actions_requiring_approval: Sequence[str] = (),
        custom_context: Optional[Mapping[str, Any]] = None,
    ) -> PolicyEvaluationContext:
        """
        Construye un PolicyEvaluationContext desacoplado, extrayendo y normalizando parámetros de la decisión y del estado.
        """
        params = dict(decision.parameters) if decision.parameters else {}
        action_type = params.get("action_type") or str(decision.action.value)
        idempotency_key = params.get("idempotency_key")
        request_id = params.get("request_id")
        corr_id = params.get("correlation_id") or state.mission_id or str(uuid.uuid4())

        # Auto-detectar propiedades si vienen en params
        if requested_budget is None and "requested_budget" in params:
            try:
                requested_budget = Decimal(str(params["requested_budget"]))
            except Exception:
                pass

        if is_external_impact is False:
            is_external_impact = bool(params.get("is_external_impact", False))
            if action_type in ("PUBLISH", "PUBLISH_LISTING", "ORDER", "PAY", "COMMIT_CAPITAL"):
                is_external_impact = True

        if is_irreversible is False:
            is_irreversible = bool(params.get("is_irreversible", False))
            if action_type in ("PUBLISH", "PUBLISH_LISTING", "PAY"):
                is_irreversible = True

        if human_approved is False:
            human_approved = bool(params.get("human_approved", False))

        act_id = actor_id or params.get("actor_id") or "autonomous_agent"
        chan = channel or params.get("channel")

        # Provenance
        prov = provenance
        if prov is None and "provenance" in params:
            p_val = params["provenance"]
            if isinstance(p_val, EvidenceProvenanceType):
                prov = p_val
            elif isinstance(p_val, str):
                try:
                    prov = EvidenceProvenanceType(p_val)
                except Exception:
                    pass

        # Risk level
        r_level = risk_level
        if r_level is None and "risk_level" in params:
            r_val = params["risk_level"]
            if isinstance(r_val, RiskLevel):
                r_level = r_val
            elif isinstance(r_val, str):
                try:
                    r_level = RiskLevel(r_val)
                except Exception:
                    pass

        # Confidence
        conf = confidence
        if conf is None and "confidence" in params:
            c_val = params["confidence"]
            if isinstance(c_val, Confidence):
                conf = c_val
            elif isinstance(c_val, str):
                try:
                    conf = Confidence(c_val)
                except Exception:
                    pass

        # Target resource
        t_res = target_resource or params.get("target_resource") or decision.target

        # Prohibited / Allowed actions from params if provided
        p_actions = list(prohibited_actions)
        if "prohibited_actions" in params and isinstance(params["prohibited_actions"], (list, tuple)):
            p_actions.extend(params["prohibited_actions"])

        a_actions = list(allowed_actions)
        if "allowed_actions" in params and isinstance(params["allowed_actions"], (list, tuple)):
            a_actions.extend(params["allowed_actions"])

        req_appr_actions = list(actions_requiring_approval)
        if "actions_requiring_approval" in params and isinstance(params["actions_requiring_approval"], (list, tuple)):
            req_appr_actions.extend(params["actions_requiring_approval"])

        ctx_custom = dict(custom_context or {})
        if "custom_context" in params and isinstance(params["custom_context"], dict):
            ctx_custom.update(params["custom_context"])

        return PolicyEvaluationContext(
            action_type=action_type,
            actor_id=act_id,
            mission_id=state.mission_id,
            correlation_id=corr_id,
            loop_decision=decision,
            loop_state=state,
            idempotency_key=idempotency_key,
            request_id=request_id,
            target_resource=t_res,
            channel=str(chan) if chan is not None else None,
            requested_budget=requested_budget,
            capital_budget=capital_budget,
            capital_allocation_decision=capital_allocation_decision,
            risk_level=r_level,
            confidence=conf,
            provenance=prov,
            is_external_impact=is_external_impact,
            is_irreversible=is_irreversible,
            human_approved=human_approved,
            executed_idempotency_keys=tuple(executed_idempotency_keys),
            in_flight_idempotency_keys=tuple(in_flight_idempotency_keys),
            allowed_actions=tuple(a_actions),
            prohibited_actions=tuple(p_actions),
            actions_requiring_approval=tuple(req_appr_actions),
            custom_context=MappingProxyType(ctx_custom),
            timestamp=datetime.now(timezone.utc),
        )

    def evaluate_decision(
        self,
        context: PolicyEvaluationContext,
    ) -> PolicyEvaluation:
        """
        Evalúa el contexto mediante el PolicyEngine configurado.
        """
        return self.policy_engine.evaluate(context)
