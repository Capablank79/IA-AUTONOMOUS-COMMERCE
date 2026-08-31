"""
Marcha Blanca y Test E2E para la Misión C-04: Supplier Recommendation & Gate C Completion.

Flujo E2E Completo:
OPPORTUNITY (Hito B)
  -> SUPPLIER DISCOVERY (C-01)
  -> COMMERCIAL QUOTE COMPARISON (C-02)
  -> RISK, RELIABILITY & PERFORMANCE (C-03)
  -> SUPPLIER RECOMMENDATION (C-04 / C.13)
  -> CONTINGENCY EVALUATION & FALLBACK PIVOT

Escenarios Validados:
- ESCENARIO A: Recomendación Incondicional (RECOMMEND)
- ESCENARIO B: Recomendación con Condiciones (RECOMMEND_WITH_CONDITIONS)
- ESCENARIO C: Sin Recomendación / Rechazo (NO_RECOMMENDATION / REJECT)
- FALLBACK E2E: Quiebre de stock del primario -> Reevaluación -> Pivot a Fallback
"""

import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

from src.domain.market_intelligence.models import (
    MarketEvidence,
    MarketListing,
    Marketplace,
    Money,
    Confidence,
    SignalType,
)
from src.domain.opportunity.models import (
    Opportunity,
    EvidenceSufficiency,
    OpportunityReadiness,
)
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierEvidence,
    SupplierCandidate,
    ProductMatch,
    ProductMatchGrade,
    SupplierStatus,
    SupplierReadiness,
    EvidenceProvenanceType,
    SupplierLocation,
    ShippingMethod,
    ShippingOption,
    ShippingComparabilityStatus,
    SLAStatus,
    SLARecord,
    SupplierObservationEvent,
    HistoricalPerformanceProfile,
    PerformanceTrend,
    ReliabilityEvaluation,
    RiskLevel,
    SupplierRiskDimension,
    SupplierRiskProfile,
    SupplierRiskComparisonItem,
    BestSupplierCandidate,
    SupplierRiskEvaluationResult,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    PrimarySupplierSelection,
    FallbackSupplierSelection,
    RecommendationCondition,
    ContingencyTrigger,
    QuoteFreshness,
)
from src.infrastructure.suppliers.directory_supplier_source import DirectorySupplierSource
from src.application.supplier_intelligence.supplier_discovery_action_executor import (
    SupplierDiscoveryActionExecutor,
)
from src.domain.supplier_intelligence.services import (
    SupplierRiskComparator,
    SupplierRecommendationEngine,
    SupplierRecommendationPolicy,
)
from src.domain.mission.models import LoopState, LoopAction, LoopDecision, MissionStatus


def build_candidate(
    supplier_id: str,
    name: str,
    wholesale_price: Decimal,
    moq: int = 5,
    lead_time_days: int = 2,
    shipping_cost: Decimal = Decimal("2500"),
    stock_available: bool = True,
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.LIVE,
    raw_payload: Dict[str, Any] = None,
) -> SupplierCandidate:
    sup = Supplier(
        supplier_id=supplier_id,
        name=name,
        source="CATALOG",
        source_type=provenance_type,
        status=SupplierStatus.VERIFIED,
        location=SupplierLocation(country="Chile", city="Santiago"),
    )
    ev = SupplierEvidence(
        supplier_id=supplier_id,
        sku="SKU-KINGSTON-480",
        wholesale_price=wholesale_price,
        currency="CLP",
        minimum_order_quantity=moq,
        stock_available=stock_available,
        shipping_cost=shipping_cost,
        lead_time_days=lead_time_days,
        confidence=Confidence.HIGH,
        signal_type=SignalType.OBSERVED,
        provenance_type=provenance_type,
        source="LIVE_SUPPLIER_API" if provenance_type == EvidenceProvenanceType.LIVE else "FIXTURE_CATALOG",
        raw_payload=raw_payload or {},
    )
    pm = ProductMatch(
        grade=ProductMatchGrade.EXACT_MATCH,
        confidence=Confidence.HIGH,
        matched_fields=("sku", "title", "brand"),
    )
    return SupplierCandidate(
        supplier=sup,
        evidence=ev,
        product_match=pm,
        readiness=SupplierReadiness.READY_FOR_ECONOMICS,
    )


