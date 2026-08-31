import pytest
from decimal import Decimal
from typing import Dict, Any, Optional

from src.domain.mission.models import (
    LoopAction,
    LoopDecision,
    LoopState,
    LoopTraceEntry,
)
from src.domain.mission.ports import DecisionProvider, ActionExecutor
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits
from src.domain.publication.models import (
    ListingDraft,
    PublicationStatus,
    PublicationResult,
    SalesChannel,
    SalesChannelType,
)
from src.domain.publication.ports import PublicationPort
from src.domain.capital.models import CapitalBudget
from src.domain.supplier_intelligence.models import RiskLevel, EvidenceProvenanceType
from src.domain.market_intelligence.models import Confidence
from src.domain.policy.models import PolicyDecisionType
from src.domain.policy.engine import PolicyEngine
from src.application.policy.policy_enforcement_service import PolicyEnforcementService
from src.application.policy.policy_guarded_action_executor import PolicyGuardedActionExecutor
from src.application.publication.publication_action_executor import PublicationActionExecutor


class MockPublicationPort(PublicationPort):
    def __init__(self):
        self.published_requests = []
        self.calls_count = 0

    def publish(self, request) -> PublicationResult:
        self.published_requests.append(request)
        self.calls_count += 1
        return PublicationResult(
            publication_id=f"pub-{len(self.published_requests)}",
            external_reference="MLB123456789",
            status=PublicationStatus.PUBLISHED,
            channel=request.channel,
            permalink="https://mercadolibre.cl/MLB123456789",
        )

    def get_status(self, publication_id: str) -> Optional[PublicationResult]:
        return None


class ScenarioDecisionProvider(DecisionProvider):
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.index = 0

    def decide(self, state: LoopState) -> LoopDecision:
        if self.index < len(self.decisions):
            d = self.decisions[self.index]
            self.index += 1
            return d
        return LoopDecision(action=LoopAction.COMPLETE, reason="End of decisions scenario")


@pytest.fixture
def default_channel():
    return SalesChannel(
        channel_id="ml-cl",
        channel_type=SalesChannelType.MARKETPLACE,
        name="MercadoLibre Chile",
        region="CL",
        currency="CLP"
    )


def test_autonomous_loop_with_policy_governance_allows_safe_publication(default_channel):
    """
    Verifica que en un ciclo autónomo completo, una acción con bajo riesgo,
    presupuesto válido y procedencia LIVE pase por PolicyEngine y ejecute PublicationPort.
    """
    mock_port = MockPublicationPort()
    base_executor = PublicationActionExecutor(publication_port=mock_port)
    
    budget = CapitalBudget(
        budget_id="b-integ-1",
        total_capital=Decimal("500000"),
        reserved_capital=Decimal("100000"),
        committed_capital=Decimal("50000"),
        currency="CLP"
    )
    
    guarded_executor = PolicyGuardedActionExecutor(
        delegate_executor=base_executor,
        capital_budget=budget,
    )

    draft = ListingDraft(
        draft_id="draft-integ-1",
        product_reference_id="prod-1",
        title="Test Safe Listing",
        description="Safe listing description",
        price=Decimal("19990"),
        currency="CLP",
        available_quantity=10,
        channel=default_channel,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publish verified opportunity",
        parameters={
            "action_type": "PUBLISH",
            "draft": draft,
            "idempotency_key": "idemp-safe-pub-1",
            "risk_level": RiskLevel.LOW,
            "provenance": EvidenceProvenanceType.LIVE,
            "human_approved": True,
        }
    )

    provider = ScenarioDecisionProvider([
        decision,
        LoopDecision(action=LoopAction.COMPLETE, reason="Task done")
    ])

    loop = AutonomousLoop(
        decision_provider=provider,
        action_executor=guarded_executor,
        max_iterations=5,
    )

    result = loop.run(mission_id="m-integ-safe", goal="Safe autonomous publishing")

    assert result.status == "COMPLETED"
    assert mock_port.calls_count == 1
    assert len(mock_port.published_requests) == 1
    assert guarded_executor.latest_evaluation.decision == PolicyDecisionType.ALLOW


