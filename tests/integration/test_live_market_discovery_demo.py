import json
from decimal import Decimal
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.marketplace_data_source import MercadoLibreMarketplaceDataSource
from src.infrastructure.mercadolibre.visits_data_source import MercadoLibreVisitsDataSource
from src.infrastructure.mercadolibre.reviews_data_source import MercadoLibreReviewsDataSource
from src.infrastructure.mercadolibre.trends_data_source import MercadoLibreTrendsDataSource
from src.infrastructure.persistence.data.json.oauth_connection_repository import JsonOAuthConnectionRepository
from src.domain.mission.models import LoopDecision, LoopAction, LoopState
from src.domain.mission.ports import DecisionProvider
from src.application.mission.autonomous_market_discovery_service import AutonomousMarketDiscoveryService
from src.domain.opportunity.engine import OpportunityEngine
from src.domain.opportunity.models import CompletionPolicy


class LiveHeuristicDecisionProvider(DecisionProvider):
    def decide(self, state: LoopState) -> LoopDecision:
        if state.iteration == 0:
            return LoopDecision(
                action=LoopAction.CONTINUE,
                target="MLC1648",
                parameters={"operation": "EXPLORE", "category": "MLC1648", "limit": 5},
                reason="Exploracion de productos destacados de alta demanda en categoria Computacion (MLC1648)"
            )
        elif state.iteration == 1:
            best = state.best_known
            if best and best.product_id:
                return LoopDecision(
                    action=LoopAction.CONTINUE,
                    target=best.product_id,
                    parameters={"operation": "INVESTIGATE", "item_id": best.product_id},
                    reason=f"Profundizar investigacion (visitas y reviews) en el candidato lider {best.product_id}"
                )
            return LoopDecision(action=LoopAction.CONTINUE, reason="Continuar exploracion")
        else:
            return LoopDecision(
                action=LoopAction.COMPLETE,
                reason="Evidencia suficiente recopilada y verificada contra Mercado Libre en vivo para declarar oportunidad ganadora"
            )


def main():
    repo = JsonOAuthConnectionRepository("data/oauth")
    conn = repo.get("mercadolibre", "55197108")
    if not conn:
        print("ERROR: OAuth connection not found in data/oauth")
        return

    api_client = MercadoLibreApiClient(access_token=conn.access_token)
    market_ds = MercadoLibreMarketplaceDataSource(api_client=api_client)
    visits_ds = MercadoLibreVisitsDataSource(api_client=api_client)
    reviews_ds = MercadoLibreReviewsDataSource(api_client=api_client)
    trends_ds = MercadoLibreTrendsDataSource(api_client=api_client)

    service = AutonomousMarketDiscoveryService(
        decision_provider=LiveHeuristicDecisionProvider(),
        marketplace_data_source=market_ds,
        visits_data_source=visits_ds,
        reviews_data_source=reviews_ds,
        trends_data_source=trends_ds,
        opportunity_engine=OpportunityEngine(),
        completion_policy=CompletionPolicy(min_candidates=1, min_score=Decimal("10.0"))
    )

    category = "MLC1648"
    mission_id = "mission-live-hito-a-demo"
    print("=== INICIANDO MISIÓN AUTÓNOMA HITO A EN VIVO (MERCADO LIBRE CHILE) ===")
    print(f"Target Category: {category} (Computación)")
    print(f"Mission ID: {mission_id}\n")

    res = service.execute_discovery_mission(query="Computacion", initial_target=category, mission_id=mission_id)

    print("=== RESULTADO DE LA MISIÓN ===")
    print(f"Status: {res.status.value}")
    print(f"Total Candidatos Evaluados: {res.output.get('total_candidates_found')}")
    print(f"Iteraciones Utilizadas: {res.output.get('iterations_used')}")
    print(f"Razón de Terminación: {res.output.get('termination_reason')}\n")

    best = res.output.get("best_opportunity")
    if best:
        print("=== MEJOR CANDIDATO GANADOR ===")
        print(f"ID: {best.get('product_id')}")
        print(f"Título: {best.get('title')}")
        print(f"Opportunity Score: {best.get('score'):.2f}")
        print(f"Confidence: {best.get('confidence')}")
        print(f"Why Winner: {best.get('why_winner')}")
        print(f"Riesgos: {best.get('risks')}")
        print(f"Unknowns: {best.get('unknowns')}\n")

    print("=== RANKING ORDENADO DE OPORTUNIDADES ===")
    for rank, item in enumerate(res.output.get("top_ranking", []), 1):
        print(f"#{rank} | Score: {item['opportunity_score']:.2f} | Confidence: {item['confidence']} | ID: {item['item_id']} | Precio: ${item['price']:,.0f} {item['currency']} | Sold: {item['sold_quantity']} | Título: {item['title']}")

    print("\n=== TRAZA DEL LOOP COGNITIVO ===")
    for trace_step in res.trace:
        print(f"- Paso: {trace_step.step} | Status: {trace_step.status.value} | Reason: {trace_step.metadata.get('reason')}")


if __name__ == "__main__":
    main()
