import pytest
from decimal import Decimal
from typing import Dict, Any, List

from src.domain.mission.models import (
    LoopDecision,
    LoopAction,
    LoopState,
    MissionStatus,
)
from src.domain.mission.ports import DecisionProvider
from src.domain.market_intelligence.models import (
    MarketEvidence,
    MarketListing,
    Marketplace,
    Money,
    Confidence,
    SignalType,
)
from src.domain.opportunity.models import Opportunity, EvidenceSufficiency, OpportunityReadiness
from src.infrastructure.suppliers.directory_supplier_source import DirectorySupplierSource
from src.infrastructure.persistence.data.json.supplier_repository import JsonSupplierRepository
from src.application.supplier_intelligence.autonomous_supplier_discovery_service import (
    AutonomousSupplierDiscoveryService,
)
from src.application.supplier_intelligence.supplier_discovery_action_executor import (
    SupplierDiscoveryActionExecutor,
)
from src.application.mission.autonomous_loop import LoopLimits


class ScriptedSupplierDecisionProvider(DecisionProvider):
    """DecisionProvider determinista de prueba que ejecuta el loop de descubrimiento."""
    def __init__(self, plan: List[LoopDecision]):
        self.plan = plan
        self.index = 0

    def decide(self, state: LoopState) -> LoopDecision:
        if self.index < len(self.plan):
            dec = self.plan[self.index]
            self.index += 1
            return dec
        return LoopDecision(
            action=LoopAction.COMPLETE,
            reason="All supplier steps completed.",
            confidence=0.9,
        )


def test_supplier_discovery_loop_integration(tmp_path):
    # 1. Crear Oportunidad simulada validada de Hito B
    listing = MarketListing(
        external_id="MLC12345678",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Disco Estado Solido SSD Kingston A400 480GB SATA3 2.5",
        price=Money(amount=Decimal("38990"), currency="CLP"),
        sold_quantity=120,
        available_quantity=50,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Hard Drives",
    )
    evidence = MarketEvidence(
        listing=listing,
    )
    opportunity = Opportunity(
        opportunity_id="OPP-TEST-001",
        product_id="PROD-SSD-480",
        title="Disco Estado Solido SSD Kingston A400 480GB SATA3 2.5",
        listing=listing,
        evidence=evidence,
        score=Decimal("85.0"),
        confidence=Confidence.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        readiness=OpportunityReadiness.READY,
        provenance={"brand": "Kingston", "model": "A400", "sku": "SA400S37/480G"},
    )

    # 2. Configurar fuentes y repositorio
    source = DirectorySupplierSource(directory_path="data/suppliers")
    repo = JsonSupplierRepository(tmp_path)

    # 3. Plan del DecisionProvider: DISCOVER -> COMPARE -> COMPLETE
    plan = [
        LoopDecision(
            action=LoopAction.CONTINUE,
            target="Disco Estado Solido SSD Kingston A400 480GB SATA3 2.5",
            parameters={"operation": "DISCOVER", "brand": "Kingston", "model": "A400", "sku": "SA400S37/480G"},
            reason="Discover suppliers in local and wholesale catalogs",
        ),
        LoopDecision(
            action=LoopAction.CONTINUE,
            target="SUP-002",
            parameters={"operation": "COMPARE", "supplier_a": "SUP-002", "supplier_b": "SUP-003"},
            reason="Compare top 2 suppliers",
        ),
        LoopDecision(
            action=LoopAction.COMPLETE,
            reason="Supplier discovery and ranking successfully completed",
            confidence=0.95,
        )
    ]
    provider = ScriptedSupplierDecisionProvider(plan)

    service = AutonomousSupplierDiscoveryService(
        decision_provider=provider,
        sources=[source],
        supplier_repository=repo,
        default_max_iterations=5,
    )

    result = service.execute_supplier_discovery_mission(opportunity=opportunity)

    assert result.status == MissionStatus.COMPLETED
    assert result.output["candidates_count"] >= 3
    assert result.output["best_known_supplier"] is not None
    assert result.output["best_known_supplier"]["score"] > 50.0
    assert result.output["best_known_supplier"]["supplier_id"] in ["SUP-002", "SUP-003"]
    assert len(result.output["best_supplier_evolution"]) >= 1


def test_supplier_discovery_no_fabricated_data_and_pivot():
    # Test que verifica que si se busca un producto inexistente en las fuentes:
    # 1. No inventa candidatos ni proveedores
    # 2. Los desconocidos se marcan
    # 3. El executor puede pivotar
    source = DirectorySupplierSource(directory_path="data/suppliers")
    executor = SupplierDiscoveryActionExecutor(sources=[source])

    state = LoopState(mission_id="m1", iteration=0, goal="test", current_target=None)

    # Descubrir un producto completamente inexistente
    dec = LoopDecision(
        action=LoopAction.CONTINUE,
        parameters={"operation": "DISCOVER", "query": "Guitarra Electrica Fender Stratocaster"},
        reason="Search guitar",
    )
    res = executor.execute(dec, state)
    assert res["status"] == "SUCCESS"
    assert len(res["candidates"]) >= 1
    # Todos deben tener NO_MATCH y score bajo
    for c in res["candidates"]:
        assert c["product_match"] == "NO_MATCH"
        assert c["readiness"] == "REJECTED"

    # Probar PIVOT
    pivot_dec = LoopDecision(
        action=LoopAction.PIVOT,
        parameters={"operation": "PIVOT", "pivot_to_source": "SECONDARY_SOURCE", "new_query": "SSD Kingston"},
        reason="No matching suppliers found on primary source",
    )
    pivot_res = executor.execute(pivot_dec, state)
    assert pivot_res["status"] == "SUCCESS"
    assert pivot_res["operation"] == "PIVOT"
