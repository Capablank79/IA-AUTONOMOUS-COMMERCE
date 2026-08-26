import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.domain.oauth.models import OAuthConnection


TOKEN_URL = "https://api.mercadolibre.com/oauth/token"


class MercadoLibreOAuthError(Exception):
    """Raised when Mercado Libre token refresh fails."""
    pass


class MercadoLibreOAuthClient:
    """
    Infrastructure client responsible for refreshing Mercado Libre OAuth tokens.
    """

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def refresh(self, connection: OAuthConnection) -> OAuthConnection:
        form_data = urlencode({
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": connection.refresh_token,
        }).encode("utf-8")

        token_request = Request(
            TOKEN_URL,
            data=form_data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        try:
            with urlopen(token_request, timeout=20) as response:
                token_data = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MercadoLibreOAuthError(
                f"Mercado Libre token refresh failed: HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise MercadoLibreOAuthError(
                "Mercado Libre token service unavailable"
            ) from exc

        try:
            token_payload = json.loads(token_data)
        except json.JSONDecodeError as exc:
            raise MercadoLibreOAuthError(
                "Mercado Libre returned invalid token JSON"
            ) from exc

        access_token = token_payload.get("access_token")
        refresh_token = token_payload.get("refresh_token")
        expires_in = token_payload.get("expires_in")

        if not access_token or not refresh_token or not expires_in:
            raise MercadoLibreOAuthError(
                "Mercado Libre returned an incomplete token response"
            )

        from datetime import datetime, timedelta, timezone

        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(expires_in)
        )

        return OAuthConnection(
            provider=connection.provider,
            user_id=connection.user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scope=token_payload.get("scope", connection.scope),
            token_type=token_payload.get("token_type", connection.token_type),
        )
