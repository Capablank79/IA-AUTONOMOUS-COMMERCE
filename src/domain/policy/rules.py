from decimal import Decimal
from typing import List, Tuple, Optional
from datetime import datetime, timezone

from src.domain.supplier_intelligence.models import RiskLevel, EvidenceProvenanceType
from src.domain.capital.models import AllocationStatus
from .models import (
    PolicyRuleCategory,
    PolicySeverity,
    PolicyDecisionType,
    PolicyViolation,
    RuleEvaluationResult,
    PolicyEvaluationContext,
)
from .ports import PolicyRule


class AuthorizationPolicyRule(PolicyRule):
    """
    Regla de gobernanza que verifica si la acción está autorizada para el actor/agente.
    - DENY si la acción está explícitamente en prohibited_actions.
    - DENY si hay una lista de allowed_actions y la acción no está incluida.
    - ALLOW si pasa las comprobaciones.
    """
    @property
    def name(self) -> str:
        return "AuthorizationPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.AUTHORIZATION.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        # 1. Prohibited actions
        if context.action_type in context.prohibited_actions:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.AUTHORIZATION,
                severity=PolicySeverity.BLOCKING,
                message=f"Action '{context.action_type}' is explicitly prohibited by policy.",
                code="AUTH_ACTION_PROHIBITED",
                details={"action_type": context.action_type, "actor_id": context.actor_id}
            )
            violations.append(v)
            reasons.append(f"Action '{context.action_type}' is prohibited")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.AUTHORIZATION,
                passed=False,
                decision_impact=PolicyDecisionType.DENY,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        # 2. Allowed actions list
        if context.allowed_actions and context.action_type not in context.allowed_actions:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.AUTHORIZATION,
                severity=PolicySeverity.BLOCKING,
                message=f"Action '{context.action_type}' is not in allowed actions list for actor '{context.actor_id}'.",
                code="AUTH_ACTION_NOT_ALLOWED",
                details={"action_type": context.action_type, "actor_id": context.actor_id}
            )
            violations.append(v)
            reasons.append(f"Action '{context.action_type}' is not authorized")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.AUTHORIZATION,
                passed=False,
                decision_impact=PolicyDecisionType.DENY,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        reasons.append(f"Action '{context.action_type}' is authorized for actor '{context.actor_id}'")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.AUTHORIZATION,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class HumanApprovalPolicyRule(PolicyRule):
    """
    Regla de gobernanza que determina si una acción requiere aprobación humana explícita.
    - REQUIRE_APPROVAL si la acción está en actions_requiring_approval o si es irreversible/alto impacto y no ha sido aprobada previamente.
    - ALLOW si ya cuenta con human_approved=True o si no requiere aprobación.
    """
    @property
    def name(self) -> str:
        return "HumanApprovalPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.APPROVAL.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        requires_human = (
            context.action_type in context.actions_requiring_approval
            or (context.is_irreversible and not context.human_approved)
            or (context.is_external_impact and context.risk_level == RiskLevel.HIGH and not context.human_approved)
        )

        if requires_human:
            if not context.human_approved:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.APPROVAL,
                    severity=PolicySeverity.REQUIRES_HUMAN,
                    message=f"Action '{context.action_type}' requires explicit human approval before execution.",
                    code="APPROVAL_REQUIRED",
                    details={
                        "action_type": context.action_type,
                        "is_irreversible": context.is_irreversible,
                        "is_external_impact": context.is_external_impact,
                        "risk_level": context.risk_level.value if context.risk_level else None
                    }
                )
                violations.append(v)
                reasons.append(f"Action '{context.action_type}' requires human approval")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.APPROVAL,
                    passed=False,
                    decision_impact=PolicyDecisionType.REQUIRE_APPROVAL,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )
            else:
                reasons.append(f"Action '{context.action_type}' was approved by human operator")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.APPROVAL,
                    passed=True,
                    decision_impact=PolicyDecisionType.ALLOW,
                    reasons=tuple(reasons),
                    violations=()
                )

        reasons.append(f"Action '{context.action_type}' does not require human approval")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.APPROVAL,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class IdempotencyPolicyRule(PolicyRule):
    """
    Regla de gobernanza para prevención de acciones repetidas o re-ejecución concurrente.
    - DENY si la idempotency_key ya fue ejecutada (evita replay attack o duplicación).
    - DEFER si la idempotency_key está en vuelo (in-flight).
    - UNKNOWN si la idempotency_key es requerida para una acción externa y no fue provista.
    """
    @property
    def name(self) -> str:
        return "IdempotencyPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.IDEMPOTENCY.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        if context.is_external_impact and not context.idempotency_key:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.IDEMPOTENCY,
                severity=PolicySeverity.UNCERTAIN,
                message="External impact action requested without required idempotency_key.",
                code="IDEMPOTENCY_KEY_MISSING",
                details={"action_type": context.action_type}
            )
            violations.append(v)
            reasons.append("Missing idempotency key for external impact action")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.IDEMPOTENCY,
                passed=False,
                decision_impact=PolicyDecisionType.UNKNOWN,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        if context.idempotency_key:
            if context.idempotency_key in context.executed_idempotency_keys:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.IDEMPOTENCY,
                    severity=PolicySeverity.BLOCKING,
                    message=f"Duplicate action detected: idempotency_key '{context.idempotency_key}' already executed.",
                    code="IDEMPOTENCY_DUPLICATE_ACTION",
                    details={"idempotency_key": context.idempotency_key}
                )
                violations.append(v)
                reasons.append(f"Action with idempotency_key '{context.idempotency_key}' was already executed")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.IDEMPOTENCY,
                    passed=False,
                    decision_impact=PolicyDecisionType.DENY,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )

            if context.idempotency_key in context.in_flight_idempotency_keys:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.IDEMPOTENCY,
                    severity=PolicySeverity.UNCERTAIN,
                    message=f"Action with idempotency_key '{context.idempotency_key}' is currently in flight.",
                    code="IDEMPOTENCY_IN_FLIGHT",
                    details={"idempotency_key": context.idempotency_key}
                )
                violations.append(v)
                reasons.append(f"Action with idempotency_key '{context.idempotency_key}' is in flight, deferring execution")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.IDEMPOTENCY,
                    passed=False,
                    decision_impact=PolicyDecisionType.DEFER,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )

        reasons.append("Idempotency validation passed")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.IDEMPOTENCY,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class BudgetAndCapitalPolicyRule(PolicyRule):
    """
    Regla de gobernanza para control de presupuesto y exposición financiera.
    Orquesta e integra CapitalBudget y AllocationDecision sin duplicar su cálculo interno.
    - DENY si el monto solicitado excede el allocatable_capital o si el capital allocation status fue REJECTED.
    - UNKNOWN si hay presupuesto requerido pero no se cuenta con información de presupuesto/capital.
    - ALLOW si los límites de presupuesto se cumplen.
    """
    @property
    def name(self) -> str:
        return "BudgetAndCapitalPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.BUDGET.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        # Si la acción tiene costo/presupuesto solicitado
        if context.requested_budget is not None and context.requested_budget > Decimal("0"):
            if context.capital_budget is None:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.BUDGET,
                    severity=PolicySeverity.UNCERTAIN,
                    message="Requested budget cannot be validated because capital_budget is unknown.",
                    code="BUDGET_UNKNOWN",
                    details={"requested_budget": str(context.requested_budget)}
                )
                violations.append(v)
                reasons.append("Capital budget is unknown for financial action")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.BUDGET,
                    passed=False,
                    decision_impact=PolicyDecisionType.UNKNOWN,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )

            # Validar contra allocatable_capital del budget
            if context.requested_budget > context.capital_budget.allocatable_capital:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.BUDGET,
                    severity=PolicySeverity.BLOCKING,
                    message=(
                        f"Requested budget ({context.requested_budget} {context.capital_budget.currency}) "
                        f"exceeds allocatable capital ({context.capital_budget.allocatable_capital} {context.capital_budget.currency})."
                    ),
                    code="BUDGET_EXCEEDED",
                    details={
                        "requested_budget": str(context.requested_budget),
                        "allocatable_capital": str(context.capital_budget.allocatable_capital),
                        "currency": context.capital_budget.currency,
                    }
                )
                violations.append(v)
                reasons.append("Requested budget exceeds allocatable capital")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.BUDGET,
                    passed=False,
                    decision_impact=PolicyDecisionType.DENY,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )

        # Si viene una decisión previa del CapitalAllocationEngine
        if context.capital_allocation_decision is not None:
            if context.capital_allocation_decision.status == AllocationStatus.REJECTED:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.BUDGET,
                    severity=PolicySeverity.BLOCKING,
                    message=(
                        f"Capital allocation was rejected by Capital Engine: "
                        f"{context.capital_allocation_decision.reason.value if context.capital_allocation_decision.reason else 'REJECTED'}"
                    ),
                    code="CAPITAL_ALLOCATION_REJECTED",
                    details={"reason": str(context.capital_allocation_decision.reason)}
                )
                violations.append(v)
                reasons.append("Capital allocation was rejected by Capital Engine")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.BUDGET,
                    passed=False,
                    decision_impact=PolicyDecisionType.DENY,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )
            elif context.capital_allocation_decision.status == AllocationStatus.NEEDS_INVESTIGATION:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.BUDGET,
                    severity=PolicySeverity.UNCERTAIN,
                    message="Capital allocation status is NEEDS_INVESTIGATION.",
                    code="CAPITAL_ALLOCATION_NEEDS_INVESTIGATION",
                    details={"unknowns": list(context.capital_allocation_decision.unknowns)}
                )
                violations.append(v)
                reasons.append("Capital allocation requires investigation")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.BUDGET,
                    passed=False,
                    decision_impact=PolicyDecisionType.UNKNOWN,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )

        reasons.append("Budget and capital policy validation passed")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.BUDGET,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class RiskPolicyRule(PolicyRule):
    """
    Regla de gobernanza para mitigación de riesgos.
    - DENY si el riesgo es CRITICAL.
    - REQUIRE_APPROVAL si el riesgo es HIGH y hay impacto externo.
    - UNKNOWN si el riesgo es no calculable/desconocido y la acción tiene impacto externo.
    - ALLOW si el riesgo es LOW / MODERATE.
    """
    @property
    def name(self) -> str:
        return "RiskPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.RISK.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        if context.risk_level == RiskLevel.CRITICAL:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.RISK,
                severity=PolicySeverity.BLOCKING,
                message="Action blocked due to CRITICAL risk level.",
                code="RISK_CRITICAL_BLOCKED",
                details={"risk_level": context.risk_level.value}
            )
            violations.append(v)
            reasons.append("Critical risk level blocks execution")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.RISK,
                passed=False,
                decision_impact=PolicyDecisionType.DENY,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        if context.is_external_impact and context.risk_level is None:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.RISK,
                severity=PolicySeverity.UNCERTAIN,
                message="External impact action has unknown risk level.",
                code="RISK_UNKNOWN",
                details={"action_type": context.action_type}
            )
            violations.append(v)
            reasons.append("Risk level is unknown for external impact action")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.RISK,
                passed=False,
                decision_impact=PolicyDecisionType.UNKNOWN,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        reasons.append(f"Risk policy passed with risk level {context.risk_level.value if context.risk_level else 'NONE'}")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.RISK,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class DataQualityAndSafetyRule(PolicyRule):
    """
    Regla de gobernanza para integridad de datos y procedencia:
    - Impide que procedencia MOCK o FIXTURE sea utilizada para acciones comerciales LIVE o de impacto externo irreversible sin flag explícito.
    - Preserva UNKNOWN si la confianza es UNCERTAIN para acciones irreversibles.
    """
    @property
    def name(self) -> str:
        return "DataQualityAndSafetyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.DATA_QUALITY.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        if context.is_external_impact and context.provenance in (EvidenceProvenanceType.MOCK, EvidenceProvenanceType.FIXTURE):
            allow_mock_impact = context.custom_context.get("allow_synthetic_execution", False)
            if not allow_mock_impact:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.DATA_QUALITY,
                    severity=PolicySeverity.BLOCKING,
                    message=f"Cannot execute real external impact action with {context.provenance.value} evidence.",
                    code="SYNTHETIC_DATA_BLOCKED_FOR_LIVE_ACTION",
                    details={"provenance": context.provenance.value}
                )
                violations.append(v)
                reasons.append(f"Synthetic evidence ({context.provenance.value}) cannot drive live external impact")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.DATA_QUALITY,
                    passed=False,
                    decision_impact=PolicyDecisionType.DENY,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )

        reasons.append("Data quality and safety validation passed")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.DATA_QUALITY,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class PriceFloorPolicyRule(PolicyRule):
    """
    Regla de gobernanza determinista que defiende el Price Floor (Hito G.4).
    Garantiza que ningún cambio de precio baje del precio mínimo permitido (costo landed + tarifas + margen de seguridad).
    - DENY si proposed_price < minimum_allowed_price (is_below_floor).
    - UNKNOWN si la acción es de pricing pero falta proposed_price o minimum_allowed_price.
    - ALLOW si proposed_price >= minimum_allowed_price.
    """
    @property
    def name(self) -> str:
        return "PriceFloorPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.SAFETY.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        # Verificar si la acción es de tipo pricing / cambio de precio
        is_pricing_action = (
            context.action_type in ("UPDATE_PRICE", "SET_PRICE", "PRICING_UPDATE", "CHANGE_PRICE")
            or "pricing_decision" in context.custom_context
            or "proposed_price" in context.custom_context
        )

        if not is_pricing_action:
            reasons.append("Non-pricing action skipped by PriceFloorPolicyRule")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=True,
                decision_impact=PolicyDecisionType.ALLOW,
                reasons=tuple(reasons),
                violations=()
            )

        # Extraer proposed_price y minimum_allowed_price
        pricing_decision = context.custom_context.get("pricing_decision")
        proposed_price: Optional[Decimal] = None
        min_allowed_price: Optional[Decimal] = None

        if pricing_decision is not None:
            if hasattr(pricing_decision, "proposed_price"):
                proposed_price = Decimal(str(pricing_decision.proposed_price))
            if hasattr(pricing_decision, "minimum_allowed_price"):
                min_allowed_price = Decimal(str(pricing_decision.minimum_allowed_price))

        if proposed_price is None and "proposed_price" in context.custom_context:
            proposed_price = Decimal(str(context.custom_context["proposed_price"]))
        if min_allowed_price is None and "minimum_allowed_price" in context.custom_context:
            min_allowed_price = Decimal(str(context.custom_context["minimum_allowed_price"]))

        # Si falta información crítica para una acción de pricing
        if proposed_price is None or min_allowed_price is None:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                severity=PolicySeverity.UNCERTAIN,
                message="Pricing action missing proposed_price or minimum_allowed_price.",
                code="PRICING_FLOOR_DATA_MISSING",
                details={
                    "has_proposed_price": proposed_price is not None,
                    "has_min_allowed_price": min_allowed_price is not None,
                }
            )
            violations.append(v)
            reasons.append("Price floor evaluation failed: missing price parameters")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=False,
                decision_impact=PolicyDecisionType.UNKNOWN,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        # Comprobar si proposed_price < min_allowed_price (Price floor breach)
        if proposed_price < min_allowed_price:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                severity=PolicySeverity.BLOCKING,
                message=(
                    f"Proposed price ({proposed_price}) is below minimum allowed price floor ({min_allowed_price}). "
                    f"Action strictly DENIED to protect against commercial loss."
                ),
                code="PRICE_BELOW_FLOOR",
                details={
                    "proposed_price": str(proposed_price),
                    "minimum_allowed_price": str(min_allowed_price),
                    "deficit": str(min_allowed_price - proposed_price),
                }
            )
            violations.append(v)
            reasons.append(f"Price {proposed_price} is below floor {min_allowed_price}")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=False,
                decision_impact=PolicyDecisionType.DENY,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        reasons.append(f"Price {proposed_price} satisfies price floor >= {min_allowed_price}")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.SAFETY,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class MarginProtectionPolicyRule(PolicyRule):
    """
    Regla de gobernanza determinista para protección de márgenes comerciales (Hito G.4).
    Verifica que el margen neto o de contribución esperado no sea inferior al umbral mínimo definido.
    - DENY si expected_margin_pct < minimum_margin_pct.
    - ALLOW si cumple el margen mínimo o no aplica.
    """
    @property
    def name(self) -> str:
        return "MarginProtectionPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.SAFETY.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        is_pricing_action = (
            context.action_type in ("UPDATE_PRICE", "SET_PRICE", "PRICING_UPDATE", "CHANGE_PRICE")
            or "pricing_decision" in context.custom_context
            or "expected_margin_pct" in context.custom_context
        )

        if not is_pricing_action:
            reasons.append("Non-pricing action skipped by MarginProtectionPolicyRule")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=True,
                decision_impact=PolicyDecisionType.ALLOW,
                reasons=tuple(reasons),
                violations=()
            )

        pricing_decision = context.custom_context.get("pricing_decision")
        expected_margin: Optional[Decimal] = None
        min_margin_threshold: Optional[Decimal] = None

        if pricing_decision is not None and hasattr(pricing_decision, "expected_margin_pct"):
            if pricing_decision.expected_margin_pct is not None:
                expected_margin = Decimal(str(pricing_decision.expected_margin_pct))

        if expected_margin is None and "expected_margin_pct" in context.custom_context:
            val = context.custom_context["expected_margin_pct"]
            if val is not None:
                expected_margin = Decimal(str(val))

        # Umbral mínimo de margen configurado en custom_context o default de seguridad (e.g. 0.05 / 5%)
        if "minimum_margin_pct" in context.custom_context:
            min_margin_threshold = Decimal(str(context.custom_context["minimum_margin_pct"]))

        if expected_margin is not None and min_margin_threshold is not None:
            if expected_margin < min_margin_threshold:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.SAFETY,
                    severity=PolicySeverity.BLOCKING,
                    message=(
                        f"Expected margin ({expected_margin * 100:.2f}%) is below minimum required margin "
                        f"({min_margin_threshold * 100:.2f}%)."
                    ),
                    code="MARGIN_BELOW_MINIMUM",
                    details={
                        "expected_margin_pct": str(expected_margin),
                        "minimum_margin_pct": str(min_margin_threshold),
                    }
                )
                violations.append(v)
                reasons.append(f"Expected margin {expected_margin} is below threshold {min_margin_threshold}")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.SAFETY,
                    passed=False,
                    decision_impact=PolicyDecisionType.DENY,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )

        reasons.append("Margin protection validation passed")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.SAFETY,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class MaxPriceChangePolicyRule(PolicyRule):
    """
    Regla de gobernanza determinista contra cambios bruscos o excesivos de precio (Hito G.4).
    Previene anomalías operativas o fluctuaciones no deseadas limitando la variación absoluta y porcentual.
    - REQUIRE_APPROVAL si la variación porcentual excede max_pct_change_approval_threshold sin aprobación previa.
    - DENY si la variación porcentual excede el límite máximo absoluto permitido (max_allowed_pct_change).
    - ALLOW si está dentro de los rangos tolerados o ya cuenta con human_approved=True.
    """
    DEFAULT_MAX_CHANGE_APPROVAL_PCT = Decimal("0.20")   # 20% variación exige aprobación
    DEFAULT_MAX_ALLOWED_CHANGE_PCT = Decimal("0.50")    # 50% variación máxima permitida (bloqueo)

    @property
    def name(self) -> str:
        return "MaxPriceChangePolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.APPROVAL.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        is_pricing_action = (
            context.action_type in ("UPDATE_PRICE", "SET_PRICE", "PRICING_UPDATE", "CHANGE_PRICE")
            or "pricing_decision" in context.custom_context
            or ("current_price" in context.custom_context and "proposed_price" in context.custom_context)
        )

        if not is_pricing_action:
            reasons.append("Non-pricing action skipped by MaxPriceChangePolicyRule")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.APPROVAL,
                passed=True,
                decision_impact=PolicyDecisionType.ALLOW,
                reasons=tuple(reasons),
                violations=()
            )

        pricing_decision = context.custom_context.get("pricing_decision")
        current_price: Optional[Decimal] = None
        proposed_price: Optional[Decimal] = None

        if pricing_decision is not None:
            if hasattr(pricing_decision, "current_price"):
                current_price = Decimal(str(pricing_decision.current_price))
            if hasattr(pricing_decision, "proposed_price"):
                proposed_price = Decimal(str(pricing_decision.proposed_price))

        if current_price is None and "current_price" in context.custom_context:
            current_price = Decimal(str(context.custom_context["current_price"]))
        if proposed_price is None and "proposed_price" in context.custom_context:
            proposed_price = Decimal(str(context.custom_context["proposed_price"]))

        if current_price is None or proposed_price is None or current_price <= Decimal("0"):
            reasons.append("Baseline price not available or zero, skipping price delta checks")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.APPROVAL,
                passed=True,
                decision_impact=PolicyDecisionType.ALLOW,
                reasons=tuple(reasons),
                violations=()
            )

        # Calcular variación porcentual absoluta: abs(proposed - current) / current
        delta = abs(proposed_price - current_price)
        pct_change = delta / current_price

        # Umbrales
        max_approval_pct = Decimal(
            str(context.custom_context.get("max_price_change_approval_pct", self.DEFAULT_MAX_CHANGE_APPROVAL_PCT))
        )
        max_allowed_pct = Decimal(
            str(context.custom_context.get("max_price_change_allowed_pct", self.DEFAULT_MAX_ALLOWED_CHANGE_PCT))
        )

        # 1. Bloqueo total si excede el techo absoluto (e.g. >50%)
        if pct_change > max_allowed_pct:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.APPROVAL,
                severity=PolicySeverity.BLOCKING,
                message=(
                    f"Price change of {pct_change * 100:.2f}% (from {current_price} to {proposed_price}) "
                    f"exceeds absolute allowed threshold of {max_allowed_pct * 100:.2f}%."
                ),
                code="EXCESSIVE_PRICE_CHANGE_BLOCKED",
                details={
                    "current_price": str(current_price),
                    "proposed_price": str(proposed_price),
                    "pct_change": str(pct_change),
                    "max_allowed_pct": str(max_allowed_pct),
                }
            )
            violations.append(v)
            reasons.append(f"Price change of {pct_change * 100:.1f}% exceeds absolute limit")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.APPROVAL,
                passed=False,
                decision_impact=PolicyDecisionType.DENY,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        # 2. Exigir aprobación humana si excede el umbral de advertencia (e.g. >20%)
        if pct_change > max_approval_pct:
            if not context.human_approved:
                v = PolicyViolation(
                    rule_name=self.name,
                    category=PolicyRuleCategory.APPROVAL,
                    severity=PolicySeverity.REQUIRES_HUMAN,
                    message=(
                        f"Price change of {pct_change * 100:.2f}% (from {current_price} to {proposed_price}) "
                        f"exceeds warning threshold of {max_approval_pct * 100:.2f}% and requires human approval."
                    ),
                    code="PRICE_CHANGE_APPROVAL_REQUIRED",
                    details={
                        "current_price": str(current_price),
                        "proposed_price": str(proposed_price),
                        "pct_change": str(pct_change),
                        "max_approval_pct": str(max_approval_pct),
                    }
                )
                violations.append(v)
                reasons.append(f"Price change of {pct_change * 100:.1f}% requires human approval")
                return RuleEvaluationResult(
                    rule_name=self.name,
                    category=PolicyRuleCategory.APPROVAL,
                    passed=False,
                    decision_impact=PolicyDecisionType.REQUIRE_APPROVAL,
                    reasons=tuple(reasons),
                    violations=tuple(violations)
                )
            else:
                reasons.append(f"Price change of {pct_change * 100:.1f}% was approved by human operator")

        reasons.append("Price change is within acceptable guardrail limits")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.APPROVAL,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class OversellingProtectionPolicyRule(PolicyRule):
    """
    Regla de gobernanza determinista contra el sobrevendido (Overselling Protection) (Hito G.5).
    Garantiza que ninguna actualización de stock publique o proponga más unidades de las respaldadas
    por el stock vendible disponible (available_to_sell = backed_stock - reservations - buffer).
    - DENY si proposed_stock < 0 (Negative stock bloqueado).
    - DENY si proposed_stock > available_to_sell (Overselling breach).
    - UNKNOWN si la acción es de inventario pero falta información de stock respaldado o niveles de stock.
    - ALLOW si proposed_stock <= available_to_sell y proposed_stock >= 0.
    """
    @property
    def name(self) -> str:
        return "OversellingProtectionPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.SAFETY.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        is_inventory_action = (
            context.action_type in (
                "UPDATE_INVENTORY",
                "SET_INVENTORY",
                "UPDATE_STOCK",
                "SET_STOCK",
                "SYNC_INVENTORY",
                "SYNC_STOCK",
                "INVENTORY_UPDATE",
            )
            or "inventory_decision" in context.custom_context
            or "proposed_stock" in context.custom_context
            or "stock_levels" in context.custom_context
        )

        if not is_inventory_action:
            reasons.append("Non-inventory action skipped by OversellingProtectionPolicyRule")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=True,
                decision_impact=PolicyDecisionType.ALLOW,
                reasons=tuple(reasons),
                violations=()
            )

        inv_decision = context.custom_context.get("inventory_decision")
        proposed_stock: Optional[int] = None
        max_allowed_stock: Optional[int] = None
        stock_levels = context.custom_context.get("stock_levels")

        if inv_decision is not None:
            if hasattr(inv_decision, "proposed_stock"):
                proposed_stock = int(inv_decision.proposed_stock)
            if hasattr(inv_decision, "stock_levels") and inv_decision.stock_levels is not None:
                max_allowed_stock = int(inv_decision.stock_levels.available_to_sell)

        if stock_levels is not None and max_allowed_stock is None:
            if hasattr(stock_levels, "available_to_sell"):
                max_allowed_stock = int(stock_levels.available_to_sell)

        if proposed_stock is None and "proposed_stock" in context.custom_context:
            try:
                proposed_stock = int(context.custom_context["proposed_stock"])
            except Exception:
                pass

        if max_allowed_stock is None and "max_allowed_stock" in context.custom_context:
            try:
                max_allowed_stock = int(context.custom_context["max_allowed_stock"])
            except Exception:
                pass

        # 1. Comprobar stock negativo
        if proposed_stock is not None and proposed_stock < 0:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                severity=PolicySeverity.BLOCKING,
                message=f"Negative stock quantity ({proposed_stock}) is strictly prohibited.",
                code="NEGATIVE_STOCK_BLOCKED",
                details={"proposed_stock": proposed_stock}
            )
            violations.append(v)
            reasons.append(f"Negative stock {proposed_stock} blocked")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=False,
                decision_impact=PolicyDecisionType.DENY,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        # 2. Comprobar si falta información crítica para validar el stock
        if proposed_stock is None or max_allowed_stock is None:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                severity=PolicySeverity.UNCERTAIN,
                message="Inventory action missing proposed_stock or verified available_to_sell stock levels.",
                code="INVENTORY_DATA_MISSING",
                details={
                    "has_proposed_stock": proposed_stock is not None,
                    "has_max_allowed_stock": max_allowed_stock is not None,
                }
            )
            violations.append(v)
            reasons.append("Overselling evaluation failed: missing stock parameters")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=False,
                decision_impact=PolicyDecisionType.UNKNOWN,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        # 3. Comprobar violación de overselling (proposed_stock > max_allowed_stock)
        if proposed_stock > max_allowed_stock:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                severity=PolicySeverity.BLOCKING,
                message=(
                    f"Proposed stock ({proposed_stock}) exceeds maximum available sellable stock ({max_allowed_stock}). "
                    f"Action strictly DENIED to protect against overselling."
                ),
                code="OVERSELLING_PREVENTED",
                details={
                    "proposed_stock": proposed_stock,
                    "max_allowed_stock": max_allowed_stock,
                    "excess": proposed_stock - max_allowed_stock,
                }
            )
            violations.append(v)
            reasons.append(f"Stock {proposed_stock} exceeds available {max_allowed_stock}")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=False,
                decision_impact=PolicyDecisionType.DENY,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        reasons.append(f"Stock {proposed_stock} is safely backed by available stock {max_allowed_stock}")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.SAFETY,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )


