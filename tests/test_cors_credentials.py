from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import middlewares

EVIL = "https://evil.example"
PANEL = "https://panel.example"


def _client(monkeypatch: pytest.MonkeyPatch, origins: list[str]) -> TestClient:
    monkeypatch.setattr(middlewares, "cors_settings", SimpleNamespace(allowed_origins=origins))
    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    middlewares.setup_middleware(app)
    return TestClient(app)


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/api/ping",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
    )


def test_a_wildcard_origin_never_answers_with_credentials_for_a_foreign_site(monkeypatch):
    client = _client(monkeypatch, ["*"])

    preflight = _preflight(client, EVIL)
    simple = client.get("/api/ping", headers={"Origin": EVIL})

    for response in (preflight, simple):
        assert response.headers.get("access-control-allow-credentials") != "true"
        assert response.headers.get("access-control-allow-origin") != EVIL


def test_a_wildcard_origin_still_serves_cross_origin_reads_without_credentials(monkeypatch):
    client = _client(monkeypatch, ["*"])

    response = client.get("/api/ping", headers={"Origin": EVIL})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"


def test_explicit_origins_keep_credentials_for_the_listed_origin_only(monkeypatch):
    client = _client(monkeypatch, [PANEL])

    allowed = _preflight(client, PANEL)
    refused = _preflight(client, EVIL)

    assert allowed.headers.get("access-control-allow-origin") == PANEL
    assert allowed.headers.get("access-control-allow-credentials") == "true"
    assert refused.headers.get("access-control-allow-origin") != EVIL
