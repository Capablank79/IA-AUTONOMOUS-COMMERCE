import json
from datetime import datetime
from pathlib import Path
from typing import Union

from src.domain.oauth.models import OAuthConnection
from src.domain.oauth.ports import OAuthConnectionRepository


class OAuthConnectionNotFoundError(Exception):
    """Raised when an OAuth connection cannot be found."""
    pass


class JsonOAuthConnectionRepository(OAuthConnectionRepository):
    """
    JSON-based implementation of OAuthConnectionRepository.
    """

    def __init__(self, data_dir: Union[Path, str]):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def save(self, connection: OAuthConnection) -> None:
        file_path = self.data_dir / (
            f"{connection.provider}_{connection.user_id}.json"
        )

        data = {
            "provider": connection.provider,
            "user_id": connection.user_id,
            "access_token": connection.access_token,
            "refresh_token": connection.refresh_token,
            "expires_at": connection.expires_at.isoformat(),
            "scope": connection.scope,
            "token_type": connection.token_type,
        }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

    def get(self, provider: str, user_id: str) -> OAuthConnection:
        file_path = self.data_dir / f"{provider}_{user_id}.json"

        if not file_path.exists():
            raise OAuthConnectionNotFoundError(
                f"OAuth connection not found: {provider}/{user_id}"
            )

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return OAuthConnection(
            provider=data["provider"],
            user_id=data["user_id"],
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            scope=data.get("scope"),
            token_type=data.get("token_type"),
        )
