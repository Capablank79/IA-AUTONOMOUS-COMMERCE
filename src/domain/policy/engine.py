import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Sequence, Optional, Dict, Any, Tuple
from types import MappingProxyType

from .models import (
    PolicyDecisionType,
    PolicyRuleCategory,
    PolicySeverity,
    PolicyViolation,
    RuleEvaluationResult,
    PolicyEvaluationContext,
    PolicyEvaluation,
)
from .ports import PolicyRule, PolicyEnginePort, PolicyAuditRepository
from .rules import (
    AuthorizationPolicyRule,
    HumanApprovalPolicyRule,
    IdempotencyPolicyRule,
    BudgetAndCapitalPolicyRule,
    RiskPolicyRule,
    DataQualityAndSafetyRule,
    PriceFloorPolicyRule,
    MarginProtectionPolicyRule,
    MaxPriceChangePolicyRule,
    OversellingProtectionPolicyRule,
    InventorySafetyBufferPolicyRule,
)


class PolicyEngine(PolicyEnginePort):
    """
    Motor determinista, auditable y desacoplado de gobernanza y políticas (Hito E.3).
    
    Barrera de control entre DECISION y ACTION:
    - Evalúa un conjunto de PolicyRules ordenadas jerárquicamente por prioridad.
    - Aplica la jerarquía de resolución de decisiones:
      DENY > REQUIRE_APPROVAL > DEFER > UNKNOWN > ALLOW
    - Preserva UNKNOWN explícito (sin convertir UNKNOWN en ALLOW ni inventar datos).
    - Preserva y propaga correlation_id, idempotency_key, audit trail y procedencia.
    - No depende de HTTP, SDKs, MercadoLibre ni credenciales.
    """

    def __init__(
        self,
        rules: Optional[Sequence[PolicyRule]] = None,
        audit_repository: Optional[PolicyAuditRepository] = None,
    ):
        if rules is not None:
            self._rules = tuple(rules)
        else:
            # Conjunto de reglas estándar por defecto ordenadas por jerarquía
            self._rules = (
                AuthorizationPolicyRule(),
                IdempotencyPolicyRule(),
                PriceFloorPolicyRule(),
                MarginProtectionPolicyRule(),
                MaxPriceChangePolicyRule(),
                OversellingProtectionPolicyRule(),
                InventorySafetyBufferPolicyRule(),
                BudgetAndCapitalPolicyRule(),
                RiskPolicyRule(),
                HumanApprovalPolicyRule(),
                DataQualityAndSafetyRule(),
            )
        self._audit_repository = audit_repository

    @property
    def rules(self) -> Tuple[PolicyRule, ...]:
        return self._rules

    def evaluate(self, context: PolicyEvaluationContext) -> PolicyEvaluation:
        """
        Evalúa todas las reglas de política contra el contexto de ejecución.
        """
        evaluation_id = f"PEVAL-{uuid.uuid4().hex[:12]}"
        rule_results: List[RuleEvaluationResult] = []
        rules_evaluated: List[str] = []
        all_reasons: List[str] = []
        all_violations: List[PolicyViolation] = []
        evidence_unknowns: List[str] = []

        # Evaluar cada regla
        for rule in self._rules:
            rules_evaluated.append(rule.name)
            res = rule.evaluate(context)
            rule_results.append(res)
            all_reasons.extend(res.reasons)
            all_violations.extend(res.violations)

            # Recolectar unknowns si aplican
            for v in res.violations:
                if v.severity == PolicySeverity.UNCERTAIN:
                    evidence_unknowns.append(f"{v.rule_name}: {v.code} - {v.message}")

        # Jerarquía de resolución de decisión:
        # 1. Si alguna regla determina DENY -> DENY
        # 2. Si alguna regla determina REQUIRE_APPROVAL -> REQUIRE_APPROVAL
        # 3. Si alguna regla determina DEFER -> DEFER
        # 4. Si alguna regla determina UNKNOWN -> UNKNOWN
        # 5. Si todas son ALLOW -> ALLOW
        has_deny = any(r.decision_impact == PolicyDecisionType.DENY for r in rule_results)
        has_require_approval = any(r.decision_impact == PolicyDecisionType.REQUIRE_APPROVAL for r in rule_results)
        has_defer = any(r.decision_impact == PolicyDecisionType.DEFER for r in rule_results)
        has_unknown = any(r.decision_impact == PolicyDecisionType.UNKNOWN for r in rule_results)

        if has_deny:
            final_decision = PolicyDecisionType.DENY
        elif has_require_approval:
            final_decision = PolicyDecisionType.REQUIRE_APPROVAL
        elif has_defer:
            final_decision = PolicyDecisionType.DEFER
        elif has_unknown:
            final_decision = PolicyDecisionType.UNKNOWN
        else:
            final_decision = PolicyDecisionType.ALLOW

        is_allowed = (final_decision == PolicyDecisionType.ALLOW)
        requires_approval = (final_decision == PolicyDecisionType.REQUIRE_APPROVAL)
        is_unknown = (final_decision == PolicyDecisionType.UNKNOWN)
        is_denied = (final_decision == PolicyDecisionType.DENY)
        is_deferred = (final_decision == PolicyDecisionType.DEFER)

        evaluation = PolicyEvaluation(
            evaluation_id=evaluation_id,
            decision=final_decision,
            action_type=context.action_type,
            actor_id=context.actor_id,
            mission_id=context.mission_id,
            correlation_id=context.correlation_id,
            rules_evaluated=tuple(rules_evaluated),
            rule_results=tuple(rule_results),
            reasons=tuple(all_reasons),
            violations=tuple(all_violations),
            is_allowed=is_allowed,
            requires_approval=requires_approval,
            is_unknown=is_unknown,
            is_denied=is_denied,
            is_deferred=is_deferred,
            budget_impact=context.requested_budget,
            risk_level=context.risk_level,
            idempotency_key=context.idempotency_key,
            evidence_unknowns=tuple(evidence_unknowns),
            timestamp=datetime.now(timezone.utc),
            metadata=MappingProxyType({
                "action_type": context.action_type,
                "is_external_impact": context.is_external_impact,
                "is_irreversible": context.is_irreversible,
            })
        )

        if self._audit_repository is not None:
            try:
                self._audit_repository.save_evaluation(evaluation)
            except Exception:
                pass

        return evaluation
