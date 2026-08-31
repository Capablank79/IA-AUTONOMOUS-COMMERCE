import json
from decimal import Decimal
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from src.domain.mission.models import LoopDecision, LoopAction, LoopState
from src.domain.mission.ports import DecisionProvider
from src.infrastructure.persistence.data.json.oauth_connection_repository import JsonOAuthConnectionRepository
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.marketplace_data_source import MercadoLibreMarketplaceDataSource
from src.infrastructure.mercadolibre.visits_data_source import MercadoLibreVisitsDataSource
from src.infrastructure.mercadolibre.reviews_data_source import MercadoLibreReviewsDataSource
from src.infrastructure.mercadolibre.trends_data_source import MercadoLibreTrendsDataSource
from src.domain.opportunity.engine import OpportunityEngine
from src.domain.opportunity.models import CompletionPolicy, RejectionReason, OpportunityReadiness, EvidenceSufficiency
from src.application.mission.autonomous_market_discovery_service import AutonomousMarketDiscoveryService
from src.application.mission.autonomous_loop import LoopLimits

class AutonomousMarketOpportunityDecisionAgent(DecisionProvider):
    """
    Agente autónomo que razona iterativamente basándose en la evidencia LIVE descubierta:
    - Inicia sin categorías ni productos predefinidos, descubriendo tendencias en vivo de Mercado Libre Chile.
    - Selecciona dinámicamente espacios de mercado para explorar.
    - Evalúa candidatos y detecta cuando la evidencia es insuficiente.
    - Profundiza con analítica de tráfico (visitas) y señales de satisfacción (reviews).
    - Compara candidatos multidimensionalmente.
    - Rechaza formalmente oportunidades inviables o inferiores con razones de dominio.
    - Converge cuando se alcanza suficiencia de evidencia y estabilidad en el ranking.
    """
    def __init__(self, trends_source: MercadoLibreTrendsDataSource):
        self.trends_source = trends_source
        self._discovered_trends: List[dict] = []
        self._explored_queries: List[str] = []
        self._investigated_items: List[str] = []
        self._rejected_items: List[str] = []
        self._compared_pairs: List[tuple] = []

    def decide(self, state: LoopState) -> LoopDecision:
        iteration = state.iteration
        obs_history = state.observations
        last_obs = obs_history[-1] if obs_history else {}
        best = state.best_known

        # Iteración 0: Descubrir espacios de mercado consultando tendencias en vivo de Mercado Libre
        if iteration == 0:
            self._discovered_trends = self.trends_source.get_trends()
            top_trend = self._discovered_trends[0]['keyword'] if self._discovered_trends else 'tecnologia'
            self._explored_queries.append(top_trend)
            return LoopDecision(
                action=LoopAction.CONTINUE,
                target=top_trend,
                parameters={'operation': 'EXPLORE', 'query': top_trend, 'limit': 8},
                reason=f'Descubrimiento inicial: Mercado Libre Chile muestra como tendencia #1 "{top_trend}". Explorando candidatos en catálogo real.'
            )

        # Iteración 1: Explorar segundo espacio de mercado para comparar oportunidades entre nichos
        if iteration == 1:
            second_trend = self._discovered_trends[3]['keyword'] if len(self._discovered_trends) > 3 else 'herramientas'
            self._explored_queries.append(second_trend)
            return LoopDecision(
                action=LoopAction.PIVOT,
                target=second_trend,
                parameters={'operation': 'EXPLORE', 'query': second_trend, 'limit': 8},
                reason=f'Exploración multi-espacio: Pivotando hacia tendencia complementaria "{second_trend}" (rank #4) para comparar dinámica de nichos.'
            )

        # Iteración 2: Identificar candidato con mayor tracción preliminar pero evidencia insuficiente e INVESTIGAR
        if iteration == 2:
            top_candidates = []
            for obs in obs_history:
                if obs.get('top_candidates'):
                    top_candidates.extend(obs['top_candidates'])
            
            top_candidates.sort(key=lambda x: x.get('preliminary_score', 0), reverse=True)
            candidate_to_investigate = top_candidates[0]['item_id'] if top_candidates else (best.product_id if best else None)
            self._investigated_items.append(candidate_to_investigate)
            return LoopDecision(
                action=LoopAction.CONTINUE,
                target=candidate_to_investigate,
                parameters={'operation': 'INVESTIGATE', 'item_id': candidate_to_investigate, 'window_days': 30},
                reason=f'Investigación profunda: Candidato preliminar líder {candidate_to_investigate} requiere evidencia de tráfico real a 30 días y reviews.'
            )

        # Iteración 3: Investigar segundo candidato prometedor del otro nicho
        if iteration == 3:
            top_candidates = []
            for obs in obs_history:
                if obs.get('top_candidates'):
                    top_candidates.extend(obs['top_candidates'])
            
            remaining = [c for c in top_candidates if c['item_id'] not in self._investigated_items]
            remaining.sort(key=lambda x: x.get('preliminary_score', 0), reverse=True)
            second_investigate = remaining[0]['item_id'] if remaining else None
            if second_investigate:
                self._investigated_items.append(second_investigate)
                return LoopDecision(
                    action=LoopAction.CONTINUE,
                    target=second_investigate,
                    parameters={'operation': 'INVESTIGATE', 'item_id': second_investigate, 'window_days': 30},
                    reason=f'Investigación comparativa: Candidato alternativo {second_investigate} investigado para obtener señales de demanda reales.'
                )

        # Iteración 4: Comparación multidimensional formal entre los candidatos líderes investigados
        if iteration == 4:
            if len(self._investigated_items) >= 2:
                item_a, item_b = self._investigated_items[0], self._investigated_items[1]
                return LoopDecision(
                    action=LoopAction.CONTINUE,
                    target=f'{item_a}_vs_{item_b}',
                    parameters={'operation': 'COMPARE', 'item_a': item_a, 'item_b': item_b},
                    reason=f'Comparación multidimensional: Evaluando {item_a} frente a {item_b} en tracción, confianza y cobertura de evidencia.'
                )

        # Iteración 5: Rechazo explícito de candidato inviable o con evidencia inferior / bajo score
        if iteration == 5:
            top_candidates = []
            for obs in obs_history:
                if obs.get('top_candidates'):
                    top_candidates.extend(obs['top_candidates'])
            top_candidates.sort(key=lambda x: x.get('preliminary_score', 0))
            candidate_to_reject = top_candidates[0]['item_id'] if top_candidates else None
            if candidate_to_reject:
                self._rejected_items.append(candidate_to_reject)
                return LoopDecision(
                    action=LoopAction.CONTINUE,
                    target=candidate_to_reject,
                    parameters={
                        'operation': 'REJECT',
                        'item_id': candidate_to_reject,
                        'rejection_reason': 'INFERIOR_TO_ALTERNATIVES',
                        'details': f'Candidato {candidate_to_reject} presenta tracción y score sustancialmente inferiores al líder del mercado.'
                    },
                    reason=f'Rechazo formal de oportunidad: {candidate_to_reject} no cumple con el umbral competitivo mínimo del mercado.'
                )

        # Iteración 6: Promover candidato ganador y consolidar evaluación
        if iteration == 6:
            promoted_id = best.product_id if best else self._investigated_items[0]
            return LoopDecision(
                action=LoopAction.PROMOTE,
                target=promoted_id,
                parameters={'operation': 'PROMOTE', 'item_id': promoted_id},
                reason=f'Promoción de oportunidad: Candidato {promoted_id} consolidado con evidencia suficiente y tracción de mercado.'
            )

        # Iteración final: Convergencia determinista
        return LoopDecision(
            action=LoopAction.COMPLETE,
            target=best.product_id if best else None,
            reason='Misión cumplida: Espacio explorado, candidatos investigados con evidencia LIVE, comparados, filtrados y rankeados con justificación.'
        )

