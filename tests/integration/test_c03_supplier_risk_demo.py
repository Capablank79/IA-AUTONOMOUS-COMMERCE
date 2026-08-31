"""
Marcha Blanca y Test E2E para la Misión C-03: Supplier Risk, Reliability & Performance.

Flujo E2E:
OPPORTUNITY (Hito B)
  -> SUPPLIERS DISCOVERED (C-01)
  -> COMMERCIAL QUOTES & MOQ (C-02)
  -> LEAD TIME INTELLIGENCE & VARIABILITY (C.8)
  -> SHIPPING INTELLIGENCE & COMPARABILITY (C.9)
  -> SUPPLIER RELIABILITY & SLA COMPLIANCE (C.10)
  -> MULTIDIMENSIONAL SUPPLIER RISK ENGINE (C.11)
  -> HISTORICAL PERFORMANCE & TRENDS (C.12)
  -> COMPARISON & COMPOSITE SUITABILITY SCORING
  -> BEST SUPPLIER CANDIDATE PRELIMINAR (NO FINAL RECOMMENDATION)
"""

import sys
import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.domain.mission.models import LoopDecision, LoopAction, LoopState, MissionStatus
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
    SupplierRejectionReason,
)
from src.infrastructure.suppliers.directory_supplier_source import DirectorySupplierSource
from src.application.supplier_intelligence.supplier_discovery_action_executor import (
    SupplierDiscoveryActionExecutor,
)
from src.domain.supplier_intelligence.services import (
    LeadTimeAnalyzer,
    ShippingAnalyzer,
    HistoricalPerformanceAnalyzer,
    ReliabilityEvaluator,
    SupplierRiskEngine,
    SupplierRiskComparator,
)


