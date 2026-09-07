"""
Unit Tests for N.7 — Financial Limits (Transversal N — Security, Governance & Safety).

Cubre los 16 casos canónicos obligatorios:
1. amount within limit
2. amount equal limit
3. amount above limit
4. missing policy != unlimited (fail-safe UNKNOWN/REJECTED)
5. Decimal only (prohibición de float en modelos y validación)
6. currency mismatch (bloqueo / UNKNOWN)
7. invalid amount (tipos inválidos)
8. negative amount (rechazo inmediato)
9. action-specific limit
10. resource/account-specific limit
11. deterministic decision (checksums estables)
12. policy versioning
13. high amount -> approval required if configured
14. N.6 approval not fabricated by N.7
15. N.3 DENY not overridden
16. no M.6 inference-cost confusion
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.domain.profit.models import Money
from src.domain.financial_limit.models import (
    FinancialLimitType,
    FinancialLimitStatus,
    FinancialLimitReasonCode,
    FinancialLimitRule,
    FinancialLimitPolicy,
    FinancialLimitRequest,
    FinancialLimitDecision,
    compute_financial_decision_checksum,
)
from src.application.financial_limit.financial_limit_service import FinancialLimitService
from src.domain.financial_limit.ports import FinancialLimitPolicyRepositoryPort
from src.domain.identity.models import IdentityType, IdentityReference
from src.domain.authentication.models import (
    PrincipalContext,
    AuthenticationResult,
    AuthenticationStatus,
    AuthenticationMethod,
)
from src.domain.authorization.models import AuthorizationDecision, AuthorizationStatus
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.application.authorization.authorization_service import AuthorizationService
from src.application.authorization.authorization_guarded_action_executor import AuthorizationGuardedActionExecutor


class InMemoryFinancialPolicyRepository(FinancialLimitPolicyRepositoryPort):
    """Repositorio en memoria para tests unitarios."""

    def __init__(self):
        self._policies = {}

    def save_policy(self, policy: FinancialLimitPolicy) -> None:
        self._policies[policy.policy_name] = policy

    def get_policy(self, policy_name: str):
        return self._policies.get(policy_name)

    def list_policies(self):
        return tuple(self._policies.values())


class DummyClock:
    def __init__(self, fixed_now: datetime):
        self._now = fixed_now

    def now(self) -> datetime:
        return self._now


class MockActionExecutor(ActionExecutor):
    def __init__(self):
        self.call_count = 0
        self.last_decision = None

    def execute(self, decision, state):
        self.call_count += 1
        self.last_decision = decision
        return {"status": "SUCCESS", "called": True}


@pytest.fixture
def dummy_clock():
    return DummyClock(datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def standard_policy():
    rule_refund = FinancialLimitRule(
        rule_id="rule_refund_01",
        limit_type=FinancialLimitType.MAX_REFUND_AMOUNT,
        currency="USD",
        max_amount=Decimal("100.00"),
        allow_approval_override=True,
        target_action="ISSUE_REFUND",
    )
    rule_order = FinancialLimitRule(
        rule_id="rule_order_01",
        limit_type=FinancialLimitType.MAX_ORDER_VALUE,
        currency="USD",
        max_amount=Decimal("500.00"),
        min_amount=Decimal("10.00"),
        allow_approval_override=False,
        target_action="PLACE_ORDER",
    )
    rule_account_spec = FinancialLimitRule(
        rule_id="rule_acc_cl_01",
        limit_type=FinancialLimitType.MAX_TRANSACTION_AMOUNT,
        currency="CLP",
        max_amount=Decimal("50000"),
        account_id="acc_chile",
        target_action="TRANSFER_FUNDS",
    )
    return FinancialLimitPolicy(
        policy_name="default_commercial_policy",
        version="1.2.0",
        currency="USD",
        rules=(rule_refund, rule_order, rule_account_spec),
    )


@pytest.fixture
def repo_with_policy(standard_policy):
    repo = InMemoryFinancialPolicyRepository()
    repo.save_policy(standard_policy)
    return repo


@pytest.fixture
def financial_service(repo_with_policy, dummy_clock):
    return FinancialLimitService(
        policy_repository=repo_with_policy,
        default_policy_name="default_commercial_policy",
        clock=dummy_clock,
    )


# 1. amount within limit
def test_1_amount_within_limit(financial_service):
    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("50.00"), currency="USD"),
    )
    decision = financial_service.evaluate(req)
    assert decision.status == FinancialLimitStatus.WITHIN_LIMIT
    assert decision.is_within_limit is True
    assert decision.reason_code == FinancialLimitReasonCode.WITHIN_CONFIGURED_LIMIT
    assert decision.remaining_allowance == Decimal("50.00")
    assert decision.limit_value == Decimal("100.00")


# 2. amount equal limit
def test_2_amount_equal_limit(financial_service):
    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("100.00"), currency="USD"),
    )
    decision = financial_service.evaluate(req)
    assert decision.status == FinancialLimitStatus.WITHIN_LIMIT
    assert decision.is_within_limit is True
    assert decision.remaining_allowance == Decimal("0.00")


# 3. amount above limit (strict rejection without approval override)
def test_3_amount_above_limit_strict_block(financial_service):
    req = FinancialLimitRequest(
        action="PLACE_ORDER",
        money=Money(amount=Decimal("600.00"), currency="USD"),
    )
    decision = financial_service.evaluate(req)
    assert decision.status == FinancialLimitStatus.LIMIT_EXCEEDED
    assert decision.is_blocked is True
    assert decision.reason_code == FinancialLimitReasonCode.LIMIT_EXCEEDED_STRICT_BLOCK


# 4. missing policy != unlimited (fail-safe UNKNOWN)
def test_4_missing_policy_fail_safe():
    empty_repo = InMemoryFinancialPolicyRepository()
    service = FinancialLimitService(policy_repository=empty_repo, default_policy_name="non_existent")
    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("10.00"), currency="USD"),
    )
    decision = service.evaluate(req)
    assert decision.status == FinancialLimitStatus.UNKNOWN
    assert decision.is_blocked is True
    assert decision.reason_code == FinancialLimitReasonCode.POLICY_NOT_FOUND


# 5. Decimal only (prohibición de float en modelos y validación)
def test_5_decimal_only_enforcement(financial_service):
    with pytest.raises(TypeError):
        # Money ya valida o FinancialLimitRequest rechaza tipos no Decimal
        FinancialLimitRequest(
            action="ISSUE_REFUND",
            money=Money(amount=100.5, currency="USD"),  # type: ignore
        )

    with pytest.raises(TypeError):
        FinancialLimitRule(
            rule_id="invalid_rule",
            limit_type=FinancialLimitType.MAX_TRANSACTION_AMOUNT,
            currency="USD",
            max_amount=100.0,  # type: ignore
        )


# 6. currency mismatch (bloqueo / UNKNOWN)
def test_6_currency_mismatch(financial_service):
    # Política es USD, enviamos EUR
    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("50.00"), currency="EUR"),
    )
    decision = financial_service.evaluate(req)
    assert decision.status == FinancialLimitStatus.UNKNOWN
    assert decision.is_blocked is True
    assert decision.reason_code == FinancialLimitReasonCode.CURRENCY_MISMATCH


# 7. invalid amount
def test_7_invalid_amount_or_missing_money(financial_service):
    with pytest.raises(TypeError):
        FinancialLimitRequest(
            action="ISSUE_REFUND",
            money=None,  # type: ignore
        )


# 8. negative and zero amount handling
def test_8_negative_amount_rejection(financial_service):
    req_neg = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("-10.00"), currency="USD"),
    )
    decision_neg = financial_service.evaluate(req_neg)
    assert decision_neg.status == FinancialLimitStatus.REJECTED
    assert decision_neg.reason_code == FinancialLimitReasonCode.NEGATIVE_OR_ZERO_AMOUNT

    req_zero = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("0.00"), currency="USD"),
        allow_zero=False,
    )
    decision_zero = financial_service.evaluate(req_zero)
    assert decision_zero.status == FinancialLimitStatus.REJECTED
    assert decision_zero.reason_code == FinancialLimitReasonCode.NEGATIVE_OR_ZERO_AMOUNT


# 9. action-specific limit
def test_9_action_specific_limit(financial_service):
    # Refund limit is 100.00 USD, Order limit is 500.00 USD
    req_refund_150 = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("150.00"), currency="USD"),
    )
    dec_refund = financial_service.evaluate(req_refund_150)
    assert dec_refund.status == FinancialLimitStatus.APPROVAL_REQUIRED  # allow_approval_override is True

    req_order_150 = FinancialLimitRequest(
        action="PLACE_ORDER",
        money=Money(amount=Decimal("150.00"), currency="USD"),
    )
    dec_order = financial_service.evaluate(req_order_150)
    assert dec_order.status == FinancialLimitStatus.WITHIN_LIMIT


# 10. resource/account-specific limit
def test_10_account_specific_limit(financial_service):
    # Regla específica para account_id='acc_chile' en CLP
    policy_clp = FinancialLimitPolicy(
        policy_name="chile_policy",
        version="1.0.0",
        currency="CLP",
        rules=(
            FinancialLimitRule(
                rule_id="rule_chile_01",
                limit_type=FinancialLimitType.MAX_TRANSACTION_AMOUNT,
                currency="CLP",
                max_amount=Decimal("50000"),
                account_id="acc_chile",
                target_action="TRANSFER_FUNDS",
            ),
        ),
    )
    repo = InMemoryFinancialPolicyRepository()
    repo.save_policy(policy_clp)
    service = FinancialLimitService(policy_repository=repo, default_policy_name="chile_policy")

    # Cuenta A (acc_chile) dentro de 50.000 CLP
    req_acc_a = FinancialLimitRequest(
        action="TRANSFER_FUNDS",
        money=Money(amount=Decimal("40000"), currency="CLP"),
        account_id="acc_chile",
    )
    dec_a = service.evaluate(req_acc_a)
    assert dec_a.status == FinancialLimitStatus.WITHIN_LIMIT

    # Cuenta B (acc_other) no tiene regla en chile_policy -> RULE_NOT_FOUND (fail-safe)
    req_acc_b = FinancialLimitRequest(
        action="TRANSFER_FUNDS",
        money=Money(amount=Decimal("40000"), currency="CLP"),
        account_id="acc_other",
    )
    dec_b = service.evaluate(req_acc_b)
    assert dec_b.status == FinancialLimitStatus.UNKNOWN
    assert dec_b.reason_code == FinancialLimitReasonCode.RULE_NOT_FOUND


# 11. deterministic decision (checksums estables)
def test_11_deterministic_decision_checksum(dummy_clock):
    dec = FinancialLimitDecision(
        decision_id="fld_test_12345",
        identity_id="user_admin",
        action="ISSUE_REFUND",
        resource="order_999",
        amount=Decimal("50.00"),
        currency="USD",
        status=FinancialLimitStatus.WITHIN_LIMIT,
        reason_code=FinancialLimitReasonCode.WITHIN_CONFIGURED_LIMIT,
        reason="Within limit",
        policy_name="default_commercial_policy",
        policy_version="1.2.0",
        limit_value=Decimal("100.00"),
        evaluated_at=dummy_clock.now(),
        metadata={"order_id": "999"},
    )
    assert len(dec.checksum) == 64
    # Re-calcular exactamente con los mismos parámetros
    computed = compute_financial_decision_checksum(
        decision_id="fld_test_12345",
        identity_id="user_admin",
        action="ISSUE_REFUND",
        resource="order_999",
        amount=Decimal("50.00"),
        currency="USD",
        status="WITHIN_LIMIT",
        limit_value=Decimal("100.00"),
        policy_name="default_commercial_policy",
        policy_version="1.2.0",
        evaluated_at=dummy_clock.now(),
        metadata={"order_id": "999"},
    )
    assert dec.checksum == computed


# 12. policy versioning
def test_12_policy_versioning(repo_with_policy, dummy_clock):
    # Actualizar política a versión 2.0.0 con límite más estricto
    v2_policy = FinancialLimitPolicy(
        policy_name="default_commercial_policy",
        version="2.0.0",
        currency="USD",
        rules=(
            FinancialLimitRule(
                rule_id="rule_refund_v2",
                limit_type=FinancialLimitType.MAX_REFUND_AMOUNT,
                currency="USD",
                max_amount=Decimal("30.00"),
                allow_approval_override=True,
                target_action="ISSUE_REFUND",
            ),
        ),
    )
    repo_with_policy.save_policy(v2_policy)
    service = FinancialLimitService(
        policy_repository=repo_with_policy,
        default_policy_name="default_commercial_policy",
        clock=dummy_clock,
    )

    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("50.00"), currency="USD"),
    )
    dec = service.evaluate(req)
    assert dec.policy_version == "2.0.0"
    assert dec.status == FinancialLimitStatus.APPROVAL_REQUIRED
    assert dec.limit_value == Decimal("30.00")


# 13. high amount -> approval required if configured
def test_13_high_amount_approval_required(financial_service):
    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("150.00"), currency="USD"),
    )
    dec = financial_service.evaluate(req)
    assert dec.status == FinancialLimitStatus.APPROVAL_REQUIRED
    assert dec.requires_approval is True
    assert dec.reason_code == FinancialLimitReasonCode.LIMIT_EXCEEDED_APPROVAL_REQUIRED


# 14. N.6 approval not fabricated by N.7
def test_14_n6_approval_not_fabricated(financial_service):
    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("200.00"), currency="USD"),
    )
    dec = financial_service.evaluate(req)
    # N.7 retorna APPROVAL_REQUIRED pero no genera ninguna ApprovalEvidence
    assert dec.status == FinancialLimitStatus.APPROVAL_REQUIRED
    assert not hasattr(dec, "attached_evidence")


# 15. N.3 DENY not overridden by financial clearance
def test_15_n3_deny_not_overridden(financial_service):
    class DenyAuthzService:
        def authorize(self, request, **kwargs):
            return AuthorizationDecision(
                decision_id="authz_deny_01",
                identity_id=request.principal_context.identity_id if request.principal_context else "anon",
                action=request.action,
                resource=request.resource,
                status=AuthorizationStatus.DENY,
                reason_codes=("ACTION_FORBIDDEN",),
                reasons=("Forbidden",),
                evaluated_at=datetime.now(timezone.utc),
            )

    identity_ref = IdentityReference(
        identity_id="operator_01",
        canonical_identifier="canonical_operator_01",
        identity_type=IdentityType.USER,
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        principal=identity_ref,
        method=AuthenticationMethod.INTERNAL_SERVICE,
        provider="system",
        authenticated_at=datetime.now(timezone.utc),
    )
    principal = PrincipalContext(
        principal=identity_ref,
        auth_result=auth_res,
    )

    delegate = MockActionExecutor()
    guarded_executor = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=DenyAuthzService(),  # type: ignore
        financial_limit_service=financial_service,
        principal_context=principal,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Testing refund within limit but authz denied",
        target="order_123",
        parameters={"action_type": "ISSUE_REFUND", "amount": Decimal("20.00"), "currency": "USD"},
    )
    state = LoopState(mission_id="m_100", iteration=1, goal="process refund")

    result = guarded_executor.execute(decision, state)
    # Debe ser bloqueado por N.3 aunque el monto esté dentro del límite
    assert result["is_allowed"] is False
    assert "AUTHORIZATION_DENY" in result["status"]
    assert delegate.call_count == 0


# 16. no M.6 inference-cost confusion
def test_16_no_m6_inference_confusion(financial_service):
    # N.7 evalúa dinero comercial, no tokens ni costo de LLM
    req = FinancialLimitRequest(
        action="ISSUE_REFUND",
        money=Money(amount=Decimal("45.00"), currency="USD"),
        context={"commercial_reason": "damaged_goods"},
    )
    dec = financial_service.evaluate(req)
    assert dec.amount == Decimal("45.00")
    assert dec.currency == "USD"
    # Asegurar que no hay campos de tokens/inferencia de M.6
    assert not hasattr(dec, "prompt_tokens")
    assert not hasattr(dec, "completion_tokens")
    assert not hasattr(dec, "model_id")
