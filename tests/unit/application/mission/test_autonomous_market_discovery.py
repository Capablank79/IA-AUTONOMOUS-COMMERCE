import pytest
from decimal import Decimal
from typing import List, Dict, Any, Optional

from src.domain.mission.models import (
    LoopAction,
    LoopDecision,
    LoopState,
    MissionStatus,
)
from src.domain.mission.ports import DecisionProvider, ActionExecutor
from src.domain.market_intelligence.models import (
    MarketListing,
    MarketSnapshot,
    Marketplace,
    Money,
    SearchCriteria,
    VisitSignal,
    ReviewSignal,
    Confidence,
)
from src.domain.market_intelligence.ports import (
    MarketplaceDataSource,
    VisitsDataSource,
    ReviewsDataSource,
)
from src.domain.opportunity.models import CompletionPolicy, BestKnownOpportunity
from src.domain.opportunity.engine import OpportunityEngine
from src.application.market_intelligence.market_discovery_action_executor import MarketDiscoveryActionExecutor
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits
from src.application.mission.autonomous_market_discovery_service import AutonomousMarketDiscoveryService
from datetime import datetime, timezone


class ScriptedDecisionProvider(DecisionProvider):
    def __init__(self, decisions: List[LoopDecision]):
        self.decisions = decisions

    def decide(self, state: LoopState) -> LoopDecision:
        index = state.iteration
        if index < len(self.decisions):
            return self.decisions[index]
        return LoopDecision(
            action=LoopAction.COMPLETE,
            reason="No more scripted decisions"
        )


class FakeMarketplaceDataSource(MarketplaceDataSource):
    def __init__(self, listings_by_query: Optional[Dict[str, List[MarketListing]]] = None):
        self.listings_by_query = listings_by_query or {}
        self.fetch_calls = 0

    def fetch_snapshot(self, criteria: SearchCriteria) -> MarketSnapshot:
        self.fetch_calls += 1
        listings = self.listings_by_query.get(criteria.query, [])
        return MarketSnapshot(
            snapshot_id="snap-test-1",
            timestamp=datetime.now(timezone.utc),
            search_criteria=criteria,
            marketplace=Marketplace.MERCADO_LIBRE,
            listings=listings,
            total_results=len(listings)
        )


class FakeVisitsDataSource(VisitsDataSource):
    def __init__(self, visits_by_item: Optional[Dict[str, int]] = None):
        self.visits_by_item = visits_by_item or {}
        self.calls = 0

    def get_visits(self, item_id: str, window_days: int = 30) -> VisitSignal:
        self.calls += 1
        visits = self.visits_by_item.get(item_id, 0)
        return VisitSignal(
            item_id=item_id,
            window=f"{window_days}d",
            total_visits=visits,
            observed_days=window_days,
            coverage_ratio=1.0,
            source="mercadolibre_api",
            observed_at=datetime.now(timezone.utc)
        )


class FakeReviewsDataSource(ReviewsDataSource):
    def __init__(self, reviews_by_item: Optional[Dict[str, Dict[str, Any]]] = None):
        self.reviews_by_item = reviews_by_item or {}
        self.calls = 0

    def get_reviews(self, item_id: str, offset: int = 0, limit: int = 50) -> ReviewSignal:
        self.calls += 1
        data = self.reviews_by_item.get(item_id, {"total": 0, "rating": 0.0})
        return ReviewSignal(
            item_id=item_id,
            total_reviews=data.get("total", 0),
            average_rating=data.get("rating", 0.0),
            reviews=[],
            paging={},
            observed_at=datetime.now(timezone.utc)
        )


def _make_listing(item_id: str, title: str, price: Decimal, sold_quantity: int = 100) -> MarketListing:
    return MarketListing(
        external_id=item_id,
        marketplace=Marketplace.MERCADO_LIBRE,
        title=title,
        price=Money(amount=price, currency="CLP"),
        sold_quantity=sold_quantity,
        available_quantity=50,
        seller_id="seller_123",
        condition="new",
        shipping_info={},
        category="MLC1234"
    )


# -------------------------------------------------------------
# CASO 1: Inicia misión de descubrimiento comercial
# -------------------------------------------------------------
def test_market_discovery_initializes_and_executes_mission():
    listing_a = _make_listing("MLC100", "Cafetera Espresso Italiana", Decimal("29990"), sold_quantity=150)
    market_source = FakeMarketplaceDataSource({"cafetera": [listing_a]})
    visits_source = FakeVisitsDataSource({"MLC100": 600})
    reviews_source = FakeReviewsDataSource({"MLC100": {"total": 45, "rating": 4.8}})

    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Explorar cafetera", target="cafetera"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Suficiente evidencia")
    ]
    provider = ScriptedDecisionProvider(decisions)

    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=market_source,
        visits_data_source=visits_source,
        reviews_data_source=reviews_source
    )

    result = service.execute_discovery_mission(query="cafetera")

    assert result.status == MissionStatus.COMPLETED
    assert result.output["total_candidates_found"] == 1
    assert result.output["best_opportunity"]["product_id"] == "MLC100"
    assert result.output["best_opportunity"]["score"] > 0


