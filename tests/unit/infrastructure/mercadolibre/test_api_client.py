from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)


def test_get_sends_bearer_token(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def read(self):
            return b'{"id":55197108,"nickname":"test-user"}'

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["method"] = request.method
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "src.infrastructure.mercadolibre.api_client.urlopen",
        fake_urlopen,
    )

    client = MercadoLibreApiClient("test-access-token")

    result = client.get("/users/me")

    assert captured["url"] == "https://api.mercadolibre.com/users/me"
    assert captured["authorization"] == "Bearer test-access-token"
    assert captured["method"] == "GET"
    assert captured["timeout"] == 20
    assert result["id"] == 55197108


def test_get_raises_on_invalid_json(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def read(self):
            return b"not-json"

    monkeypatch.setattr(
        "src.infrastructure.mercadolibre.api_client.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    client = MercadoLibreApiClient("test-access-token")

    import pytest

    with pytest.raises(MercadoLibreApiError):
        client.get("/users/me")


def test_post_sends_payload_and_json_headers(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def read(self):
            return b'{"id":"MLC123","status":"active"}'

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["content_type"] = request.get_header("Content-type")
        captured["method"] = request.method
        captured["data"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "src.infrastructure.mercadolibre.api_client.urlopen",
        fake_urlopen,
    )

    client = MercadoLibreApiClient("test-access-token")

    payload = {"title": "Test Product", "price": 1000}
    result = client.post("/items", payload=payload)

    assert captured["url"] == "https://api.mercadolibre.com/items"
    assert captured["authorization"] == "Bearer test-access-token"
    assert captured["content_type"] == "application/json"
    assert captured["method"] == "POST"
    assert b'"title": "Test Product"' in captured["data"]
    assert result["id"] == "MLC123"
    assert result["status"] == "active"