def test_autonomous_loop_with_policy_governance_blocks_mock_evidence(default_channel):
    """
    Verifica que el ciclo autónomo NO ejecute publicación externa si la evidencia proviene de MOCK/FIXTURE (Data Quality Violation).
    """
    mock_port = MockPublicationPort()
    base_executor = PublicationActionExecutor(publication_port=mock_port)
    
    guarded_executor = PolicyGuardedActionExecutor(
        delegate_executor=base_executor,
    )

    draft = ListingDraft(
        draft_id="draft-integ-mock",
        product_reference_id="prod-2",
        title="Mock Listing Test",
        description="Mock listing description",
        price=Decimal("15000"),
        currency="CLP",
        available_quantity=5,
        channel=default_channel,
    )

    # Decisión alimentada con evidencia MOCK
    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publish opportunity with mock evidence",
        parameters={
            "action_type": "PUBLISH",
            "draft": draft,
            "idempotency_key": "idemp-mock-pub-1",
            "risk_level": RiskLevel.LOW,
            "provenance": EvidenceProvenanceType.MOCK,  # VIOLACIÓN DE CALIDAD
            "human_approved": True,
        }
    )

    provider = ScenarioDecisionProvider([
        decision,
        LoopDecision(action=LoopAction.COMPLETE, reason="Abort loop")
    ])

    loop = AutonomousLoop(
        decision_provider=provider,
        action_executor=guarded_executor,
        max_iterations=5,
    )

    result = loop.run(mission_id="m-integ-mock", goal="Mock blocking test")

    # La acción externa jamás debe ser invocada
    assert mock_port.calls_count == 0
    assert len(mock_port.published_requests) == 0
    assert guarded_executor.latest_evaluation.decision == PolicyDecisionType.DENY
    assert any("MOCK" in r for r in guarded_executor.latest_evaluation.reasons)


def test_autonomous_loop_with_policy_governance_blocks_critical_risk(default_channel):
    """
    Verifica que el PolicyEngine bloquee una acción de impacto cuando el RiskLevel es CRITICAL.
    """
    mock_port = MockPublicationPort()
    base_executor = PublicationActionExecutor(publication_port=mock_port)
    
    guarded_executor = PolicyGuardedActionExecutor(
        delegate_executor=base_executor,
    )

    draft = ListingDraft(
        draft_id="draft-integ-risk",
        product_reference_id="prod-3",
        title="High Risk Listing",
        description="High risk listing description",
        price=Decimal("15000"),
        currency="CLP",
        available_quantity=2,
        channel=default_channel,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publish high risk item",
        parameters={
            "action_type": "PUBLISH",
            "draft": draft,
            "idempotency_key": "idemp-risk-pub-1",
            "risk_level": RiskLevel.CRITICAL,  # RIESGO CRÍTICO
            "provenance": EvidenceProvenanceType.LIVE,
            "human_approved": True,
        }
    )

    provider = ScenarioDecisionProvider([
        decision,
        LoopDecision(action=LoopAction.COMPLETE, reason="Finish")
    ])

    loop = AutonomousLoop(
        decision_provider=provider,
        action_executor=guarded_executor,
        max_iterations=5,
    )

    result = loop.run(mission_id="m-integ-risk", goal="Risk blocking test")

    assert mock_port.calls_count == 0
    assert guarded_executor.latest_evaluation.decision == PolicyDecisionType.DENY
    assert guarded_executor.latest_evaluation.violations[0].code == "RISK_CRITICAL_BLOCKED"


def test_autonomous_loop_with_policy_governance_blocks_unapproved_irreversible_action(default_channel):
    """
    Verifica que una acción irreversible sin human_approved sea detenida con REQUIRE_APPROVAL
    y NO ejecute llamadas externas.
    """
    mock_port = MockPublicationPort()
    base_executor = PublicationActionExecutor(publication_port=mock_port)
    
    guarded_executor = PolicyGuardedActionExecutor(
        delegate_executor=base_executor,
    )

    draft = ListingDraft(
        draft_id="draft-integ-appr",
        product_reference_id="prod-4",
        title="Unapproved Listing",
        description="Unapproved listing description",
        price=Decimal("25000"),
        currency="CLP",
        available_quantity=3,
        channel=default_channel,
    )

    decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publish without human approval",
        parameters={
            "action_type": "PUBLISH",
            "draft": draft,
            "idempotency_key": "idemp-appr-pub-1",
            "risk_level": RiskLevel.LOW,
            "provenance": EvidenceProvenanceType.LIVE,
            "human_approved": False,  # REQUIERE APROBACIÓN
        }
    )

    provider = ScenarioDecisionProvider([
        decision,
        LoopDecision(action=LoopAction.COMPLETE, reason="Finish")
    ])

    loop = AutonomousLoop(
        decision_provider=provider,
        action_executor=guarded_executor,
        max_iterations=5,
    )

    result = loop.run(mission_id="m-integ-appr", goal="Approval test")

    assert mock_port.calls_count == 0
    assert guarded_executor.latest_evaluation.decision == PolicyDecisionType.REQUIRE_APPROVAL
    assert guarded_executor.latest_evaluation.requires_approval is True
