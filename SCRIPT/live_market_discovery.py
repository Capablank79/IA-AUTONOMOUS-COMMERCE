
import os
import uuid
import json
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

from src.domain.mission.models import Mission, MissionType, MissionStatus
from src.application.mission.orchestrator import BasicMissionOrchestrator
from src.infrastructure.mission.repository import InMemoryMissionRepository
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.marketplace_data_source import MercadoLibreMarketplaceDataSource
from src.infrastructure.mercadolibre.visits_data_source import MercadoLibreVisitsDataSource
from src.application.oauth.connection_service import OAuthConnectionService
from src.infrastructure.mercadolibre.oauth_client import MercadoLibreOAuthClient
from src.infrastructure.persistence.data.json.oauth_connection_repository import JsonOAuthConnectionRepository
from src.application.market_intelligence.traffic_intelligence_service import TrafficIntelligenceService

def run_live_discovery():
    print("--- INICIANDO LIVE-01: REAL MARKET DISCOVERY ---")

    # 1. Cargar Entorno
    load_dotenv()
    client_id = os.getenv("MERCADOLIBRE_CLIENT_ID")
    client_secret = os.getenv("MERCADOLIBRE_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("BLOQUEO: Faltan credenciales en .env (MERCADOLIBRE_CLIENT_ID/SECRET)")
        return

    # 2. Setup Infraestructura OAuth
    oauth_repo = JsonOAuthConnectionRepository(Path("data/oauth"))
    oauth_client = MercadoLibreOAuthClient(client_id, client_secret)
    oauth_service = OAuthConnectionService(oauth_repo, oauth_client)

    user_id = "55197108"
    print(f"Evidencia: Usando User ID {user_id} desde data/oauth")

    try:
        connection = oauth_service.get_valid_connection("mercadolibre", user_id)
        print("Evidencia: Conexión OAuth validada/refrescada exitosamente.")
    except Exception as e:
        print(f"BLOQUEO: Error al validar/refrescar OAuth: {str(e)}")
        return

    # 3. Setup API Client y Data Sources
    api_client = MercadoLibreApiClient(connection.access_token)
    market_data_source = MercadoLibreMarketplaceDataSource(api_client)
    visits_data_source = MercadoLibreVisitsDataSource(api_client)
    traffic_intelligence = TrafficIntelligenceService(visits_data_source)

    # 4. Setup Orquestador
    mission_repo = InMemoryMissionRepository()
    orchestrator = BasicMissionOrchestrator(
        repository=mission_repo,
        market_data_source=market_data_source,
        traffic_intelligence=traffic_intelligence
    )

    # 5. Crear Misión
    mission_id = str(uuid.uuid4())
    mission = Mission(
        mission_id=mission_id,
        type=MissionType.MARKET_DISCOVERY,
        parameters={
            "query": "ssd sata 480gb",
            "user_id": user_id,
            "limit": 5,
            "marketplace": "MERCADO_LIBRE"
        }
    )

    print(f"Ejecutando misión {mission_id}...")

    # 6. Ejecutar
    try:
        orchestrator.submit(mission)
        result = orchestrator.get_result(mission_id)

        # 7. Reportar
        print("\n--- MISSION RESULT ---")
        print(f"Status: {result.status.value}")
        print(f"Finished At: {result.finished_at}")

        print("\n--- TRACE ---")
        for entry in result.trace:
            print(f"[{entry.status.value}] {entry.step}: {entry.metadata}")

        if result.blocks:
            print("\n--- BLOCKS ---")
            for block in result.blocks:
                print(f"- {block}")

        if result.errors:
            print("\n--- ERRORS ---")
            for error in result.errors:
                print(f"- {error}")

        print("\n--- EVIDENCES (Count) ---")
        print(f"Total evidences collected: {len(result.evidences)}")

        # Guardar resultado para inspección
        output_file = Path("LIVE_MISSION_RESULT.json")

        # Helper para serializar Decimal y datetime
        def default_serializer(obj):
            if isinstance(obj, (datetime)):
                return obj.isoformat()
            if hasattr(obj, 'value'): # Enums
                return obj.value
            if hasattr(obj, '__dict__'):
                return obj.__dict__
            return str(obj)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result.__dict__, f, indent=2, default=default_serializer)

        print(f"\nResultado detallado guardado en: {output_file}")

    except Exception as e:
        print(f"BLOQUEO: Error durante la ejecución de la misión: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_live_discovery()
