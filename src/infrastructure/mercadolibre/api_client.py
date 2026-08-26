import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.mercadolibre.com"


class MercadoLibreApiError(Exception):
    """Raised when Mercado Libre API requests fail."""
    pass


class MercadoLibreApiClient:
    """
    HTTP client for authenticated Mercado Libre API requests.
    """

    def __init__(self, access_token: str):
        self.access_token = access_token

    def get(self, path: str) -> dict:
        url = f"{API_BASE_URL}{path}"

        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
            },
            method="GET",
        )

        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MercadoLibreApiError(
                f"Mercado Libre API request failed: HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise MercadoLibreApiError(
                "Mercado Libre API unavailable"
            ) from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise MercadoLibreApiError(
                "Mercado Libre API returned invalid JSON"
            ) from exc
