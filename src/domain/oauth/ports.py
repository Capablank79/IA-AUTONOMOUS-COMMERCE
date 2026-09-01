from typing import Protocol

from .models import OAuthConnection


class OAuthConnectionRepository(Protocol):
    def save(self, connection: OAuthConnection) -> None:
        """Persist an OAuth connection."""
        ...

    def get(self, provider: str, user_id: str) -> OAuthConnection:
        """Retrieve an OAuth connection by provider and user ID."""
        ...


class OAuthClientPort(Protocol):
    def refresh(self, connection: OAuthConnection) -> OAuthConnection:
        """Refresh OAuth tokens for a connection."""
        ...