def test_marcha_blanca_c04_e2e():
    print("=" * 80)
    print("AI AUTONOMOUS COMMERCE - HITO C (SUPPLIER INTELLIGENCE)")
    print("MARCHA BLANCA & VALIDACIÓN E2E: MISIÓN C-04 — SUPPLIER RECOMMENDATION")
    print("=" * 80)

    # =========================================================================
    # ESCENARIO A: RECOMENDACIÓN INCONDICIONAL (RECOMMEND)
    # =========================================================================
    print("\n>>> ESCENARIO A: CANDIDATO ÓPTIMO CON EVIDENCIA COMPLETA Y VERIFICADA")
    cand_alpha = build_candidate("SUP-01", "Mayorista Alpha SpA", wholesale_price=Decimal("19500"), lead_time_days=2, shipping_cost=Decimal("2000"), provenance_type=EvidenceProvenanceType.LIVE)
    cand_beta = build_candidate("SUP-02", "Distribuidora Beta Ltda", wholesale_price=Decimal("22000"), lead_time_days=4, shipping_cost=Decimal("2500"), provenance_type=EvidenceProvenanceType.LIVE)

    histories_a = {
        "SUP-01": [
            SupplierObservationEvent(event_id="EV-1", supplier_id="SUP-01", metric="lead_time_days", observed_value=2),
            SupplierObservationEvent(event_id="EV-2", supplier_id="SUP-01", metric="fulfillment", observed_value=1.0),
        ],
        "SUP-02": [
            SupplierObservationEvent(event_id="EV-3", supplier_id="SUP-02", metric="lead_time_days", observed_value=4),
            SupplierObservationEvent(event_id="EV-4", supplier_id="SUP-02", metric="fulfillment", observed_value=0.98),
        ],
    }

    risk_res_a = SupplierRiskComparator.evaluate_and_compare([cand_alpha, cand_beta], target_product_title="Disco Kingston 480GB", supplier_histories=histories_a)
    rec_a = SupplierRecommendationEngine.generate_recommendation(risk_res_a, opportunity_id="OPP-A")

    print(f"Decisión: {rec_a.decision.value}")
    print(f"Proveedor Primario: {rec_a.primary_supplier.supplier_name} (ID: {rec_a.primary_supplier.supplier_id})")
    print(f"Proveedor Fallback: {rec_a.fallback_supplier.supplier_name} (ID: {rec_a.fallback_supplier.supplier_id})")
    print(f"Confianza: {rec_a.confidence.value} | Procedencia: {rec_a.provenance.value}")
    print(f"Por qué sobre Fallback: {rec_a.primary_supplier.why_over_fallback}")
    print(f"Condiciones: {len(rec_a.conditions)}")

    assert rec_a.decision == SupplierRecommendationDecision.RECOMMEND
    assert rec_a.primary_supplier.supplier_id == "SUP-01"
    assert rec_a.fallback_supplier.supplier_id == "SUP-02"
    assert len(rec_a.conditions) == 0

    # =========================================================================
    # ESCENARIO B: RECOMENDACIÓN CON CONDICIONES (RECOMMEND_WITH_CONDITIONS)
    # =========================================================================
    print("\n>>> ESCENARIO B: CANDIDATO FUERTE PERO CON COSTO DE ENVÍO PENDIENTE DE COTIZAR")
    cand_cond = build_candidate("SUP-03", "Logistics Gamma", wholesale_price=Decimal("18900"), lead_time_days=2, shipping_cost=None, provenance_type=EvidenceProvenanceType.LIVE)
    risk_res_b = SupplierRiskComparator.evaluate_and_compare([cand_cond], target_product_title="Disco Kingston 480GB")
    rec_b = SupplierRecommendationEngine.generate_recommendation(risk_res_b, opportunity_id="OPP-B")

    print(f"Decisión: {rec_b.decision.value}")
    print(f"Proveedor Primario: {rec_b.primary_supplier.supplier_name}")
    print(f"Condiciones Requeridas: {[c.code for c in rec_b.conditions]}")
    for c in rec_b.conditions:
        print(f"  - [{c.code}] {c.description} -> Acción: {c.suggested_action}")

    assert rec_b.decision == SupplierRecommendationDecision.RECOMMEND_WITH_CONDITIONS
    assert any(c.code == "VERIFY_SHIPPING_COST" for c in rec_b.conditions)

    # =========================================================================
    # ESCENARIO C: NO RECOMENDACIÓN / RECHAZO (NO_RECOMMENDATION)
    # =========================================================================
    print("\n>>> ESCENARIO C: PROVEEDORES SIN STOCK O CON RIESGO CRÍTICO")
    cand_bad1 = build_candidate("SUP-04", "Out of Stock S.A.", wholesale_price=Decimal("17000"), stock_available=False)
    cand_bad2 = build_candidate("SUP-05", "No Price S.A.", wholesale_price=None, stock_available=True)

    risk_res_c = SupplierRiskComparator.evaluate_and_compare([cand_bad1, cand_bad2], target_product_title="Disco Kingston 480GB")
    rec_c = SupplierRecommendationEngine.generate_recommendation(risk_res_c, opportunity_id="OPP-C")

    print(f"Decisión: {rec_c.decision.value}")
    print(f"Razón: {rec_c.decision_reason}")
    print(f"Primario: {rec_c.primary_supplier}")

    assert rec_c.decision in [SupplierRecommendationDecision.NO_RECOMMENDATION, SupplierRecommendationDecision.REJECT, SupplierRecommendationDecision.NEEDS_INVESTIGATION]

    # =========================================================================
    # FALLBACK E2E: CONTINGENCIA Y PIVOT AUTOMÁTICO
    # =========================================================================
    print("\n>>> FALLBACK E2E: QUIEBRE DE STOCK DE PRIMARIO -> ACTIVACIÓN DETERMINISTA DE FALLBACK")
    print(f"Estado Inicial: Primario={rec_a.primary_supplier.supplier_name}, Fallback={rec_a.fallback_supplier.supplier_name}")
    
    new_rec, pivoted = SupplierRecommendationEngine.reevaluate_and_pivot_fallback(
        recommendation=rec_a,
        trigger=ContingencyTrigger.STOCK_UNAVAILABLE,
        trigger_details="Physical warehouse inventory dropped to 0 units",
    )

    print(f"Pivoted: {pivoted}")
    print(f"Nuevo Proveedor Primario: {new_rec.primary_supplier.supplier_name} (ID: {new_rec.primary_supplier.supplier_id})")
    print(f"Razón de Selección tras Pivoteo: {new_rec.primary_supplier.selection_reason}")
    print(f"Nuevo Fallback: {new_rec.fallback_supplier}")

    assert pivoted is True
    assert new_rec.primary_supplier.supplier_id == "SUP-02"
    assert new_rec.fallback_supplier is None
    assert "STOCK_UNAVAILABLE" in new_rec.primary_supplier.selection_reason

    print("\n" + "=" * 80)
    print("MARCHA BLANCA C-04 & FALLBACK E2E COMPLETADA CON ÉXITO (100% PASS)")
    print("=" * 80)


if __name__ == "__main__":
    test_marcha_blanca_c04_e2e()
