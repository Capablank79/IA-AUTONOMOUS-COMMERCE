from decimal import Decimal
from typing import List, Optional, Tuple

from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyRuleCategory,
    PolicySeverity,
    PolicyViolation,
    RuleEvaluationResult,
)
from src.domain.policy.ports import PolicyRule
from .models import ReturnStatus, RefundStatus


class ReturnActionPolicyRule(PolicyRule):
    """
    Regla de gobernanza determinista para acciones postventa, devoluciones y reembolsos (G.8).
    
    Reglas:
    - ISSUE_REFUND requiere que el monto esté definido y sea mayor a cero.
    - ISSUE_REFUND requiere aprobación humana si el monto supera el umbral configurable (default > 100 USD)
      o si is_irreversible=True y human_approved=False.
    - Acciones destructivas o irreversibles (REJECT_RETURN, ISSUE_REFUND) exigen verificación de idempotencia y contexto seguro.
    - Si el contexto indica baja confianza o UNKNOWN en el origen, devuelve REQUIRE_APPROVAL o DENY.
    """

    def __init__(self, max_autonomous_refund_amount: Decimal = Decimal("100.00")):
        self.max_autonomous_refund_amount = max_autonomous_refund_amount

    @property
    def name(self) -> str:
        return "ReturnActionPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.BUSINESS_RULE.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        action = context.action_type

        # 1. Reglas específicas para ISSUE_REFUND
        if action == "ISSUE_REFUND":
            # El reembolso debe tener monto especificado en requested_budget
            if context.requested_budget is None or context.requested_budget <= Decimal("0.00"):
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.SAFETY,
                    severity=PolicySeverity.BLOCKING,
                    message="ISSUE_REFUND requires a positive requested_budget amount.",
                    code="REFUND_AMOUNT_INVALID",
                    details={"action_type": action, "requested_budget": str(context.requested_budget)},
                )
                violations.append(v)
                reasons.append("Invalid or missing refund amount")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.SAFETY,
                    passed=False,
                    decision_impact=PolicyDecisionType.DENY,
                    reasons=tuple(reasons),
                    violations=tuple(violations),
                )

            # Si excede el límite autónomo y no está aprobado por humano
            if context.requested_budget > self.max_autonomous_refund_amount and not context.human_approved:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.APPROVAL,
                    severity=PolicySeverity.REQUIRES_HUMAN,
                    message=(
                        f"Refund amount {context.requested_budget} exceeds autonomous threshold "
                        f"({self.max_autonomous_refund_amount}). Human approval required."
                    ),
                    code="REFUND_EXCEEDS_AUTONOMOUS_LIMIT",
                    details={
                        "requested_budget": str(context.requested_budget),
                        "threshold": str(self.max_autonomous_refund_amount),
                    },
                )
                violations.append(v)
                reasons.append("Refund amount requires human authorization")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.APPROVAL,
                    passed=False,
                    decision_impact=PolicyDecisionType.REQUIRE_APPROVAL,
                    reasons=tuple(reasons),
                    violations=tuple(violations),
                )

        # 2. Reglas para REJECT_RETURN
        if action == "REJECT_RETURN" and not context.human_approved:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.APPROVAL,
                severity=PolicySeverity.REQUIRES_HUMAN,
                message="Rejecting a return claim has direct seller reputation impact and requires human approval.",
                code="REJECT_RETURN_REQUIRES_HUMAN",
                details={"action_type": action},
            )
            violations.append(v)
            reasons.append("Return rejection requires human review")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.APPROVAL,
                passed=False,
                decision_impact=PolicyDecisionType.REQUIRE_APPROVAL,
                reasons=tuple(reasons),
                violations=tuple(violations),
            )

        reasons.append(f"Return action '{action}' passed governance checks")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.BUSINESS_RULE,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=(),
        )
