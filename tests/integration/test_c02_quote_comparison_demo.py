"""
Marcha Blanca y Test E2E para la Misión C-02: Quote Comparison & MOQ Intelligence.
Flujo E2E:
OPPORTUNITY (Hito B)
  -> SUPPLIER CANDIDATES (C-01)
  -> QUOTES (C-02 CommercialQuote Modeling)
  -> NORMALIZATION (Unit price, Currency, MOQ, Price Tiers, Shipping, Lead Time)
  -> COMPARABILITY & CONFLICT CHECK (Moneda, Tiers, Freshness, Provenance)
  -> SCENARIO EVALUATION (QTY=1, QTY=MOQ, Volume Tiers)
  -> DETERMINISTIC PRELIMINARY COMMERCIAL RANKING
  -> BEST COMMERCIAL CANDIDATE
"""

import sys
import json
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

from src.domain.mission.models import LoopDecision, LoopAction, LoopState, MissionStatus
from src.domain.mission.ports import DecisionProvider
from src.domain.market_intelligence.models import (
    MarketEvidence,
    MarketListing,
    Marketplace,
    Money,
    Confidence,
)
from src.domain.opportunity.models import (
    Opportunity,
    EvidenceSufficiency,
    OpportunityReadiness,
)
from src.domain.supplier_intelligence.models import (
    SupplierCandidate,
    QuoteFreshness,
    QuoteComparabilityStatus,
    EvidenceProvenanceType,
)
from src.infrastructure.suppliers.directory_supplier_source import DirectorySupplierSource
from src.application.supplier_intelligence.supplier_discovery_action_executor import (
    SupplierDiscoveryActionExecutor,
)
from src.domain.supplier_intelligence.services import QuoteNormalizer, QuoteComparator


class AutonomousC02DecisionProvider(DecisionProvider):
    """
    DecisionProvider determinista para la Misión C-02.
    Secuencia:
    1. DISCOVER proveedores en catálogos estructurados.
    2. COMPARE cotizaciones normalizadas, MOQ, Price Tiers y evaluación por escenarios de volumen.
    3. COMPLETE cuando se obtiene el ranking comercial preliminar y el Best Commercial Candidate.
    """
    def __init__(self):
        self._step = 0

    def decide(self, state: LoopState) -> LoopDecision:
        self._step += 1
        if self._step == 1:
            return LoopDecision(
                action=LoopAction.CONTINUE,
                target=state.current_target,
                parameters={"operation": "DISCOVER"},
                reason="Descubrir proveedores para el producto objetivo en fuentes disponibles",
                confidence=0.9,
            )
        elif self._step == 2:
            return LoopDecision(
                action=LoopAction.CONTINUE,
                target=state.current_target,
                parameters={
                    "operation": "COMPARE",
                    "target_market_price": Decimal("38990"),
                    "analysis_quantities": [1, 5, 10, 50, 100],
                },
                reason="Ejecutar comparativa comercial profunda, MOQ intelligence, price tiers y ranking",
                confidence=0.95,
            )
        else:
            return LoopDecision(
                action=LoopAction.COMPLETE,
                reason="Misión C-02 completada: Comparación de cotizaciones y evaluación de MOQ validadas",
                confidence=0.98,
            )


