import os
from pathlib import Path

from dotenv import load_dotenv

from src.application.oauth.connection_service import OAuthConnectionService
from src.infrastructure.mercadolibre.oauth_client import MercadoLibreOAuthClient
from src.infrastructure.persistence.data.json.oauth_connection_repository import (
    JsonOAuthConnectionRepository,
)

load_dotenv()

CLIENT_ID = os.getenv("MERCADOLIBRE_CLIENT_ID")
CLIENT_SECRET = os.getenv("MERCADOLIBRE_CLIENT_SECRET")

oauth_repository = JsonOAuthConnectionRepository(
    Path("data/oauth")
)

oauth_client = MercadoLibreOAuthClient(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
)

oauth_service = OAuthConnectionService(
    repository=oauth_repository,
    oauth_client=oauth_client,
)

