"""
Domain Models for N.7 — Financial Limits (Transversal N — Security, Governance & Safety).

Responde a la pregunta:
"¿Esta operación financiera/comercial está dentro de los límites económicos permitidos?"

Principios:
- Toda representación monetaria usa Decimal y Currency explícita (REUSE src.domain.profit.models.Money).
- Cero float, cero conversiones de divisa implícitas, cero redondeos silenciosos.
- Fail-secure: missing policy != unlimited (UNKNOWN / REJECTED por defecto).
- Integración N.6: límite excedido puede requerir aprobación según política, pero N.7 no inventa aprobaciones.
- Inmutabilidad estricta (frozen=True, MappingProxyType).
- Determinismo temporal vía ClockPort (K.7).
- Cero secretos N.5, cero CoT / razonamiento interno (K.2).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union

from src.domain.profit.models import Money
from src.domain.security.models import (
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)


class FinancialLimitType(str, Enum):
    """
    Tipos canónicos y extensibles de límites económicos de negocio (N.7).
    """
    MAX_TRANSACTION_AMOUNT = "MAX_TRANSACTION_AMOUNT"
    MAX_REFUND_AMOUNT = "MAX_REFUND_AMOUNT"
    MAX_ORDER_VALUE = "MAX_ORDER_VALUE"
    MAX_PRICE_CHANGE = "MAX_PRICE_CHANGE"
    MIN_ALLOWED_PRICE = "MIN_ALLOWED_PRICE"
    MAX_ALLOWED_PRICE = "MAX_ALLOWED_PRICE"
    MAX_EXPOSURE = "MAX_EXPOSURE"
    DAILY_SPEND_LIMIT = "DAILY_SPEND_LIMIT"


class FinancialLimitStatus(str, Enum):
    """
    Estados canónicos de decisión para límites financieros (N.7).
    """
    WITHIN_LIMIT = "WITHIN_LIMIT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class FinancialLimitReasonCode(str, Enum):
    """
    Códigos de razón estructurados y deterministas para N.7.
    """
    WITHIN_CONFIGURED_LIMIT = "WITHIN_CONFIGURED_LIMIT"
    LIMIT_EXCEEDED_STRICT_BLOCK = "LIMIT_EXCEEDED_STRICT_BLOCK"
    LIMIT_EXCEEDED_APPROVAL_REQUIRED = "LIMIT_EXCEEDED_APPROVAL_REQUIRED"
    BELOW_MINIMUM_ALLOWED = "BELOW_MINIMUM_ALLOWED"
    ABOVE_MAXIMUM_ALLOWED = "ABOVE_MAXIMUM_ALLOWED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    MISSING_OR_INVALID_AMOUNT = "MISSING_OR_INVALID_AMOUNT"
    NEGATIVE_OR_ZERO_AMOUNT = "NEGATIVE_OR_ZERO_AMOUNT"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    RULE_NOT_FOUND = "RULE_NOT_FOUND"
    RESOURCE_MISMATCH = "RESOURCE_MISMATCH"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    IDENTITY_NOT_AUTHORIZED_FOR_AMOUNT = "IDENTITY_NOT_AUTHORIZED_FOR_AMOUNT"
    DECISION_INTEGRITY_COMPROMISED = "DECISION_INTEGRITY_COMPROMISED"
    CORRUPTED_POLICY_OR_STATE = "CORRUPTED_POLICY_OR_STATE"
    EVALUATION_ERROR = "EVALUATION_ERROR"


def compute_financial_policy_checksum(
    policy_name: str,
    version: str,
    currency: str,
    rules: Sequence["FinancialLimitRule"],
    default_strict_rejection: bool,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Calcula un checksum SHA-256 canónico para una FinancialLimitPolicy.
    """
    rules_payload = []
    for r in rules:
        max_s = str(r.max_amount) if r.max_amount is not None else ""
        min_s = str(r.min_amount) if r.min_amount is not None else ""
        rules_payload.append(
            f"{r.rule_id}:{r.limit_type.value}:{r.currency}:{max_s}:{min_s}:{r.allow_approval_override}:{r.target_action}:{r.target_resource_type}:{r.account_id}"
        )
    sorted_rules = "|".join(sorted(rules_payload))
    meta_dict = dict(metadata) if metadata else {}
    sanitized_meta = sanitize_security_data(meta_dict)
    sorted_meta_json = json.dumps(sanitized_meta, sort_keys=True, default=str)

    payload = f"{policy_name}|{version}|{currency}|{default_strict_rejection}|{sorted_rules}|{sorted_meta_json}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_financial_decision_checksum(
    decision_id: str,
    identity_id: str,
    action: str,
    resource: str,
    amount: Decimal,
    currency: str,
    status: str,
    limit_value: Optional[Decimal],
    policy_name: str,
    policy_version: str,
    evaluated_at: datetime,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Calcula un checksum SHA-256 canónico y determinista sobre la decisión de límite financiero.
    """
    eval_dt_str = evaluated_at.isoformat() if evaluated_at else ""
    limit_str = str(limit_value) if limit_value is not None else ""

    meta_dict = dict(metadata) if metadata else {}
    sanitized_meta = sanitize_security_data(meta_dict)
    sorted_meta_json = json.dumps(sanitized_meta, sort_keys=True, default=str)

    payload = (
        f"{decision_id}|{identity_id}|{action}|{resource}|{str(amount)}|{currency}|"
        f"{status}|{limit_str}|{policy_name}|{policy_version}|{eval_dt_str}|{sorted_meta_json}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FinancialLimitRule:
    """
    Regla económica inmutable aplicable a una acción/contexto financiero.
    """
    rule_id: str
    limit_type: FinancialLimitType
    currency: str
    max_amount: Optional[Decimal] = None
    min_amount: Optional[Decimal] = None
    allow_approval_override: bool = False  # Si True, exceder el límite deriva en APPROVAL_REQUIRED en lugar de REJECTED
    target_action: str = ""
    target_resource_type: str = ""
    account_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.rule_id, "rule_id")
        if not self.currency or not isinstance(self.currency, str):
            raise ValueError("currency must be a non-empty string.")

        # Validar tipo de límite
        if not isinstance(self.limit_type, FinancialLimitType):
            try:
                object.__setattr__(self, "limit_type", FinancialLimitType(self.limit_type))
            except Exception as e:
                raise ValueError(f"Invalid FinancialLimitType: {self.limit_type}") from e

        # Validaciones de montos en Decimal
        if self.max_amount is not None:
            if not isinstance(self.max_amount, Decimal):
                raise TypeError(f"max_amount must be a Decimal, got {type(self.max_amount)}")
            if self.max_amount < Decimal("0"):
                raise ValueError("max_amount cannot be negative.")

        if self.min_amount is not None:
            if not isinstance(self.min_amount, Decimal):
                raise TypeError(f"min_amount must be a Decimal, got {type(self.min_amount)}")
            if self.min_amount < Decimal("0"):
                raise ValueError("min_amount cannot be negative.")

        if self.max_amount is not None and self.min_amount is not None:
            if self.min_amount > self.max_amount:
                raise ValueError(f"min_amount ({self.min_amount}) cannot be greater than max_amount ({self.max_amount}).")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))


@dataclass(frozen=True)
class FinancialLimitPolicy:
    """
    Política financiera inmutable, versionada y auditable.
    """
    policy_name: str
    version: str = "1.0.0"
    currency: str = "USD"
    rules: Tuple[FinancialLimitRule, ...] = field(default_factory=tuple)
    default_strict_rejection: bool = True  # Si no hay override de aprobación, rechaza estrictamente
    description: str = ""
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.policy_name, "policy_name")
        if not self.version or not isinstance(self.version, str):
            raise ValueError("version must be a non-empty string.")
        if not self.currency or not isinstance(self.currency, str):
            raise ValueError("currency must be a non-empty string.")
        if not isinstance(self.rules, tuple):
            object.__setattr__(self, "rules", tuple(self.rules))
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        expected_cksum = compute_financial_policy_checksum(
            policy_name=self.policy_name,
            version=self.version,
            currency=self.currency,
            rules=self.rules,
            default_strict_rejection=self.default_strict_rejection,
            metadata=self.metadata,
        )
        if not self.checksum:
            object.__setattr__(self, "checksum", expected_cksum)
        elif self.checksum != expected_cksum:
            raise ValueError("FinancialLimitPolicy integrity check failed: Checksum mismatch.")

    def find_rule(
        self,
        action: str,
        limit_type: Optional[FinancialLimitType] = None,
        resource_type: str = "",
        account_id: str = "",
    ) -> Optional[FinancialLimitRule]:
        """
        Encuentra la regla más específica aplicable al contexto.
        Prioridad de coincidencia:
        1. action + resource_type + account_id (+ limit_type si se especifica)
        2. action + account_id
        3. action + resource_type
        4. action
        5. limit_type genérico
        """
        best_match = None
        best_score = -1

        for r in self.rules:
            # Si se especificó limit_type y la regla no coincide, omitir
            if limit_type is not None and r.limit_type != limit_type:
                continue

            score = 0
            if r.target_action:
                if r.target_action != action:
                    continue
                score += 4

            if r.account_id:
                if r.account_id != account_id:
                    continue
                score += 2

            if r.target_resource_type:
                if r.target_resource_type != resource_type:
                    continue
                score += 1

            if score > best_score:
                best_score = score
                best_match = r

        return best_match


@dataclass(frozen=True)
class FinancialLimitRequest:
    """
    Petición inmutable para evaluación de límites económicos (N.7).
    """
    action: str
    money: Money
    resource: str = "global"
    resource_type: str = ""
    identity_id: str = "system"
    account_id: str = ""
    limit_type: Optional[FinancialLimitType] = None
    policy_name: Optional[str] = None
    correlation_id: str = ""
    allow_zero: bool = False
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.action or not isinstance(self.action, str):
            raise ValueError("action must be a non-empty string.")
        if not isinstance(self.money, Money):
            raise TypeError(f"money must be an instance of Money (Decimal+currency), got {type(self.money)}")
        if not isinstance(self.money.amount, Decimal):
            raise TypeError(f"money.amount must be a Decimal, got {type(self.money.amount)}")
        sanitized_ctx = sanitize_security_data(dict(self.context))
        object.__setattr__(self, "context", deep_freeze(sanitized_ctx))


@dataclass(frozen=True)
class FinancialLimitDecision:
    """
    Decisión inmutable, determinista y auditable sobre límites económicos (N.7).
    """
    decision_id: str
    identity_id: str
    action: str
    resource: str
    amount: Decimal
    currency: str
    status: FinancialLimitStatus
    reason_code: FinancialLimitReasonCode
    reason: str
    policy_name: str
    policy_version: str
    limit_value: Optional[Decimal] = None
    remaining_allowance: Optional[Decimal] = None
    applicable_rule_id: Optional[str] = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = ""
    checksum: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.decision_id, "decision_id")
        if not isinstance(self.amount, Decimal):
            raise TypeError(f"amount must be a Decimal, got {type(self.amount)}")
        if self.limit_value is not None and not isinstance(self.limit_value, Decimal):
            raise TypeError(f"limit_value must be a Decimal, got {type(self.limit_value)}")
        if self.remaining_allowance is not None and not isinstance(self.remaining_allowance, Decimal):
            raise TypeError(f"remaining_allowance must be a Decimal, got {type(self.remaining_allowance)}")

        if not isinstance(self.status, FinancialLimitStatus):
            try:
                object.__setattr__(self, "status", FinancialLimitStatus(self.status))
            except Exception as e:
                raise ValueError(f"Invalid FinancialLimitStatus: {self.status}") from e

        if not isinstance(self.reason_code, FinancialLimitReasonCode):
            try:
                object.__setattr__(self, "reason_code", FinancialLimitReasonCode(self.reason_code))
            except Exception as e:
                raise ValueError(f"Invalid FinancialLimitReasonCode: {self.reason_code}") from e

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        expected_checksum = compute_financial_decision_checksum(
            decision_id=self.decision_id,
            identity_id=self.identity_id,
            action=self.action,
            resource=self.resource,
            amount=self.amount,
            currency=self.currency,
            status=self.status.value,
            limit_value=self.limit_value,
            policy_name=self.policy_name,
            policy_version=self.policy_version,
            evaluated_at=self.evaluated_at,
            metadata=self.metadata,
        )
        if not self.checksum:
            object.__setattr__(self, "checksum", expected_checksum)
        elif self.checksum != expected_checksum:
            raise ValueError("FinancialLimitDecision integrity check failed: Checksum mismatch.")

    @property
    def is_within_limit(self) -> bool:
        return self.status == FinancialLimitStatus.WITHIN_LIMIT

    @property
    def requires_approval(self) -> bool:
        return self.status == FinancialLimitStatus.APPROVAL_REQUIRED

    @property
    def is_blocked(self) -> bool:
        return self.status in (
            FinancialLimitStatus.LIMIT_EXCEEDED,
            FinancialLimitStatus.REJECTED,
            FinancialLimitStatus.UNKNOWN,
            FinancialLimitStatus.ERROR,
        )