def run_marcha_blanca_c03():
    print("=" * 80)
    print("AI AUTONOMOUS COMMERCE - HITO C (SUPPLIER INTELLIGENCE)")
    print("MARCHA BLANCA: MISIÓN C-03 — SUPPLIER RISK, RELIABILITY & PERFORMANCE")
    print("=" * 80)

    # 1. Oportunidad de entrada
    listing = MarketListing(
        external_id="MLC987654321",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Disco Solido Kingston A400 480GB SSD SATA 3 2.5",
        price=Money(amount=Decimal("38990"), currency="CLP"),
        sold_quantity=240,
        available_quantity=85,
        seller_id="TOP-SELLER-CHILE",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Hard Drives",
    )
    evidence = MarketEvidence(
        listing=listing,
        confidence=Confidence.HIGH,
    )
    opportunity = Opportunity(
        opportunity_id="OPP-HITO-B-WINNER",
        product_id="PROD-KINGSTON-480",
        title="Disco Solido Kingston A400 480GB SSD SATA 3 2.5",
        listing=listing,
        evidence=evidence,
        score=Decimal("88.5"),
        confidence=Confidence.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        readiness=OpportunityReadiness.READY,
        provenance={"brand": "Kingston", "model": "A400", "sku": "SA400S37/480G"},
    )

    print(f"\n[PRODUCTO OBJETIVO]")
    print(f"Título: {opportunity.title}")
    print(f"SKU Referencia: {opportunity.provenance.get('sku')}")
    print(f"PVP de Mercado: {opportunity.listing.price.amount} {opportunity.listing.price.currency}")

    # 2. Configurar fuentes y executor
    repo_root = Path(__file__).resolve().parents[2]
    data_dir = repo_root / "data" / "suppliers"
    supplier_source = DirectorySupplierSource(directory_path=data_dir)

    executor = SupplierDiscoveryActionExecutor(
        sources=[supplier_source],
        target_opportunity=opportunity,
    )

    state = LoopState(
        mission_id="MISSION-C03-E2E",
        iteration=1,
        goal="Evaluar riesgo, confiabilidad logística y desempeño histórico",
        current_target=opportunity.title,
    )

    # 3. Paso 1: Discover
    print("\n--- PASO 1: DESCUBRIMIENTO DE CANDIDATOS ---")
    disc_res = executor.execute(
        LoopDecision(action=LoopAction.CONTINUE, target=opportunity.title, reason="Descubrir candidatos"),
        state,
    )
    print(f"Candidatos encontrados en catálogo: {disc_res['raw_candidates_count']}")
    print(f"Candidatos deduplicados: {disc_res['deduplicated_count']}")

    # 4. Historial enriquecido determinista de observaciones para testing E2E
    now = datetime.now(timezone.utc)
    supplier_histories = {
        "SUP-INGRAM-01": [
            SupplierObservationEvent("EV-1", "SUP-INGRAM-01", "lead_time_days", 2, timestamp=now - timedelta(days=20)),
            SupplierObservationEvent("EV-2", "SUP-INGRAM-01", "lead_time_days", 2, timestamp=now - timedelta(days=10)),
            SupplierObservationEvent("EV-3", "SUP-INGRAM-01", "lead_time_days", 2, timestamp=now - timedelta(days=2)),
            SupplierObservationEvent("EV-4", "SUP-INGRAM-01", "sla_on_time_delivery", 1.0, timestamp=now - timedelta(days=10)),
        ],
        "SUP-INTCOMEX-02": [
            SupplierObservationEvent("EV-5", "SUP-INTCOMEX-02", "lead_time_days", 3, timestamp=now - timedelta(days=30)),
            SupplierObservationEvent("EV-6", "SUP-INTCOMEX-02", "lead_time_days", 3, timestamp=now - timedelta(days=15)),
            SupplierObservationEvent("EV-7", "SUP-INTCOMEX-02", "sla_on_time_delivery", 1.0, timestamp=now - timedelta(days=15)),
        ],
        "SUP-NEXSYS-03": [
            SupplierObservationEvent("EV-8", "SUP-NEXSYS-03", "incident", "delayed_order_10d", timestamp=now - timedelta(days=40)),
            SupplierObservationEvent("EV-9", "SUP-NEXSYS-03", "incident", "billing_discrepancy", timestamp=now - timedelta(days=20)),
            SupplierObservationEvent("EV-10", "SUP-NEXSYS-03", "sla_on_time_delivery", 0.0, timestamp=now - timedelta(days=20)),
        ]
    }

    # 5. Paso 2: Compare Risk & Reliability
    print("\n--- PASO 2: EVALUACIÓN DE RIESGO, CONFIABILIDAD, SLA Y LOGÍSTICA ---")
    risk_res = executor.execute(
        LoopDecision(
            action=LoopAction.CONTINUE,
            target=opportunity.title,
            parameters={
                "operation": "COMPARE_RISK",
                "target_market_price": Decimal("38990"),
                "supplier_histories": supplier_histories,
            },
            reason="Evaluar riesgos multidimensionales, confiabilidad y flete",
        ),
        state,
    )

    print("\n" + "=" * 80)
    print("SUPPLIER RISK & RELIABILITY ANALYSIS")
    print("=" * 80)

    for item in risk_res["items"]:
        print(f"\nSupplier: {item['supplier_name']} ({item['supplier_id']}) [Rank #{item['rank']}]")
        print(f"  - Commercial Score: {item['commercial_score']:.1f}/100.0" if item['commercial_score'] is not None else "  - Commercial Score: UNKNOWN")
        print(f"  - Lead Time: {item['lead_time_days']} días" if item['lead_time_days'] is not None else "  - Lead Time: UNKNOWN")
        print(f"  - Lead Time Variability (Variance): {item['lead_time_variance']:.2f}" if item['lead_time_variance'] is not None else "  - Lead Time Variability: UNKNOWN (Sin varianza histórica)")
        print(f"  - Shipping Cost: {item['shipping_cost']} CLP ({item['shipping_method']})" if item['shipping_cost'] is not None else "  - Shipping: UNKNOWN")
        print(f"  - Free Shipping Observed: {item['shipping_is_free']}")
        print(f"  - SLA Compliance Rate: {item['sla_compliance_rate']:.1%}" if item['sla_compliance_rate'] is not None else "  - SLA Compliance Rate: UNKNOWN")
        print(f"  - Reliability Score: {item['reliability_score']:.1f}/100.0" if item['reliability_score'] is not None else "  - Reliability Score: UNKNOWN")
        print(f"  - Historical Trend: {item['historical_trend']}")
        print(f"  - Overall Risk Score: {item['risk_score']:.1f}/100.0 (Nivel: {item['risk_level']})")
        print(f"  - Composite Suitability Score: {item['composite_suitability_score']:.1f}/100.0")
        print(f"  - Reject Recommended: {item['is_reject_recommended']}")
        if item['rejection_reasons']:
            print(f"  - Rejection Reasons: {', '.join(item['rejection_reasons'])}")
        print(f"  - Unknowns: {', '.join(item['unknowns']) if item['unknowns'] else 'Ninguno'}")

    print("\n" + "=" * 80)
    print("SUPPLIER COMPARISON")
    print("=" * 80)
    for item in risk_res["items"]:
        print(f"{item['rank']}. {item['supplier_name']} ({item['supplier_id']}) — Suitability: {item['composite_suitability_score']:.1f} | Commercial: {item['commercial_score']:.1f} | Reliability: {item['reliability_score'] if item['reliability_score'] is not None else 'UNKNOWN'} | Risk: {item['risk_score']:.1f} ({item['risk_level']})")

    print("\n" + "=" * 80)
    print("BEST SUPPLIER CANDIDATE (PRELIMINAR C-03)")
    print("=" * 80)
    best = risk_res["best_supplier_candidate"]
    if best:
        print(f"Candidato Seleccionado: {best['supplier_name']} ({best['supplier_id']})")
        print(f"Score Compuesto de Idoneidad: {best['composite_suitability_score']:.1f}/100.0")
        print(f"Score Comercial: {best['commercial_score']:.1f} | Confiabilidad: {best['reliability_score']:.1f} | Riesgo Global: {best['overall_risk_score']:.1f}")
        print(f"Confianza: {best['confidence']} | Procedencia: {best['provenance_type']}")
        print(f"\nWHY:")
        print(f"{best['why_best']}")
        print(f"\nKEY STRENGTHS:")
        for s in best["key_strengths"]:
            print(f"  * {s}")
        if best["identified_risks"]:
            print(f"\nIDENTIFIED RISKS:")
            for r in best["identified_risks"]:
                print(f"  * {r}")
        print(f"\nUNKNOWN INFORMATION:")
        print(f"{', '.join(best['remaining_unknowns']) if best['remaining_unknowns'] else 'Sin incertidumbres críticas pendientes.'}")
    else:
        print("No se determinó un Best Supplier Candidate válido.")

    print("\n" + "=" * 80)
    print("REJECTED / HIGH-RISK SUPPLIERS")
    print("=" * 80)
    if risk_res["rejected_suppliers"]:
        for rj in risk_res["rejected_suppliers"]:
            print(f"- Proveedor: {rj['supplier_id']} (Nivel de Riesgo: {rj['risk_level']})")
            print(f"  Motivos: {', '.join(rj['reasons'])}")
            for exp in rj["explanation"]:
                print(f"    * {exp}")
    else:
        print("Ningún proveedor fue rechazado por riesgo crítico en este ciclo.")

    print("\n" + "=" * 80)
    print("UNKNOWN INFORMATION")
    print("=" * 80)
    print(f"Dimensiones no comparables o con incertidumbre: {', '.join(risk_res['unknown_dimensions']) if risk_res['unknown_dimensions'] else 'Ninguna detectada a nivel de lote.'}")

    print("\n" + "=" * 80)
    print("LIMITATIONS")
    print("=" * 80)
    print("- Evaluación preliminar de riesgo y confiabilidad logística para filtrar opciones inviables.")
    print("- NO constituye la recomendación definitiva de adquisición ni decisión de compra (reservado para C-04).")
    print("- Los scores de confiabilidad e historial son deterministas y basados estrictamente en evidencias observadas (UNKNOWN ≠ 0).")
    print("=" * 80)

    return risk_res


def test_marcha_blanca_c03_execution():
    result = run_marcha_blanca_c03()
    assert result["status"] == "SUCCESS"
    assert result["evaluated_candidates_count"] >= 3
    assert result["best_supplier_candidate"] is not None
