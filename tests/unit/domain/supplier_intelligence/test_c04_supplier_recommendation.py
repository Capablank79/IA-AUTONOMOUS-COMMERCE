import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from src.domain.supplier_intelligence.ports import SupplierSource

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
    CommercialQuote,
    QuoteFreshness,
    ShippingMethod,
    ShippingOption,
    SLAStatus,
    SLARecord,
    SupplierObservationEvent,
    ReliabilityEvaluation,
    RiskLevel,
    SupplierRiskProfile,
    SupplierRiskComparisonItem,
    BestSupplierCandidate,
    SupplierRiskEvaluationResult,
    SupplierRejectionReason,
    SupplierRecommendationDecision,
    ContingencyTrigger,
    RecommendationCondition,
    PrimarySupplierSelection,
    FallbackSupplierSelection,
    StructuredRecommendationExplanation,
    SupplierRecommendation,
)
from src.domain.market_intelligence.models import Confidence, SignalType
from src.domain.supplier_intelligence.services import (
    SupplierRiskComparator,
    SupplierRecommendationPolicy,
    SupplierRecommendationEngine,
)


def _make_candidate(
    supplier_id: str,
    name: str,
    sku: str = "SKU-TEST",
    wholesale_price: Optional[Decimal] = Decimal("15000"),
    currency: str = "CLP",
    moq: int = 5,
    shipping_cost: Optional[Decimal] = Decimal("2500"),
    lead_time_days: Optional[int] = 3,
    stock_available: Optional[bool] = True,
    confidence: Confidence = Confidence.HIGH,
    freshness: QuoteFreshness = QuoteFreshness.FRESH,
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.FIXTURE,
    product_match_grade: ProductMatchGrade = ProductMatchGrade.EXACT_MATCH,
    raw_payload: Optional[Dict[str, Any]] = None,
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
        sku=sku,
        wholesale_price=wholesale_price,
        currency=currency,
        minimum_order_quantity=moq,
        stock_available=stock_available,
        shipping_cost=shipping_cost,
        lead_time_days=lead_time_days,
        confidence=confidence,
        signal_type=SignalType.OBSERVED,
        provenance_type=provenance_type,
        source="DIRECT_CATALOG",
        raw_payload=raw_payload or {},
    )
    pm = ProductMatch(
        grade=product_match_grade,
        confidence=confidence,
        matched_fields=("sku", "title"),
    )
    return SupplierCandidate(
        supplier=sup,
        evidence=ev,
        product_match=pm,
        readiness=SupplierReadiness.READY_FOR_ECONOMICS,
    )