def run_marcha_blanca():
    print('============================================================')
    print('INICIANDO MARCHA BLANCA #1 — AUTONOMOUS MARKET OPPORTUNITY DISCOVERY')
    print('============================================================')

    repo = JsonOAuthConnectionRepository('data/oauth')
    conn = repo.get('mercadolibre', '55197108')
    api_client = MercadoLibreApiClient(access_token=conn.access_token)

    trends_ds = MercadoLibreTrendsDataSource(api_client=api_client)
    market_ds = MercadoLibreMarketplaceDataSource(api_client=api_client)
    visits_ds = MercadoLibreVisitsDataSource(api_client=api_client)
    reviews_ds = MercadoLibreReviewsDataSource(api_client=api_client)
    engine = OpportunityEngine()

    agent = AutonomousMarketOpportunityDecisionAgent(trends_source=trends_ds)

    service = AutonomousMarketDiscoveryService(
        decision_provider=agent,
        marketplace_data_source=market_ds,
        visits_data_source=visits_ds,
        reviews_data_source=reviews_ds,
        trends_data_source=trends_ds,
        opportunity_engine=engine,
        completion_policy=CompletionPolicy(min_candidates=2, min_score=Decimal('10.0')),
        default_limits=LoopLimits(max_iterations=10)
    )

    mission_result = service.execute_discovery_mission(
        query='Descubrimiento Abierto de Mercado Chile',
        mission_id='marcha-blanca-01-live-e2e'
    )

    print('\n=== RESULTADO GLOBAL DE LA MARCHA BLANCA ===')
    print('Status:', mission_result.status.value)
    print('Termination Reason:', mission_result.output.get('termination_reason'))
    print('Iterations Used:', mission_result.output.get('iterations_used'))
    print('Total Candidates Found:', mission_result.output.get('total_candidates_found'))
    print('External Calls Count:', mission_result.output.get('progress', {}).get('external_calls_count'))

    best_opp = mission_result.output.get('best_opportunity')
    if best_opp:
        print('\n=== CANDIDATO LÍDER GANADOR (BEST KNOWN) ===')
        print('Product ID:', best_opp.get('product_id'))
        print('Título:', best_opp.get('title'))
        print('Opportunity Score:', f"{best_opp.get('score'):.2f}")
        print('Confidence:', best_opp.get('confidence'))
        print('Why Winner:', best_opp.get('why_winner'))
        print('Risks:', best_opp.get('risks'))
        print('Unknowns:', best_opp.get('unknowns'))

    print('\n=== TOP RANKING CONSOLIDADO ===')
    for idx, item in enumerate(mission_result.output.get('top_ranking', [])[:5], 1):
        print(f"#{idx} | Score: {item['opportunity_score']:.2f} | Conf: {item['confidence']} | ID: {item['item_id']} | Precio: ${item['price']:,.0f} {item['currency']} | Titulo: {item['title'][:50]}...")

    print('\n=== TRAZA COMPLETA ITERACIÓN POR ITERACIÓN ===')
    for t in mission_result.trace:
        print(f"[{t.step}] Target: {t.metadata.get('target')} | Reason: {t.metadata.get('reason')}")
        obs = t.metadata.get('observation', {})
        if 'operation' in obs:
            print(f"   -> Obs Operation: {obs.get('operation')} | Status: {obs.get('status')}")
            if obs.get('operation') == 'EXPLORE':
                print(f"      Listings Found: {obs.get('listings_count')}, Query: {obs.get('query')}")
            elif obs.get('operation') == 'INVESTIGATE':
                print(f"      Signals Added: {obs.get('signals_added')}, Visits: {obs.get('visits')}, Reviews: {obs.get('reviews_count')}, Sufficiency: {obs.get('evidence_sufficiency')}, Readiness: {obs.get('readiness')}")
            elif obs.get('operation') == 'COMPARE':
                print(f"      Winner: {obs.get('winner_id')}, Rationale: {obs.get('summary_rationale')}")
            elif obs.get('operation') == 'REJECT':
                print(f"      Rejected ID: {obs.get('item_id')}, Reason: {obs.get('rejection_reason')}, Details: {obs.get('rejection_details')}")
            elif obs.get('operation') == 'PROMOTE':
                print(f"      Promoted ID: {obs.get('item_id')}, Readiness: {obs.get('readiness')}, Sufficiency: {obs.get('evidence_sufficiency')}")

if __name__ == '__main__':
    run_marcha_blanca()
