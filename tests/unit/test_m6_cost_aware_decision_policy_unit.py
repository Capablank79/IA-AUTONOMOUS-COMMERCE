"""
Tests Unitarios para la Política de Decisión Consciente del Coste (Cost-aware Decision Policy - Hito M.6).

Cobertura requerida:
1. route within budget
2. route over budget
3. cheaper valid route selected
4. cheap incapable route excluded (Quality First)
5. cheap low-quality route excluded (Quality First)
6. HIGH criticality preserved (no degraded / no unknown cost)
7. UNKNOWN cost != zero
8. missing pricing -> UNKNOWN
9. Decimal cost arithmetic
10. cache HIT cost handling (avoided inference, incremental cost = 0.00)
11. compressed token count used (post-compression tokens)
12. deterministic tie-break
13. policy versioning and checksum verification
14. estimated != actual cost separation
15. no false approval when all routes fail or exceed budget
"""

from decimal import Decimal
import pytest

from src.domain.cost_aware_policy.models import (
    CostAwareDecision,
    CostAwareDecisionStatus,
    CostAwarePolicy,
    CostAwareReasonCode,
    CostAwareRequest,
    RouteCostEstimate,
)
from src.domain.model_routing.models import (
    LatencyRequirement,
    ModelRoute,
    QualityRequirement,
    RouteCapability,
    RouteStatus,
    TaskCriticality,
)
from src.domain.cost.models import PricingRate, CostType, UsageRecord, UsageUnit
from src.application.cost.pricing_catalog import InMemoryPricingCatalog
from src.application.cost_aware_policy.cost_aware_decision_service import (
    CostAwareDecisionService,
)


@pytest.fixture
def custom_catalog():
    """Catálogo de tarifas de prueba para inferencia."""
    cat = InMemoryPricingCatalog()
    # Modelo económico estándar
    cat.register_rate(
        PricingRate(
            provider="provider-a",
            service_or_model="mini-model",
            currency="USD",
            input_rate=Decimal("0.10"),  # $0.10 por 1M
            output_rate=Decimal("0.40"), # $0.40 por 1M
            rate_scale=Decimal("1000000"),
            version="1.0.0",
        )
    )
    # Modelo intermedio potente
    cat.register_rate(
        PricingRate(
            provider="provider-a",
            service_or_model="pro-model",
            currency="USD",
            input_rate=Decimal("1.00"),  # $1.00 por 1M
            output_rate=Decimal("3.00"), # $3.00 por 1M
            rate_scale=Decimal("1000000"),
            version="1.0.0",
        )
    )
    # Modelo superior caro
    cat.register_rate(
        PricingRate(
            provider="provider-b",
            service_or_model="flagship-model",
            currency="USD",
            input_rate=Decimal("5.00"),  # $5.00 por 1M
            output_rate=Decimal("15.00"),# $15.00 por 1M
            rate_scale=Decimal("1000000"),
            version="1.0.0",
        )
    )
    return cat


@pytest.fixture
def decision_service(custom_catalog):
    return CostAwareDecisionService(pricing_catalog=custom_catalog)


