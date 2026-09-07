"""
Application Service for N.7 — Financial Limits (Transversal N — Security, Governance & Safety).

Responsabilidades:
- Recibe el contexto financiero y monetario (Money: Decimal + Currency).
- Resuelve la política y regla económica aplicable.
- Valida límites (máximos, mínimos, variaciones).
- Controla compatibilidad de monedas estricta (rechazo inmediato ante currency mismatch sin FX confiable).
- Fail-secure: Missing policy -> UNKNOWN / REJECTED (nunca unlimited).
- Integración N.6: límite excedido determina si es bloqueo estricto o APPROVAL_REQUIRED.
- Emisión de auditoría determinista (K.1 AuditRecordType.FINANCIAL_LIMIT_EVALUATED / LIMIT_EXCEEDED).
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
import uuid
from types import MappingProxyType
from typing import Optional, Dict, Any, Sequence, Union

from src.domain.financial_limit.models import (
    FinancialLimitType,
    FinancialLimitStatus,
    FinancialLimitReasonCode,
    FinancialLimitRule,
    FinancialLimitPolicy,
    FinancialLimitRequest,
    FinancialLimitDecision,
)
from src.domain.financial_limit.ports import (
    FinancialLimitPolicyRepositoryPort,
    FinancialLimitServicePort,
)
from src.domain.profit.models import Money
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.security.models import sanitize_security_data

logger = logging.getLogger(__name__)


class FinancialLimitService(FinancialLimitServicePort):
    """
    Servicio de evaluación y gobernanza económica de límites comerciales (Hito N.7).
    """

    def __init__(
        self,
        policy_repository: Optional[FinancialLimitPolicyRepositoryPort] = None,
        default_policy_name: str = "default_financial_limit_policy",
        audit_repository: Optional[AuditRepositoryPort] = None,
        clock: Optional[Any] = None,
    ):
        self.policy_repository = policy_repository
        self.default_policy_name = default_policy_name
        self.audit_repository = audit_repository
        self.clock = clock

    def _get_current_time(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now"):
            now_val = self.clock.now()
            if now_val.tzinfo is None:
                return now_val.replace(tzinfo=timezone.utc)
            return now_val
        return datetime.now(timezone.utc)

    def evaluate(self, request: FinancialLimitRequest) -> FinancialLimitDecision:
        """
        Evalúa una petición de límite financiero contra las políticas configuradas.
        """
        eval_time = self._get_current_time()
        decision_id = f"fld_{uuid.uuid4().hex[:16]}"
        policy_name = request.policy_name or self.default_policy_name

        # 1. Validar integridad básica del monto
        if request.money is None or not isinstance(request.money, Money):
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.ERROR,
                reason_code=FinancialLimitReasonCode.MISSING_OR_INVALID_AMOUNT,
                reason="Invalid money object provided in request.",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        amount = request.money.amount
        currency = request.money.currency

        if not isinstance(amount, Decimal):
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.ERROR,
                reason_code=FinancialLimitReasonCode.MISSING_OR_INVALID_AMOUNT,
                reason=f"Amount must be a Decimal, got {type(amount).__name__}.",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        # Chequeo de montos negativos o cero (a menos que allow_zero sea explícito)
        if amount < Decimal("0"):
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.REJECTED,
                reason_code=FinancialLimitReasonCode.NEGATIVE_OR_ZERO_AMOUNT,
                reason="Negative financial amounts are strictly prohibited.",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        if amount == Decimal("0") and not request.allow_zero:
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.REJECTED,
                reason_code=FinancialLimitReasonCode.NEGATIVE_OR_ZERO_AMOUNT,
                reason="Zero amount is not allowed for this operation.",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        # 2. Cargar política aplicable
        if self.policy_repository is None:
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.UNKNOWN,
                reason_code=FinancialLimitReasonCode.POLICY_NOT_FOUND,
                reason="No financial policy repository configured. Fail-safe UNKNOWN applied.",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        try:
            policy = self.policy_repository.get_policy(policy_name)
        except Exception as e:
            logger.error(f"Error loading financial limit policy {policy_name}: {e}")
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.ERROR,
                reason_code=FinancialLimitReasonCode.CORRUPTED_POLICY_OR_STATE,
                reason=f"Corrupted or inaccessible financial limit policy: {e}",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        if policy is None:
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.UNKNOWN,
                reason_code=FinancialLimitReasonCode.POLICY_NOT_FOUND,
                reason=f"Financial limit policy '{policy_name}' not found. Fail-safe UNKNOWN applied (missing policy != unlimited).",
                policy_name=policy_name,
                policy_version="0.0.0",
                evaluated_at=eval_time,
            )

        # 3. Validar coincidencia de moneda con la política / regla
        # Si la moneda de la petición no coincide con la de la política
        if policy.currency and currency.upper() != policy.currency.upper():
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.UNKNOWN,
                reason_code=FinancialLimitReasonCode.CURRENCY_MISMATCH,
                reason=f"Currency mismatch: request currency '{currency}' does not match policy currency '{policy.currency}'. Implicit FX is forbidden.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=eval_time,
            )

        # 4. Encontrar regla aplicable
        rule = policy.find_rule(
            action=request.action,
            limit_type=request.limit_type,
            resource_type=request.resource_type,
            account_id=request.account_id,
        )

        if rule is None:
            # Si no hay regla específica, política fail-safe: UNKNOWN / REJECTED
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.UNKNOWN,
                reason_code=FinancialLimitReasonCode.RULE_NOT_FOUND,
                reason=f"No matching financial rule found for action '{request.action}' in policy '{policy.policy_name}'. Missing rule != unlimited.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                evaluated_at=eval_time,
            )

        # Validar moneda específica de la regla
        if rule.currency and currency.upper() != rule.currency.upper():
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.UNKNOWN,
                reason_code=FinancialLimitReasonCode.CURRENCY_MISMATCH,
                reason=f"Currency mismatch: request currency '{currency}' does not match rule currency '{rule.currency}'.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                applicable_rule_id=rule.rule_id,
                evaluated_at=eval_time,
            )

        # 5. Evaluar límites numéricos
        limit_val = rule.max_amount
        min_val = rule.min_amount

        # Chequeo de mínimo permitido
        if min_val is not None and amount < min_val:
            return self._build_decision(
                decision_id=decision_id,
                request=request,
                status=FinancialLimitStatus.REJECTED,
                reason_code=FinancialLimitReasonCode.BELOW_MINIMUM_ALLOWED,
                reason=f"Amount {amount} {currency} is below minimum allowed {min_val} {currency}.",
                policy_name=policy.policy_name,
                policy_version=policy.version,
                limit_value=min_val,
                applicable_rule_id=rule.rule_id,
                evaluated_at=eval_time,
            )

        # Chequeo de máximo permitido
        if limit_val is not None and amount > limit_val:
            # Si permite override de aprobación (N.6)
            if rule.allow_approval_override:
                return self._build_decision(
                    decision_id=decision_id,
                    request=request,
                    status=FinancialLimitStatus.APPROVAL_REQUIRED,
                    reason_code=FinancialLimitReasonCode.LIMIT_EXCEEDED_APPROVAL_REQUIRED,
                    reason=f"Amount {amount} {currency} exceeds autonomous limit {limit_val} {currency}. Human approval required.",
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                    limit_value=limit_val,
                    applicable_rule_id=rule.rule_id,
                    evaluated_at=eval_time,
                )
            else:
                return self._build_decision(
                    decision_id=decision_id,
                    request=request,
                    status=FinancialLimitStatus.LIMIT_EXCEEDED,
                    reason_code=FinancialLimitReasonCode.LIMIT_EXCEEDED_STRICT_BLOCK,
                    reason=f"Amount {amount} {currency} exceeds strict limit {limit_val} {currency} without approval override option.",
                    policy_name=policy.policy_name,
                    policy_version=policy.version,
                    limit_value=limit_val,
                    applicable_rule_id=rule.rule_id,
                    evaluated_at=eval_time,
                )

        # Dentro de los límites
        remaining = (limit_val - amount) if limit_val is not None else None
        return self._build_decision(
            decision_id=decision_id,
            request=request,
            status=FinancialLimitStatus.WITHIN_LIMIT,
            reason_code=FinancialLimitReasonCode.WITHIN_CONFIGURED_LIMIT,
            reason=f"Amount {amount} {currency} is within configured limit.",
            policy_name=policy.policy_name,
            policy_version=policy.version,
            limit_value=limit_val,
            remaining_allowance=remaining,
            applicable_rule_id=rule.rule_id,
            evaluated_at=eval_time,
        )

    def _build_decision(
        self,
        decision_id: str,
        request: FinancialLimitRequest,
        status: FinancialLimitStatus,
        reason_code: FinancialLimitReasonCode,
        reason: str,
        policy_name: str,
        policy_version: str,
        evaluated_at: datetime,
        limit_value: Optional[Decimal] = None,
        remaining_allowance: Optional[Decimal] = None,
        applicable_rule_id: Optional[str] = None,
    ) -> FinancialLimitDecision:
        """Construye y audita la decisión financiera inmutable."""
        amt = request.money.amount if (request.money and isinstance(request.money.amount, Decimal)) else Decimal("0")
        curr = request.money.currency if request.money else "UNKNOWN"

        sanitized_ctx = sanitize_security_data(dict(request.context))

        decision = FinancialLimitDecision(
            decision_id=decision_id,
            identity_id=request.identity_id,
            action=request.action,
            resource=request.resource,
            amount=amt,
            currency=curr,
            status=status,
            reason_code=reason_code,
            reason=reason,
            policy_name=policy_name,
            policy_version=policy_version,
            limit_value=limit_value,
            remaining_allowance=remaining_allowance,
            applicable_rule_id=applicable_rule_id,
            evaluated_at=evaluated_at,
            correlation_id=request.correlation_id,
            metadata=sanitized_ctx,
        )

        self._record_audit(decision)
        return decision

    def _record_audit(self, decision: FinancialLimitDecision) -> None:
        """Registra el evento de auditoría K.1 si el audit repository está disponible."""
        if self.audit_repository is None:
            return

        record_type = AuditRecordType.FINANCIAL_LIMIT_EVALUATED
        if decision.status in (FinancialLimitStatus.LIMIT_EXCEEDED, FinancialLimitStatus.APPROVAL_REQUIRED):
            record_type = AuditRecordType.LIMIT_EXCEEDED

        metadata = {
            "decision_id": decision.decision_id,
            "action": decision.action,
            "resource": decision.resource,
            "amount": str(decision.amount),
            "currency": decision.currency,
            "status": decision.status.value,
            "reason_code": decision.reason_code.value,
            "limit_value": str(decision.limit_value) if decision.limit_value is not None else None,
            "policy_name": decision.policy_name,
            "policy_version": decision.policy_version,
            "applicable_rule_id": decision.applicable_rule_id,
        }
        if decision.metadata:
            metadata.update(dict(decision.metadata))

        audit_record = AuditRecord(
            audit_id=f"rec_fin_{uuid.uuid4().hex[:16]}",
            record_type=record_type,
            actor=AuditActor(
                actor_type=AuditActorType.POLICY_ENGINE,
                actor_id=decision.identity_id,
            ),
            subject_type="financial_resource",
            subject_id=decision.resource,
            action_or_operation=decision.action,
            status=decision.status.value,
            correlation_id=decision.correlation_id or "financial_limit_eval",
            occurred_at=decision.evaluated_at,
            metadata=metadata,
        )

        try:
            self.audit_repository.append(audit_record)
        except Exception as e:
            logger.warning(f"Failed to record audit event for financial limit evaluation: {e}")