def run_marcha_blanca_c02():
    print("=" * 80)
    print("AI AUTONOMOUS COMMERCE - HITO C (SUPPLIER INTELLIGENCE)")
    print("MARCHA BLANCA: MISIÓN C-02 — QUOTE COMPARISON & MOQ INTELLIGENCE")
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
    print(f"PVP de Mercado (Mercado Libre): {opportunity.listing.price.amount} {opportunity.listing.price.currency}")

    # 2. Configurar fuentes y executor
    repo_root = Path(__file__).resolve().parents[2]
    data_dir = repo_root / "data" / "suppliers"
    supplier_source = DirectorySupplierSource(directory_path=data_dir)

    executor = SupplierDiscoveryActionExecutor(
        sources=[supplier_source],
        target_opportunity=opportunity,
    )

    state = LoopState(
        mission_id="MISSION-C02-E2E",
        iteration=1,
        goal="Comparar cotizaciones y evaluar condiciones comerciales/MOQ",
        current_target=opportunity.title,
    )

    # 3. Descubrir candidatos
    print("\n--- PASO 1: DESCUBRIMIENTO Y RECOPILACIÓN DE EVIDENCIA COMERCIAL ---")
    disc_res = executor.execute(
        LoopDecision(action=LoopAction.CONTINUE, target=opportunity.title, reason="Descubrir candidatos"),
        state,
    )
    print(f"Candidatos encontrados en catálogo: {disc_res['raw_candidates_count']}")
    print(f"Candidatos deduplicados: {disc_res['deduplicated_count']}")

    # 4. Comparación Comercial Profunda
    print("\n--- PASO 2: QUOTE NORMALIZATION, MOQ & MULTI-TIER PRICING EVALUATION ---")
    comp_res = executor.execute(
        LoopDecision(
            action=LoopAction.CONTINUE,
            target=opportunity.title,
            parameters={
                "operation": "COMPARE",
                "target_market_price": Decimal("38990"),
                "analysis_quantities": [1, 5, 10, 50, 100],
            },
            reason="Comparar cotizaciones y rankings comerciales",
        ),
        state,
    )

    print("\n" + "=" * 80)
    print("SUPPLIER COMPARISON (REPORTE DETALLADO POR PROVEEDOR)")
    print("=" * 80)
    for item in comp_res["ranked_items"]:
        print(f"\nSupplier: {item['supplier_name']} ({item['supplier_id']}) [Rank #{item['rank']}]")
        print(f"  - Quote ID: {item['quote_id']}")
        print(f"  - Unit Price: {item['unit_price']} {item['currency'] if item['unit_price'] else 'UNKNOWN'}")
        print(f"  - MOQ: {item['moq']} ({item['moq_type']})")
        print(f"  - Shipping Cost: {item['shipping_cost']} {item['currency'] if item['shipping_cost'] else 'UNKNOWN'}")
        print(f"  - Lead Time: {item['lead_time_days']} días" if item['lead_time_days'] else "  - Lead Time: UNKNOWN")
        print(f"  - Stock Availability: {'Disponible' if item['stock_available'] else ('Agotado' if item['stock_available'] is False else 'UNKNOWN')}")
        print(f"  - Commercial Score: {item['commercial_score']:.1f}/100.0")
        print(f"  - Comparability: {item['comparability']}")
        print(f"  - Freshness: {item['freshness']}")
        print(f"  - Provenance: {item['provenance']}")
        print(f"  - Knowns: {', '.join(item['knowns']) if item['knowns'] else 'Ninguno'}")
        print(f"  - Unknowns: {', '.join(item['unknowns']) if item['unknowns'] else 'Ninguno'}")
        print(f"  - Advantages: {', '.join(item['advantages']) if item['advantages'] else 'Ninguna'}")
        print(f"  - Risks: {', '.join(item['risks']) if item['risks'] else 'Ninguno'}")
        print("  - Evaluaciones por escenario de volumen:")
        for sc in item["scenarios"]:
            moq_status = "MOQ OK" if sc["is_moq_satisfied"] else "BAJO MOQ"
            print(f"      * Qty {sc['qty']:>3} uds -> Unit Price: {str(sc['unit_price']):>7} {item['currency']} | Subtotal: {str(sc['total_goods']):>9} {item['currency']} ({moq_status})")

    print("\n" + "=" * 80)
    print("PRELIMINARY COMMERCIAL RANKING")
    print("=" * 80)
    for item in comp_res["ranked_items"]:
        print(f"{item['rank']}. {item['supplier_name']} ({item['supplier_id']}) — Score: {item['commercial_score']:.1f}/100 | Moneda: {item['currency']} | Unit Price: {item['unit_price']}")

    print("\n" + "=" * 80)
    print("BEST COMMERCIAL CANDIDATE")
    print("=" * 80)
    best = comp_res["best_commercial_candidate"]
    if best:
        print(f"Candidato Elegido: {best['supplier_name']} ({best['supplier_id']})")
        print(f"Cotización: {best['quote_id']} | Precio Unitario: {best['unit_price']} {best['currency']}")
        print(f"MOQ: {best['moq']} unidades | Lead Time: {best['lead_time_days']} días | Shipping: {best['shipping_cost']} {best['currency']}")
        print(f"Score Comercial: {best['commercial_score']:.1f}/100.0 | Provenance: {best['provenance_type']} | Freshness: {best['freshness']}")
        print(f"\nWHY:")
        print(f"{best['why_best']}")
        print(f"\nUNKNOWN INFORMATION:")
        print(f"{', '.join(best['remaining_unknowns']) if best['remaining_unknowns'] else 'Sin incógnitas críticas pendientes.'}")
    else:
        print("No se determinó un Best Commercial Candidate válido.")

    print("\nLIMITATIONS:")
    print("- Comparación preliminar sujeta a confirmación en orden de compra.")
    print("- No constituye recomendación definitiva de adquisición (Gate D / Profit Engine pendiente).")
    print("- Las cotizaciones de divisas extranjeras sin tipo de cambio verificado se aíslan como NOT_COMPARABLE.")
    print("=" * 80)

    return comp_res


def test_marcha_blanca_c02_execution():
    result = run_marcha_blanca_c02()
    assert result["status"] == "SUCCESS"
    assert result["candidates_compared"] >= 3
    assert result["best_commercial_candidate"] is not None
    assert result["best_commercial_candidate"]["supplier_id"] == "SUP-002"


if __name__ == "__main__":
    run_marcha_blanca_c02()