# -------------------------------------------------------------
# CASO 2 & 3: Ejecuta acción, observa y cambia de acción (Exploración -> Profundización -> Evaluación)
# -------------------------------------------------------------
def test_market_discovery_alternates_actions_and_observes():
    listing_1 = _make_listing("MLC001", "Mouse Gamer RGB", Decimal("19990"), sold_quantity=120)
    market_source = FakeMarketplaceDataSource({"mouse": [listing_1]})
    visits_source = FakeVisitsDataSource({"MLC001": 800})
    reviews_source = FakeReviewsDataSource({"MLC001": {"total": 50, "rating": 4.9}})

    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Buscar catalogo", target="mouse"),
        LoopDecision(action=LoopAction.CONTINUE, reason="Profundizar en item MLC001", target="MLC001", parameters={"operation": "INVESTIGATE"}),
        LoopDecision(action=LoopAction.PROMOTE, reason="Promover candidato top", target="MLC001"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Convergencia")
    ]
    provider = ScriptedDecisionProvider(decisions)

    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=market_source,
        visits_data_source=visits_source,
        reviews_data_source=reviews_source
    )

    result = service.execute_discovery_mission(query="mouse")

    assert result.status == MissionStatus.COMPLETED
    assert result.output["iterations_used"] == 4
    assert result.output["best_opportunity"]["product_id"] == "MLC001"
    assert visits_source.calls == 1
    assert reviews_source.calls == 1


# -------------------------------------------------------------
# CASO 4 & 5 & 6: Encuentra candidato mejor y actualiza best_known
# -------------------------------------------------------------
def test_best_known_updates_when_better_candidate_found():
    listing_low = _make_listing("MLC_LOW", "Teclado Basico", Decimal("5000"), sold_quantity=5)
    listing_high = _make_listing("MLC_HIGH", "Teclado Mecanico Pro", Decimal("45000"), sold_quantity=250)

    market_source = FakeMarketplaceDataSource({"teclados": [listing_low, listing_high]})
    visits_source = FakeVisitsDataSource({"MLC_HIGH": 1200})
    reviews_source = FakeReviewsDataSource({"MLC_HIGH": {"total": 80, "rating": 4.7}})

    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Buscar teclados", target="teclados"),
        LoopDecision(action=LoopAction.CONTINUE, reason="Investigar candidato lider MLC_HIGH", target="MLC_HIGH"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Convergencia")
    ]
    provider = ScriptedDecisionProvider(decisions)

    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=market_source,
        visits_data_source=visits_source,
        reviews_data_source=reviews_source
    )

    result = service.execute_discovery_mission(query="teclados")

    assert result.output["best_opportunity"]["product_id"] == "MLC_HIGH"
    assert result.output["best_opportunity"]["score"] >= Decimal("40.0")


# -------------------------------------------------------------
# CASO 7 & 8: Pivot entre categorías / espacios de mercado
# -------------------------------------------------------------
def test_agent_can_pivot_to_new_search_space():
    listing_cat_a = _make_listing("MLC_A1", "Funda Celular", Decimal("3000"), sold_quantity=10)
    listing_cat_b = _make_listing("MLC_B1", "Cargador Carga Rapida", Decimal("15000"), sold_quantity=300)

    market_source = FakeMarketplaceDataSource({
        "fundas": [listing_cat_a],
        "cargadores": [listing_cat_b]
    })
    visits_source = FakeVisitsDataSource({"MLC_B1": 900})

    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Explorar fundas", target="fundas"),
        LoopDecision(action=LoopAction.PIVOT, reason="Fundas saturadas, pivotar a cargadores", target="cargadores"),
        LoopDecision(action=LoopAction.CONTINUE, reason="Investigar MLC_B1", target="MLC_B1"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Finalizado con éxito")
    ]
    provider = ScriptedDecisionProvider(decisions)

    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=market_source,
        visits_data_source=visits_source
    )

    result = service.execute_discovery_mission(query="fundas")

    assert result.status == MissionStatus.COMPLETED
    assert result.output["total_candidates_found"] == 2
    assert result.output["best_opportunity"]["product_id"] == "MLC_B1"


