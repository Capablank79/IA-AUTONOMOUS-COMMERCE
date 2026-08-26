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