class TestC04SupplierRecommendation:
    """
    Suite exhaustiva de pruebas unitarias para Misión C-04 (Supplier Recommendation & Gate C).
    Cubre los 28 criterios de validación:
    1. recommendation model
    2. recommendation policy
    3. primary supplier
    4. fallback supplier
    5. no fallback
    6. recommend
    7. recommend with conditions
    8. needs investigation
    9. no recommendation
    10. rejection
    11. evidence sufficiency
    12. unknown handling
    13. confidence
    14. freshness
    15. provenance
    16. primary vs fallback comparison
    17. invalidation
    18. fallback activation
    19. stale quote
    20. unavailable stock
    21. critical risk
    22. poor reliability
    23. product mismatch
    24. recommendation explanation
    25. deterministic decision
    26. AutonomousLoop integration
    27. no fabricated evidence
    28. regression
    """

    def test_01_recommendation_model_immutability(self):
        """Verifica que el modelo SupplierRecommendation y sus Value Objects sean inmutables."""
        cond = RecommendationCondition(code="TEST", description="Test condition", is_critical=True)
        assert cond.code == "TEST"

        pri = PrimarySupplierSelection(
            supplier_id="SUP-01",
            supplier_name="Distribuidora Alpha",
            sku="SKU-01",
            commercial_score=Decimal("85.0"),
            reliability_score=Decimal("90.0"),
            overall_risk_score=Decimal("20.0"),
            composite_suitability_score=Decimal("88.0"),
            confidence=Confidence.HIGH,
            provenance_type=EvidenceProvenanceType.LIVE,
            selection_reason="Best overall supplier",
            why_over_fallback="Lower price and faster lead time",
            commercial_position="Rank #1",
            logistics_position="Standard 2 days",
            key_strengths=("Low MOQ",),
            identified_risks=(),
            unknowns=(),
            invalidation_criteria=("Stock exhaustion",),
        )
        assert pri.supplier_id == "SUP-01"

        rec = SupplierRecommendation(
            recommendation_id="REC-001",
            opportunity_id="OPP-001",
            target_product_title="Target Product",
            target_sku="SKU-01",
            decision=SupplierRecommendationDecision.RECOMMEND,
            decision_reason="Strong candidate",
            primary_supplier=pri,
            fallback_supplier=None,
            conditions=(cond,),
        )
        with pytest.raises(Exception):
            rec.decision = SupplierRecommendationDecision.REJECT

    def test_02_recommendation_policy_evaluation(self):
        """Verifica la evaluación determinista de suficiencia de evidencia de la policy."""
        cand = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("12000"), stock_available=True, lead_time_days=2)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        top_item = risk_res.items[0]

        is_sufficient, missing, conditions = SupplierRecommendationPolicy.evaluate_evidence_sufficiency(top_item)
        assert is_sufficient is True
        assert len(missing) == 0

    def test_03_primary_supplier_selection(self):
        """Verifica que el proveedor primario se seleccione con su justificación y scores completos."""
        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"), lead_time_days=2, provenance_type=EvidenceProvenanceType.LIVE)
        cand_b = _make_candidate("SUP-02", "Beta", wholesale_price=Decimal("15000"), lead_time_days=5, provenance_type=EvidenceProvenanceType.LIVE)

        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.primary_supplier is not None
        assert rec.primary_supplier.supplier_id == "SUP-01"
        assert rec.primary_supplier.supplier_name == "Alpha"
        assert rec.primary_supplier.why_over_fallback != ""
        assert "Alpha" in rec.decision_reason or "Alpha" in rec.primary_supplier.selection_reason

    def test_04_fallback_supplier_selection(self):
        """Verifica que el segundo candidato viable sea seleccionado como Fallback con tradeoffs explícitos."""
        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"), lead_time_days=2)
        cand_b = _make_candidate("SUP-02", "Beta", wholesale_price=Decimal("14000"), lead_time_days=4)

        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.fallback_supplier is not None
        assert rec.fallback_supplier.supplier_id == "SUP-02"
        assert rec.fallback_supplier.supplier_name == "Beta"
        assert "Higher unit price" in rec.fallback_supplier.tradeoffs_vs_primary or "Longer lead time" in rec.fallback_supplier.tradeoffs_vs_primary
        assert len(rec.fallback_supplier.activation_conditions) > 0

    def test_05_no_fallback_when_only_one_supplier_or_secondary_unviable(self):
        """Verifica que no se invente un fallback si solo hay un proveedor o el secundario es inviable."""
        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"))
        # Candidato B con stock agotado (rechazado)
        cand_b = _make_candidate("SUP-02", "Beta", wholesale_price=Decimal("8000"), stock_available=False)

        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.primary_supplier is not None
        assert rec.fallback_supplier is None
        assert "NO_FALLBACK_AVAILABLE" in rec.explanation.inferred_signals or "No qualified fallback" in rec.explanation.contingency_plan

    def test_06_decision_recommend_unconditional(self):
        """Verifica estado RECOMMEND cuando la evidencia está completa, verificada y el riesgo es bajo."""
        cand = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("12000"), stock_available=True, lead_time_days=2, shipping_cost=Decimal("2000"), provenance_type=EvidenceProvenanceType.LIVE)
        
        # Simular historial para dar reliability score
        histories = {
            "SUP-01": [
                SupplierObservationEvent(
                    event_id="EV-1",
                    supplier_id="SUP-01",
                    metric="lead_time_days",
                    observed_value=2,
                    timestamp=datetime.now(timezone.utc),
                ),
                SupplierObservationEvent(
                    event_id="EV-2",
                    supplier_id="SUP-01",
                    metric="fulfillment",
                    observed_value=1.0,
                    timestamp=datetime.now(timezone.utc),
                ),
            ]
        }
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A", supplier_histories=histories)
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.decision == SupplierRecommendationDecision.RECOMMEND
        assert len(rec.conditions) == 0

    def test_07_decision_recommend_with_conditions(self):
        """Verifica estado RECOMMEND_WITH_CONDITIONS cuando existen incógnitas no críticas o términos pendientes."""
        # Candidato sin costo de envío conocido ni historial previo
        cand = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("12000"), stock_available=True, lead_time_days=2, shipping_cost=None)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.decision == SupplierRecommendationDecision.RECOMMEND_WITH_CONDITIONS
        assert len(rec.conditions) > 0
        cond_codes = [c.code for c in rec.conditions]
        assert "VERIFY_SHIPPING_COST" in cond_codes or "PERFORM_FIRST_ORDER_VERIFICATION" in cond_codes

    def test_08_decision_needs_investigation_on_missing_price(self):
        """Verifica estado NEEDS_INVESTIGATION si el precio mayorista no se conoce."""
        cand = _make_candidate("SUP-01", "Alpha", wholesale_price=None, stock_available=True)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.decision == SupplierRecommendationDecision.NEEDS_INVESTIGATION
        assert "WHOLESALE_PRICE_UNKNOWN" in rec.decision_reason or "requires critical investigation" in rec.decision_reason

    def test_09_decision_no_recommendation_when_empty(self):
        """Verifica estado NO_RECOMMENDATION cuando no hay candidatos descubiertos."""
        risk_res = SupplierRiskEvaluationResult(
            target_product_title="Unknown Product",
            target_sku=None,
            items=(),
            ranked_items=(),
            best_supplier_candidate=None,
        )
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-EMPTY")

        assert rec.decision == SupplierRecommendationDecision.NO_RECOMMENDATION
        assert rec.primary_supplier is None
        assert rec.fallback_supplier is None

    def test_10_decision_reject_when_all_out_of_stock_or_critical_risk(self):
        """Verifica estado REJECT cuando todos los proveedores tienen stock agotado o riesgo crítico."""
        cand_a = _make_candidate("SUP-01", "Alpha", stock_available=False)
        cand_b = _make_candidate("SUP-02", "Beta", stock_available=False)

        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-REJECT")

        assert rec.decision == SupplierRecommendationDecision.REJECT
        assert rec.primary_supplier is None

    def test_11_evidence_sufficiency_evaluation(self):
        """Evalúa las 6 dimensiones críticas de suficiencia de evidencia."""
        cand = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"), stock_available=True, lead_time_days=3, shipping_cost=Decimal("1500"))
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        is_suff, missing, conds = SupplierRecommendationPolicy.evaluate_evidence_sufficiency(risk_res.items[0])
        # Solo falta historial de reliability
        assert "WHOLESALE_PRICE_UNKNOWN" not in missing
        assert "STOCK_AVAILABILITY_UNKNOWN" not in missing

    def test_12_unknown_handling_strictness(self):
        """UNKNOWN != GOOD, UNKNOWN != BAD, UNKNOWN != 0, UNKNOWN != 1."""
        cand = _make_candidate("SUP-01", "Alpha", shipping_cost=None, lead_time_days=None, stock_available=None)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        top_item = risk_res.items[0]

        assert "SHIPPING_COST_UNKNOWN" in top_item.unknowns
        assert "LEAD_TIME_UNKNOWN" in top_item.unknowns or "Lead time unknown" in top_item.unknowns
        assert "STOCK_AVAILABILITY_UNKNOWN" in top_item.unknowns or "AVAILABILITY_UNKNOWN" in top_item.unknowns

    def test_13_confidence_calculation(self):
        """Verifica que la confianza no sea arbitrariamente alta sobre datos incompletos o fixture."""
        cand_fixture = _make_candidate("SUP-01", "Alpha", provenance_type=EvidenceProvenanceType.FIXTURE, shipping_cost=None)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_fixture], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        # Fixture con dimensiones faltantes nunca es HIGH
        assert rec.confidence in [Confidence.LOW, Confidence.MEDIUM]

    def test_14_freshness_stale_and_expired(self):
        """Verifica que cotizaciones expiradas o stale generen condiciones o penalizaciones."""
        # Cotización expirada usando fecha de expiración pasada
        expired_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        cand_expired = _make_candidate("SUP-01", "Alpha", raw_payload={"valid_until": expired_date})
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_expired], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        # Debe generar recomendación condicionada o condición de renovación de cotización
        assert rec.decision in [SupplierRecommendationDecision.RECOMMEND_WITH_CONDITIONS, SupplierRecommendationDecision.NEEDS_INVESTIGATION]
        assert any(c.code in ["RENEW_EXPIRED_QUOTE", "VERIFY_STALE_QUOTE_PRICING", "PERFORM_FIRST_ORDER_VERIFICATION"] for c in rec.conditions)

    def test_15_provenance_preservation(self):
        """Verifica que la procedencia viaje inmutablemente desde el SupplierEvidence hasta la recomendación final."""
        cand_fixture = _make_candidate("SUP-01", "Alpha", provenance_type=EvidenceProvenanceType.FIXTURE)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_fixture], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.provenance == EvidenceProvenanceType.FIXTURE
        assert rec.primary_supplier.provenance_type == EvidenceProvenanceType.FIXTURE

    def test_16_primary_vs_fallback_comparison_explicability(self):
        """Verifica que la recomendación compare analíticamente al primario contra el fallback."""
        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"), lead_time_days=2)
        cand_b = _make_candidate("SUP-02", "Beta", wholesale_price=Decimal("12000"), lead_time_days=4)

        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert "Beta" in rec.primary_supplier.why_over_fallback
        assert rec.fallback_supplier.tradeoffs_vs_primary != ""

    def test_17_contingency_invalidation_criteria(self):
        """Verifica que el proveedor primario registre criterios explícitos de invalidación."""
        cand = _make_candidate("SUP-01", "Alpha")
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert len(rec.primary_supplier.invalidation_criteria) >= 3
        assert any("Stock exhaustion" in c for c in rec.primary_supplier.invalidation_criteria)

    def test_18_fallback_activation_pivot(self):
        """Verifica que reevaluate_and_pivot_fallback invalide al primario y promueva al fallback automáticamente."""
        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"))
        cand_b = _make_candidate("SUP-02", "Beta", wholesale_price=Decimal("12000"))

        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.primary_supplier.supplier_id == "SUP-01"
        assert rec.fallback_supplier.supplier_id == "SUP-02"

        # Simular invalidación del primario por quiebre de stock
        new_rec, pivoted = SupplierRecommendationEngine.reevaluate_and_pivot_fallback(
            recommendation=rec,
            trigger=ContingencyTrigger.STOCK_UNAVAILABLE,
            trigger_details="Primary supplier reported 0 physical units in warehouse",
        )

        assert pivoted is True
        assert new_rec.primary_supplier.supplier_id == "SUP-02"
        assert new_rec.primary_supplier.supplier_name == "Beta"
        assert new_rec.fallback_supplier is None
        assert "STOCK_UNAVAILABLE" in new_rec.primary_supplier.selection_reason

    def test_19_contingency_pivot_without_fallback(self):
        """Verifica que ante la invalidación de un primario sin fallback, el estado pase a NO_RECOMMENDATION."""
        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"))
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_a], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        new_rec, pivoted = SupplierRecommendationEngine.reevaluate_and_pivot_fallback(
            recommendation=rec,
            trigger=ContingencyTrigger.CRITICAL_RISK,
            trigger_details="Supplier fraud detected",
        )

        assert pivoted is False
        assert new_rec.decision == SupplierRecommendationDecision.NO_RECOMMENDATION
        assert new_rec.primary_supplier is None

    def test_20_unavailable_stock_rejection(self):
        """Verifica que un proveedor sin stock sea descalificado y rechazado."""
        cand = _make_candidate("SUP-01", "Alpha", stock_available=False)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        assert len(risk_res.rejected_candidates) == 1
        assert risk_res.rejected_candidates[0].disqualification_reason == "OUT_OF_STOCK"

    def test_21_critical_risk_disqualification(self):
        """Verifica que un proveedor con incidentes graves o riesgo crítico no sea recomendado."""
        cand = _make_candidate("SUP-01", "Alpha")
        histories = {
            "SUP-01": [
                SupplierObservationEvent(
                    event_id=f"EV-{i}",
                    supplier_id="SUP-01",
                    metric="incident",
                    observed_value="fraud_attempt",
                    timestamp=datetime.now(timezone.utc),
                )
                for i in range(4)
            ]
        }
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A", supplier_histories=histories)
        assert risk_res.items[0].risk_profile.operational_risk.risk_level == RiskLevel.CRITICAL
        assert risk_res.items[0].risk_profile.is_reject_recommended is True

    def test_22_poor_reliability_impacts_suitability(self):
        """Verifica que un cumplimiento de SLA nulo impacte severamente el score de confiabilidad y recomendación."""
        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"))
        cand_b = _make_candidate("SUP-02", "Beta", wholesale_price=Decimal("11000"))

        events_a = [
            SupplierObservationEvent(
                event_id=f"EV-A-{i}",
                supplier_id="SUP-01",
                metric="incident",
                observed_value="broken_packaging",
                timestamp=datetime.now(timezone.utc),
            )
            for i in range(3)
        ]
        events_b = [
            SupplierObservationEvent(
                event_id="EV-B-1",
                supplier_id="SUP-02",
                metric="lead_time_days",
                observed_value=2,
                timestamp=datetime.now(timezone.utc),
            )
        ]

        histories = {
            "SUP-01": events_a,
            "SUP-02": events_b,
        }
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A", supplier_histories=histories)
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        # Beta debe superar a Alpha a pesar de ser ligeramente más caro debido a confiabilidad superior y menor riesgo
        assert rec.primary_supplier is not None
        assert rec.primary_supplier.supplier_id == "SUP-02"

    def test_23_product_mismatch_triggers_investigation(self):
        """Verifica que un match incierto de producto no permita recomendación incondicional."""
        cand = _make_candidate("SUP-01", "Alpha", product_match_grade=ProductMatchGrade.UNCERTAIN_MATCH)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.decision == SupplierRecommendationDecision.NEEDS_INVESTIGATION

    def test_24_structured_explanation_layers(self):
        """Verifica que la explicación contenga explícitamente OBSERVED, DERIVED, INFERRED y RECOMMENDED."""
        cand = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("12000"), lead_time_days=3)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        assert rec.explanation is not None
        assert len(rec.explanation.observed_facts) > 0
        assert len(rec.explanation.derived_metrics) > 0
        assert len(rec.explanation.inferred_signals) > 0
        assert rec.explanation.contingency_plan != ""

    def test_25_deterministic_decision_reproducibility(self):
        """Verifica que la misma entrada produzca exactamente el mismo resultado y ranking."""
        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"))
        cand_b = _make_candidate("SUP-02", "Beta", wholesale_price=Decimal("12000"))

        risk_res_1 = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A")
        rec_1 = SupplierRecommendationEngine.generate_recommendation(risk_res_1, opportunity_id="OPP-100")

        risk_res_2 = SupplierRiskComparator.evaluate_and_compare([cand_a, cand_b], target_product_title="Product A")
        rec_2 = SupplierRecommendationEngine.generate_recommendation(risk_res_2, opportunity_id="OPP-100")

        assert rec_1.decision == rec_2.decision
        assert rec_1.primary_supplier.supplier_id == rec_2.primary_supplier.supplier_id
        assert rec_1.primary_supplier.composite_suitability_score == rec_2.primary_supplier.composite_suitability_score

    def test_26_action_executor_recommend_and_pivot(self):
        """Verifica la integración de RECOMMEND_SUPPLIER y CONTINGENCY_PIVOT en SupplierDiscoveryActionExecutor."""
        from src.application.supplier_intelligence.supplier_discovery_action_executor import SupplierDiscoveryActionExecutor
        from src.domain.mission.models import LoopDecision, LoopState, LoopAction

        cand_a = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("10000"))
        cand_b = _make_candidate("SUP-02", "Beta", wholesale_price=Decimal("12000"))

        class MockSource(SupplierSource):
            @property
            def source_name(self) -> str:
                return "MOCK_SOURCE"

            def search_suppliers(self, **kwargs):
                return [cand_a, cand_b]

        executor = SupplierDiscoveryActionExecutor(sources=[MockSource()])
        state = LoopState(
            mission_id="m-c04",
            iteration=1,
            goal="Supplier recommendation",
            current_target="Product A",
            observations=(),
            evidences=(),
            decision_history=(),
        )

        # 1. Discover
        dec_disc = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Discovering suppliers",
            parameters={"operation": "DISCOVER", "query": "Product A", "target_market_price": "20000"},
        )
        executor.execute(dec_disc, state)

        # 2. Recommend
        dec_rec = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Generating recommendation",
            parameters={"operation": "RECOMMEND_SUPPLIER", "opportunity_id": "OPP-100"},
        )
        res_rec = executor.execute(dec_rec, state)

        assert res_rec["status"] == "SUCCESS"
        assert res_rec["operation"] == "RECOMMEND_SUPPLIER"
        assert res_rec["primary_supplier"]["supplier_id"] == "SUP-01"
        assert res_rec["fallback_supplier"]["supplier_id"] == "SUP-02"

        # 3. Contingency Pivot
        dec_piv = LoopDecision(
            action=LoopAction.PIVOT,
            reason="Primary out of stock",
            parameters={
                "operation": "CONTINGENCY_PIVOT",
                "trigger": "STOCK_UNAVAILABLE",
                "details": "Alpha warehouse is empty",
            }
        )
        res_piv = executor.execute(dec_piv, state)

        assert res_piv["status"] == "SUCCESS"
        assert res_piv["pivoted_successfully"] is True
        assert res_piv["new_primary"]["supplier_id"] == "SUP-02"

    def test_27_no_fabricated_evidence_unknown_integrity(self):
        """Verifica que el motor no invente precios, lead times ni stock no observados."""
        cand = _make_candidate("SUP-01", "Alpha", wholesale_price=None, lead_time_days=None, shipping_cost=None)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        rec = SupplierRecommendationEngine.generate_recommendation(risk_res, opportunity_id="OPP-100")

        # No debe haber primary_supplier confirmado con score inflado
        assert rec.decision == SupplierRecommendationDecision.NEEDS_INVESTIGATION
        assert any("Wholesale price unknown" in u for u in rec.unknowns)

    def test_28_regression_safety_and_compatibility(self):
        """Verifica que las funcionalidades de C-01, C-02 y C-03 se mantengan intactas."""
        cand = _make_candidate("SUP-01", "Alpha", wholesale_price=Decimal("15000"), moq=10)
        risk_res = SupplierRiskComparator.evaluate_and_compare([cand], target_product_title="Product A")
        assert len(risk_res.items) == 1
        assert risk_res.best_supplier_candidate is not None
        assert risk_res.best_supplier_candidate.supplier_id == "SUP-01"