def test_1_route_within_budget(decision_service):
    """1. Caso: Ruta técnicamente válida y dentro del presupuesto es aprobada."""
    route = ModelRoute(
        route_id="r-mini",
        provider="provider-a",
        model_id="mini-model",
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.STANDARD,
    )
    request = CostAwareRequest(
        task_type="market_analysis",
        estimated_input_tokens=1000,
        estimated_output_tokens=500,
        budget_ceiling=Decimal("0.01"),  # Presupuesto amplio ($0.01)
        eligible_routes=(route,),
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.is_approved is True
    assert decision.selected_route.route_id == "r-mini"
    # Coste esperado: (1000/1M)*0.10 + (500/1M)*0.40 = 0.00010 + 0.00020 = 0.00030
    assert decision.estimated_cost == Decimal("0.000300")
    assert CostAwareReasonCode.WITHIN_BUDGET.value in decision.reason_codes


def test_2_route_over_budget(decision_service):
    """2. Caso: La única ruta excede el presupuesto disponible -> REJECTED."""
    route = ModelRoute(
        route_id="r-flagship",
        provider="provider-b",
        model_id="flagship-model",
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.SUPERIOR,
    )
    request = CostAwareRequest(
        task_type="market_analysis",
        estimated_input_tokens=100000,  # 100K tokens -> Coste input: (100K/1M)*5 = $0.50
        estimated_output_tokens=50000,  # 50K tokens -> Coste output: (50K/1M)*15 = $0.75 => Total: $1.25
        budget_ceiling=Decimal("0.50"), # Presupuesto $0.50 (< $1.25)
        eligible_routes=(route,),
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.REJECTED
    assert decision.is_approved is False
    assert decision.selected_route is None
    assert decision.estimated_cost == Decimal("1.250000")
    assert CostAwareReasonCode.EXCEEDS_BUDGET.value in decision.reason_codes


def test_3_cheaper_valid_route_selected(decision_service):
    """3. Caso: Entre dos rutas técnicamente válidas y dentro de presupuesto, se selecciona la más económica."""
    route_mini = ModelRoute(
        route_id="r-mini",
        provider="provider-a",
        model_id="mini-model",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        quality_class=QualityRequirement.STANDARD,
    )
    route_pro = ModelRoute(
        route_id="r-pro",
        provider="provider-a",
        model_id="pro-model",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        quality_class=QualityRequirement.HIGH,
    )

    request = CostAwareRequest(
        task_type="order_dispatch",
        min_quality=QualityRequirement.STANDARD,
        required_capabilities=(RouteCapability.TOOL_USE,),
        estimated_input_tokens=10000,
        estimated_output_tokens=2000,
        budget_ceiling=Decimal("0.10"),
        eligible_routes=(route_pro, route_mini),  # pro primero en lista
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "r-mini"
    assert CostAwareReasonCode.CHEAPEST_VALID_SELECTED.value in decision.reason_codes


def test_4_cheap_incapable_route_excluded(decision_service):
    """4. Caso Quality First: Una ruta muy barata que carece de capacidades requeridas NUNCA es seleccionada."""
    cheap_incapable = ModelRoute(
        route_id="r-cheap-dumb",
        provider="provider-a",
        model_id="mini-model",
        capabilities=(),  # Sin capacidades
        quality_class=QualityRequirement.STANDARD,
    )
    capable_pro = ModelRoute(
        route_id="r-pro",
        provider="provider-a",
        model_id="pro-model",
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.VISION),
        quality_class=QualityRequirement.HIGH,
    )

    request = CostAwareRequest(
        task_type="visual_inspection",
        required_capabilities=(RouteCapability.VISION,),
        estimated_input_tokens=2000,
        estimated_output_tokens=1000,
        budget_ceiling=Decimal("0.05"),
        eligible_routes=(cheap_incapable, capable_pro),
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "r-pro"
    # Verificar que cheap_incapable fue catalogado con CAPABILITY_UNMET
    dumb_est = next(e for e in decision.route_estimates if e.route_id == "r-cheap-dumb")
    assert dumb_est.is_technically_eligible is False
    assert CostAwareReasonCode.CAPABILITY_UNMET.value in dumb_est.exclusion_reasons


def test_5_cheap_low_quality_route_excluded(decision_service):
    """5. Caso Quality First: Una ruta barata con calidad inferior al mínimo exigido es excluida."""
    cheap_standard = ModelRoute(
        route_id="r-mini",
        provider="provider-a",
        model_id="mini-model",
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.STANDARD,
    )
    expensive_high = ModelRoute(
        route_id="r-pro",
        provider="provider-a",
        model_id="pro-model",
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.HIGH,
    )

    request = CostAwareRequest(
        task_type="legal_compliance",
        min_quality=QualityRequirement.HIGH,  # Exige HIGH
        required_capabilities=(RouteCapability.TOOL_USE,),
        estimated_input_tokens=5000,
        estimated_output_tokens=1000,
        budget_ceiling=Decimal("0.05"),
        eligible_routes=(cheap_standard, expensive_high),
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "r-pro"
    std_est = next(e for e in decision.route_estimates if e.route_id == "r-mini")
    assert std_est.is_technically_eligible is False
    assert CostAwareReasonCode.QUALITY_UNMET.value in std_est.exclusion_reasons


def test_6_high_criticality_preserved(decision_service):
    """6. Caso Criticidad HIGH/CRITICAL: No degrada a rutas con estado DEGRADED ni acepta coste desconocido."""
    degraded_cheap = ModelRoute(
        route_id="r-degraded",
        provider="provider-a",
        model_id="mini-model",
        status=RouteStatus.DEGRADED,
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.STANDARD,
    )
    available_pro = ModelRoute(
        route_id="r-pro",
        provider="provider-a",
        model_id="pro-model",
        status=RouteStatus.AVAILABLE,
        capabilities=(RouteCapability.TOOL_USE,),
        quality_class=QualityRequirement.HIGH,
    )

    request = CostAwareRequest(
        task_type="financial_settlement",
        criticality=TaskCriticality.HIGH,
        required_capabilities=(RouteCapability.TOOL_USE,),
        estimated_input_tokens=2000,
        estimated_output_tokens=500,
        budget_ceiling=Decimal("0.05"),
        eligible_routes=(degraded_cheap, available_pro),
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "r-pro"
    deg_est = next(e for e in decision.route_estimates if e.route_id == "r-degraded")
    assert deg_est.is_technically_eligible is False
    assert CostAwareReasonCode.DEGRADED_ROUTE_REJECTED.value in deg_est.exclusion_reasons


def test_7_unknown_cost_not_zero():
    """7. Caso: UNKNOWN cost nunca es tratado como 0.00 ni como gratuito."""
    est = RouteCostEstimate(
        route_id="r-unknown",
        provider="unknown-provider",
        model_id="mystery-model",
        estimated_total_cost=None,
        is_known=False,
    )

    assert est.estimated_total_cost is None
    assert est.is_known is False
    # No es igual a Decimal(0)
    assert est.estimated_total_cost != Decimal("0.00")


def test_8_missing_pricing_produces_unknown(decision_service):
    """8. Caso: Modelo sin tarifa configurada ni metadatos de precio produce UNKNOWN."""
    unpriced_route = ModelRoute(
        route_id="r-unpriced",
        provider="custom-ai",
        model_id="unlisted-model-v1",
        capabilities=(RouteCapability.TOOL_USE,),
    )
    request = CostAwareRequest(
        task_type="experimental_task",
        criticality=TaskCriticality.HIGH,  # En criticidad alta, uncosted route genera UNKNOWN global
        estimated_input_tokens=1000,
        estimated_output_tokens=500,
        eligible_routes=(unpriced_route,),
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.UNKNOWN
    assert decision.selected_route is None
    assert decision.estimated_cost is None
    assert CostAwareReasonCode.UNKNOWN_COST.value in decision.reason_codes


def test_9_decimal_cost_precision(decision_service):
    """9. Caso: Aritmética financiera estricta en Decimal (sin pérdidas de coma flotante)."""
    route = ModelRoute(
        route_id="r-mini",
        provider="provider-a",
        model_id="mini-model",
        capabilities=(RouteCapability.TOOL_USE,),
    )
    request = CostAwareRequest(
        task_type="micro_task",
        estimated_input_tokens=7,
        estimated_output_tokens=3,
        eligible_routes=(route,),
    )

    decision = decision_service.evaluate(request)

    assert isinstance(decision.estimated_cost, Decimal)
    # (7/1000000)*0.10 + (3/1000000)*0.40 = 0.0000007 + 0.0000012 = 0.0000019
    expected = Decimal("0.00000190")
    assert decision.estimated_cost == expected


def test_10_cache_hit_cost_handling(decision_service):
    """10. Caso M.4 Caching: Cache HIT confirmado evita la inferencia (coste incremental 0.00)."""
    route = ModelRoute(
        route_id="r-mini",
        provider="provider-a",
        model_id="mini-model",
        capabilities=(RouteCapability.TOOL_USE,),
    )
    request = CostAwareRequest(
        task_type="frequent_query",
        cache_hit=True,  # HIT confirmado
        estimated_input_tokens=50000,
        estimated_output_tokens=10000,
        budget_ceiling=Decimal("0.001"),
        eligible_routes=(route,),
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.cache_impact_avoided is True
    assert decision.estimated_cost == Decimal("0.00")
    assert CostAwareReasonCode.CACHE_HIT_AVOIDED.value in decision.reason_codes


def test_11_compressed_token_count_used(decision_service):
    """11. Caso M.3 Prompt Compression: El cálculo utiliza el número de tokens final post-compresión."""
    route = ModelRoute(
        route_id="r-pro",
        provider="provider-a",
        model_id="pro-model",
        capabilities=(RouteCapability.TOOL_USE,),
    )
    # Sin compresión (10,000 tokens input)
    req_uncompressed = CostAwareRequest(
        task_type="doc_summary",
        estimated_input_tokens=10000,
        estimated_output_tokens=1000,
        eligible_routes=(route,),
    )
    # Con compresión (reducido a 4,000 tokens input)
    req_compressed = CostAwareRequest(
        task_type="doc_summary",
        estimated_input_tokens=4000,
        estimated_output_tokens=1000,
        compression_applied=True,
        eligible_routes=(route,),
    )

    dec_uncomp = decision_service.evaluate(req_uncompressed)
    dec_comp = decision_service.evaluate(req_compressed)

    assert dec_uncomp.estimated_cost > dec_comp.estimated_cost
    # Uncompressed: (10000/1M)*1.00 + (1000/1M)*3.00 = 0.010 + 0.003 = 0.013
    assert dec_uncomp.estimated_cost == Decimal("0.013000")
    # Compressed: (4000/1M)*1.00 + (1000/1M)*3.00 = 0.004 + 0.003 = 0.007
    assert dec_comp.estimated_cost == Decimal("0.007000")


def test_12_deterministic_tie_break(decision_service):
    """12. Caso: Desempate determinista ante rutas con idéntico coste, calidad y latencia."""
    route_b = ModelRoute(
        route_id="r-b",
        provider="provider-a",
        model_id="mini-model",
        priority=100,
        quality_class=QualityRequirement.STANDARD,
    )
    route_a = ModelRoute(
        route_id="r-a",
        provider="provider-a",
        model_id="mini-model",
        priority=100,
        quality_class=QualityRequirement.STANDARD,
    )

    # Entran desordenadas
    request = CostAwareRequest(
        task_type="tie_task",
        estimated_input_tokens=1000,
        estimated_output_tokens=500,
        eligible_routes=(route_b, route_a),
    )

    decision = decision_service.evaluate(request)

    assert decision.status == CostAwareDecisionStatus.APPROVED
    assert decision.selected_route.route_id == "r-a"  # r-a < r-b lexicográficamente
    assert CostAwareReasonCode.TIE_BREAK_DETERMINISTIC.value in decision.reason_codes


def test_13_policy_versioning_and_checksum():
    """13. Caso: CostAwarePolicy es inmutable, versionada y genera checksum SHA-256 consistente."""
    policy1 = CostAwarePolicy(
        policy_id="corp_policy_2026",
        version="2.1.0",
        max_cost_per_inference=Decimal("0.50"),
        quality_floor=QualityRequirement.STANDARD,
    )
    policy2 = CostAwarePolicy(
        policy_id="corp_policy_2026",
        version="2.1.0",
        max_cost_per_inference=Decimal("0.50"),
        quality_floor=QualityRequirement.STANDARD,
    )

    assert policy1.policy_id == "corp_policy_2026"
    assert policy1.version == "2.1.0"
    assert policy1.calculate_checksum() == policy2.calculate_checksum()


def test_14_estimated_vs_actual_separation():
    """14. Caso: Separación conceptual entre ESTIMATED COST (M.6) y ACTUAL COST (K.3 CostRecord)."""
    decision = CostAwareDecision(
        status=CostAwareDecisionStatus.APPROVED,
        selected_route=None,
        estimated_cost=Decimal("0.025"),
        currency="USD",
        actual_cost_record_id=None,
    )
    # El registro real de K.3 puede diferir tras la ejecución real de inferencia
    actual_record_id = "cst-rec-987654"

    # Inmutabilidad: creamos nueva decisión vinculada
    linked_decision = CostAwareDecision(
        status=decision.status,
        selected_route=decision.selected_route,
        estimated_cost=decision.estimated_cost,
        currency=decision.currency,
        actual_cost_record_id=actual_record_id,
    )

    assert decision.actual_cost_record_id is None
    assert linked_decision.actual_cost_record_id == "cst-rec-987654"
    assert linked_decision.estimated_cost == Decimal("0.025")


def test_15_no_false_approval(decision_service):
    """15. Caso: Si ninguna ruta cumple o no hay rutas provistas, no existe falsa aprobación silenciosa."""
    request_empty = CostAwareRequest(
        task_type="empty_request",
        eligible_routes=(),
    )
    decision = decision_service.evaluate(request_empty)

    assert decision.status == CostAwareDecisionStatus.NO_ELIGIBLE_OPTION
    assert decision.is_approved is False
    assert decision.selected_route is None
    assert CostAwareReasonCode.NO_ELIGIBLE_ROUTES.value in decision.reason_codes
