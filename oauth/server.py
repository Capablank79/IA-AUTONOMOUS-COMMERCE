import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from src.domain.oauth.models import OAuthConnection
from src.infrastructure.persistence.data.json.oauth_connection_repository import (
    JsonOAuthConnectionRepository,
)

load_dotenv()

CLIENT_ID = os.getenv("MERCADOLIBRE_CLIENT_ID")
CLIENT_SECRET = os.getenv("MERCADOLIBRE_CLIENT_SECRET")
REDIRECT_URI = "https://auth.exesoft.cl/oauth/mercadolibre/callback"
AUTHORIZATION_URL = "https://auth.mercadolibre.cl/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

oauth_sessions = {}

oauth_repository = JsonOAuthConnectionRepository(
    "data/oauth"
)


async def health(request):
    return JSONResponse({
        "status": "ok",
        "service": "ai-autonomous-commerce",
        "environment": "development",
    })


async def oauth_login(request):
    if not CLIENT_ID:
        return JSONResponse(
            {"error": "MERCADOLIBRE_CLIENT_ID is not configured"},
            status_code=500,
        )

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)

    challenge = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode("ascii")

    oauth_sessions[state] = {
        "code_verifier": code_verifier,
    }

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    return RedirectResponse(
        f"{AUTHORIZATION_URL}?{urlencode(params)}",
        status_code=302,
    )


async def oauth_callback(request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return JSONResponse(
            {
                "error": "mercadolibre_authorization_failed",
                "detail": error,
            },
            status_code=400,
        )

    if not code or not state:
        return JSONResponse(
            {
                "error": "missing_code_or_state",
            },
            status_code=400,
        )

    session = oauth_sessions.pop(state, None)

    if not session:
        return JSONResponse(
            {
                "error": "invalid_or_expired_state",
            },
            status_code=400,
        )

    if not CLIENT_ID or not CLIENT_SECRET:
        return JSONResponse(
            {
                "error": "mercadolibre_credentials_not_configured",
            },
            status_code=500,
        )

    form_data = urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": session["code_verifier"],
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
        return JSONResponse(
            {
                "error": "mercadolibre_token_exchange_failed",
                "status": exc.code,
                "detail": detail,
            },
            status_code=502,
        )
    except URLError:
        return JSONResponse(
            {
                "error": "mercadolibre_token_service_unavailable",
            },
            status_code=502,
        )

    try:
        token_payload = json.loads(token_data)
    except json.JSONDecodeError:
        return JSONResponse(
            {
                "error": "mercadolibre_invalid_token_response",
            },
            status_code=502,
        )

    access_token = token_payload.get("access_token")
    refresh_token = token_payload.get("refresh_token")
    user_id = token_payload.get("user_id")
    expires_in = token_payload.get("expires_in")

    if not access_token or not refresh_token or not user_id or not expires_in:
        return JSONResponse(
            {
                "error": "mercadolibre_invalid_token_response",
            },
            status_code=502,
        )

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(expires_in)
    )

    connection = OAuthConnection(
        provider="mercadolibre",
        user_id=str(user_id),
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scope=token_payload.get("scope"),
        token_type=token_payload.get("token_type"),
    )

    oauth_repository.save(connection)

    return JSONResponse({
        "status": "authorized",
        "provider": connection.provider,
        "user_id": connection.user_id,
        "expires_at": connection.expires_at.isoformat(),
        "scope": connection.scope,
        "token_type": connection.token_type,
    })


app = Starlette(
    debug=False,
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/oauth/login", oauth_login, methods=["GET"]),
        Route(
            "/oauth/mercadolibre/callback",
            oauth_callback,
            methods=["GET"],
        ),
    ],
)
