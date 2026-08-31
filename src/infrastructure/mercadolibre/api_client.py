import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.mercadolibre.com"


class MercadoLibreApiError(Exception):
    """Raised when Mercado Libre API requests fail."""
    def __init__(self, message: str, status_code: int = None, response_body: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class MercadoLibreApiClient:
    """
    HTTP client for authenticated Mercado Libre API requests.
    """

    def __init__(self, access_token: str):
        self.access_token = access_token

    def get(self, path: str) -> dict:
        return self._request(path, method="GET")

    def post(self, path: str, payload: dict) -> dict:
        return self._request(path, method="POST", payload=payload)

    def put(self, path: str, payload: dict) -> dict:
        return self._request(path, method="PUT", payload=payload)

    def _request(self, path: str, method: str = "GET", payload: dict = None) -> dict:
        url = f"{API_BASE_URL}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MercadoLibreApiError(
                f"Mercado Libre API request failed: HTTP {exc.code}: {detail}",
                status_code=exc.code,
                response_body=detail,
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