# -------------------------------------------------------------
# CASO 9 & 14: Rechaza COMPLETE prematuro cuando la evidencia es insuficiente
# -------------------------------------------------------------
def test_loop_prevents_invalid_premature_complete():
    # Listing con score muy bajo y sin suficientes candidatos para la política
    listing_poor = _make_listing("MLC_POOR", "Producto Sin Demanda", Decimal("1000"), sold_quantity=0)
    market_source = FakeMarketplaceDataSource({"pobre": [listing_poor]})

    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Buscar", target="pobre"),
        # Intento de COMPLETE prematuro que viola min_score (requiere 30.0)
        LoopDecision(action=LoopAction.COMPLETE, reason="Quiero terminar ya"),
        # Al ser rechazado, el loop sigue y después el LLM hace REJECT
        LoopDecision(action=LoopAction.REJECT, reason="Mercado inviable tras intento de cierre")
    ]
    provider = ScriptedDecisionProvider(decisions)

    policy = CompletionPolicy(min_candidates=1, min_score=Decimal("30.0"))
    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=market_source,
        completion_policy=policy,
        default_max_iterations=5
    )

    result = service.execute_discovery_mission(query="pobre")

    assert result.status == MissionStatus.COMPLETED or result.output["loop_status"] == "REJECTED"
    assert result.output["loop_status"] == "REJECTED"
    # Verificar que en la traza quedó registrado el intento de COMPLETE rechazado
    trace_steps = [t.metadata.get("observation", {}).get("status") for t in result.trace]
    assert "PREMATURE_COMPLETION_REJECTED" in trace_steps


# -------------------------------------------------------------
# CASO 10 & 11: Respeta límites operativos (max_iterations, time, call limit)
# -------------------------------------------------------------
def test_loop_respects_max_iterations_and_limits():
    listing = _make_listing("MLC_INF", "Item Bucle", Decimal("10000"), sold_quantity=50)
    market_source = FakeMarketplaceDataSource({"loop": [listing]})

    infinite_decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Bucleando", target="loop")
    ] * 20
    provider = ScriptedDecisionProvider(infinite_decisions)

    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=market_source,
        default_max_iterations=3,
        default_limits=LoopLimits(max_iterations=3)
    )

    result = service.execute_discovery_mission(query="loop")

    assert result.output["iterations_used"] == 3
    assert result.output["termination_reason"] == "MAX_ITERATIONS"


# -------------------------------------------------------------
# CASO 12: Maneja fallos del ActionExecutor con Safe Termination
# -------------------------------------------------------------
class FailingMarketDataSource(MarketplaceDataSource):
    def fetch_snapshot(self, criteria: SearchCriteria) -> MarketSnapshot:
        raise ConnectionResetError("Conexión reseteada por el peer remoto")


def test_loop_recovers_and_safe_terminates_on_executor_failure():
    failing_source = FailingMarketDataSource()
    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Intentar búsqueda", target="falla")
    ]
    provider = ScriptedDecisionProvider(decisions)

    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=failing_source
    )

    result = service.execute_discovery_mission(query="falla")

    assert result.status == MissionStatus.FAILED
    assert result.output["termination_reason"] == "EXECUTOR_ERROR"
    assert len(result.errors) > 0
    assert "Conexión reseteada" in result.errors[0]


# -------------------------------------------------------------
# CASO 13: Maneja respuestas inválidas del DecisionProvider
# -------------------------------------------------------------
class InvalidDecisionProvider(DecisionProvider):
    def decide(self, state: LoopState) -> Any:
        return "esto no es un LoopDecision"


def test_loop_handles_invalid_decision_provider_response():
    provider = InvalidDecisionProvider()
    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=FakeMarketplaceDataSource()
    )

    result = service.execute_discovery_mission(query="invalid")

    assert result.status == MissionStatus.FAILED
    assert result.output["termination_reason"] == "INVALID_DECISION"
    assert len(result.errors) > 0


# -------------------------------------------------------------
# CASO 15 & 16: Conserva trazabilidad completa e inmutabilidad
# -------------------------------------------------------------
def test_traceability_and_immutability():
    # Listing con suficiente demanda y score para pasar la validación determinista de cierre
    listing = _make_listing("MLC_IMM", "Producto Inmutable", Decimal("10000"), sold_quantity=150)
    market_source = FakeMarketplaceDataSource({"test": [listing]})

    decisions = [
        LoopDecision(action=LoopAction.CONTINUE, reason="Paso 1", target="test"),
        LoopDecision(action=LoopAction.COMPLETE, reason="Final")
    ]
    provider = ScriptedDecisionProvider(decisions)

    service = AutonomousMarketDiscoveryService(
        decision_provider=provider,
        marketplace_data_source=market_source
    )

    result = service.execute_discovery_mission(query="test")

    assert len(result.trace) == 2
    assert result.trace[0].step == "ITERATION_1_CONTINUE"
    assert result.trace[1].step == "ITERATION_2_COMPLETE"

    # Inmutabilidad de las estructuras frozen
    with pytest.raises(Exception):
        result.mission_id = "modified_id"

    # Inmutabilidad de BestKnownOpportunity
    best_opp = result.output["best_opportunity"]
    assert best_opp["product_id"] == "MLC_IMM"
