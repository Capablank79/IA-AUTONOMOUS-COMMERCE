import pytest
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, List

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierEvidence,
    SupplierCandidate,
    ProductMatch,
    ProductMatchGrade,
    SupplierStatus,
    SupplierReadiness,
    EvidenceProvenanceType,
    SupplierObservationEvent,
)
from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.ports import SupplierSource
from src.application.supplier_intelligence.supplier_discovery_action_executor import (
    SupplierDiscoveryActionExecutor,
)


class MockSupplierSource(SupplierSource):
    @property
    def source_name(self) -> str:
        return "MOCK_SOURCE"

    def search_suppliers(
        self,
        query: str,
        brand: str = None,
        model: str = None,
        sku: str = None,
        limit: int = 10,
    ) -> List[SupplierCandidate]:
        candidates = []
        for i in range(1, 4):
            sup = Supplier(
                supplier_id=f"SUP-0{i}",
                name=f"Supplier Test {i}",
                source=self.source_name,
                source_type=EvidenceProvenanceType.FIXTURE,
                status=SupplierStatus.VERIFIED,
            )
            ev = SupplierEvidence(
                supplier_id=f"SUP-0{i}",
                sku=f"SKU-0{i}",
                wholesale_price=Decimal(str(10000 * i)),
                currency="CLP",
                minimum_order_quantity=5,
                stock_available=True if i != 3 else False,
                shipping_cost=Decimal("2500"),
                lead_time_days=2 * i,
                confidence=Confidence.HIGH,
                signal_type=SignalType.OBSERVED,
                provenance_type=EvidenceProvenanceType.FIXTURE,
                source=self.source_name,
            )
            pm = ProductMatch(
                grade=ProductMatchGrade.EXACT_MATCH,
                confidence=Confidence.HIGH,
                matched_fields=("title",),
                discrepancies=(),
                details=f"{query} Variant {i}",
            )
            candidates.append(
                SupplierCandidate(
                    supplier=sup,
                    evidence=ev,
                    product_match=pm,
                    readiness=SupplierReadiness.EVALUATED,
                )
            )
        return candidates


def test_action_executor_compare_risk_flow():
    source = MockSupplierSource()
    executor = SupplierDiscoveryActionExecutor(sources=[source])

    # 1. Ejecutar DISCOVER
    state_0 = LoopState(
        mission_id="m-test",
        iteration=0,
        goal="Discover suppliers",
        current_target="Teclado Mecanico",
        observations=(),
        evidences=(),
        decision_history=(),
    )
    dec_disc = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Discovering candidates",
        parameters={"operation": "DISCOVER", "query": "Teclado Mecanico", "target_market_price": "25000"},
        confidence=0.9,
    )
    res_disc = executor.execute(dec_disc, state_0)
    assert res_disc["status"] == "SUCCESS"
    assert len(executor.get_all_candidates()) == 3

    # 2. Ejecutar COMPARE_RISK
    state_1 = LoopState(
        mission_id="m-test",
        iteration=1,
        goal="Assess risk and reliability",
        current_target="Teclado Mecanico",
        observations=(res_disc,),
        evidences=(),
        decision_history=(dec_disc,),
    )
    dec_risk = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Assessing supplier risks and reliability",
        parameters={"operation": "COMPARE_RISK", "target_market_price": "25000"},
        confidence=0.9,
    )
    res_risk = executor.execute(dec_risk, state_1)
    assert res_risk["status"] == "SUCCESS"
    assert res_risk["operation"] == "COMPARE_RISK"
    assert res_risk["evaluated_candidates_count"] == 3
    assert res_risk["best_supplier_candidate"] is not None

    # SUP-03 no tiene stock, debe figurar como recomendado a rechazo
    rejected_ids = [r["supplier_id"] for r in res_risk["rejected_suppliers"]]
    assert "SUP-03" in rejected_ids