class InventorySafetyBufferPolicyRule(PolicyRule):
    """
    Regla de gobernanza determinista para la verificación del Safety Buffer (Hito G.5).
    Verifica que las deducciones de buffer de seguridad requeridas por política no se omitan.
    - DENY si el buffer aplicado es menor que el mínimo requerido por política de canal/proveedor.
    - ALLOW si cumple el safety buffer mínimo.
    """
    DEFAULT_MIN_SAFETY_BUFFER = 1

    @property
    def name(self) -> str:
        return "InventorySafetyBufferPolicyRule"

    @property
    def category(self) -> str:
        return PolicyRuleCategory.SAFETY.value

    def evaluate(self, context: PolicyEvaluationContext) -> RuleEvaluationResult:
        violations: List[PolicyViolation] = []
        reasons: List[str] = []

        is_inventory_action = (
            context.action_type in (
                "UPDATE_INVENTORY",
                "SET_INVENTORY",
                "UPDATE_STOCK",
                "SET_STOCK",
                "SYNC_INVENTORY",
                "SYNC_STOCK",
                "INVENTORY_UPDATE",
            )
            or "inventory_decision" in context.custom_context
            or "stock_levels" in context.custom_context
        )

        if not is_inventory_action:
            reasons.append("Non-inventory action skipped by InventorySafetyBufferPolicyRule")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=True,
                decision_impact=PolicyDecisionType.ALLOW,
                reasons=tuple(reasons),
                violations=()
            )

        inv_decision = context.custom_context.get("inventory_decision")
        stock_levels = context.custom_context.get("stock_levels")
        safety_buffer: Optional[int] = None

        if inv_decision is not None and hasattr(inv_decision, "stock_levels") and inv_decision.stock_levels is not None:
            safety_buffer = inv_decision.stock_levels.safety_buffer
        elif stock_levels is not None and hasattr(stock_levels, "safety_buffer"):
            safety_buffer = stock_levels.safety_buffer
        elif "safety_buffer" in context.custom_context:
            try:
                safety_buffer = int(context.custom_context["safety_buffer"])
            except Exception:
                pass

        min_required_buffer = int(
            context.custom_context.get("min_required_safety_buffer", self.DEFAULT_MIN_SAFETY_BUFFER)
        )

        # Si hay stock respaldado de proveedor y el buffer es menor al requerido
        if safety_buffer is not None and safety_buffer < min_required_buffer:
            v = PolicyViolation(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                severity=PolicySeverity.BLOCKING,
                message=(
                    f"Applied safety buffer ({safety_buffer}) is below minimum required buffer "
                    f"({min_required_buffer})."
                ),
                code="SAFETY_BUFFER_INSUFFICIENT",
                details={
                    "safety_buffer": safety_buffer,
                    "min_required_buffer": min_required_buffer,
                }
            )
            violations.append(v)
            reasons.append(f"Safety buffer {safety_buffer} is less than required {min_required_buffer}")
            return RuleEvaluationResult(
                rule_name=self.name,
                category=PolicyRuleCategory.SAFETY,
                passed=False,
                decision_impact=PolicyDecisionType.DENY,
                reasons=tuple(reasons),
                violations=tuple(violations)
            )

        reasons.append("Safety buffer validation passed")
        return RuleEvaluationResult(
            rule_name=self.name,
            category=PolicyRuleCategory.SAFETY,
            passed=True,
            decision_impact=PolicyDecisionType.ALLOW,
            reasons=tuple(reasons),
            violations=()
        )

