import asyncio
import gzip
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sse_starlette.sse import EventSourceResponse

from app import middlewares
from app.middlewares.gzip import PanelGZipMiddleware
from config import subscription_env_settings

SUB_PREFIX = "/" + subscription_env_settings.path.strip("/")
SUB_TOKEN = "dXNlcm5hbWUsMTcwMDAwMDAwMA.c2lnbmF0dXJl"
SUB_PAGE = "<html>" + "<div class='config'>vless://node.example.com:443</div>" * 300 + "</html>"
SUB_LINKS = "\n".join(f"vless://{index:032d}@edge{index}.example.com:443#node-{index}" for index in range(80))


def _client(monkeypatch, tmp_path=None) -> TestClient:
    settings = SimpleNamespace(
        gzip_enabled=True, gzip_minimum_size=1000, proxy_headers=False, forwarded_allow_ips="127.0.0.1"
    )
    monkeypatch.setattr(middlewares, "server_settings", settings)
    app = FastAPI()
    middlewares.setup_middleware(app)
    app.get(f"{SUB_PREFIX}/{{token}}")(lambda token: HTMLResponse(SUB_PAGE))
    app.get(f"{SUB_PREFIX}/{{token}}/links")(lambda token: PlainTextResponse(SUB_LINKS))
    if tmp_path is not None:
        target = tmp_path / "bundle.js"
        target.write_bytes(b"console.log('panel');\n" * 400)
        app.get("/bundle.js")(lambda: FileResponse(target))
        app.mount("/static", StaticFiles(directory=tmp_path))

    async def events():
        yield {"data": "y" * 4000}

    app.get("/api/node/1/logs")(lambda: EventSourceResponse(events()))
    return TestClient(app)


def _raw(client: TestClient, path: str, accept_encoding: str | None = "gzip", **headers: str):
    request_headers = dict(headers)
    if accept_encoding is None:
        client.headers.pop("accept-encoding", None)
    else:
        request_headers["Accept-Encoding"] = accept_encoding
    with client.stream("GET", path, headers=request_headers) as response:
        return response, b"".join(response.iter_raw())


@pytest.mark.parametrize(("suffix", "expected"), [("", SUB_PAGE), ("/links", SUB_LINKS)])
def test_subscription_bodies_are_gzipped_and_decode_to_the_identity_bytes(monkeypatch, suffix, expected):
    client = _client(monkeypatch)
    path = f"{SUB_PREFIX}/{SUB_TOKEN}{suffix}"
    gzipped, gzipped_body = _raw(client, path)
    identity, identity_body = _raw(client, path, accept_encoding="identity")

    assert gzipped.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in identity.headers
    assert gzip.decompress(gzipped_body) == identity_body == expected.encode()
    assert gzipped.headers["content-security-policy"] == identity.headers["content-security-policy"]
    assert gzipped.headers["x-content-type-options"] == "nosniff"
    assert "Accept-Encoding" in gzipped.headers["vary"]


def test_subscription_without_accept_encoding_is_identity(monkeypatch):
    client = _client(monkeypatch)
    response, body = _raw(client, f"{SUB_PREFIX}/{SUB_TOKEN}/links", accept_encoding=None)

    assert "content-encoding" not in response.headers
    assert body == SUB_LINKS.encode()


@pytest.mark.parametrize(
    "accept_encoding", ["gzip;q=0", "GZIP; Q=0", "gzip;q=0.000", "br, gzip;q=0", "*;q=1, gzip;q=0"]
)
def test_gzip_with_zero_quality_is_refused(monkeypatch, accept_encoding):
    response, body = _raw(_client(monkeypatch), f"{SUB_PREFIX}/{SUB_TOKEN}", accept_encoding=accept_encoding)

    assert "content-encoding" not in response.headers
    assert body == SUB_PAGE.encode()


@pytest.mark.parametrize("accept_encoding", ["gzip", "identity"])
def test_range_request_is_served_uncompressed(monkeypatch, tmp_path, accept_encoding):
    client = _client(monkeypatch, tmp_path)
    full = (tmp_path / "bundle.js").read_bytes()
    response, body = _raw(client, "/bundle.js", accept_encoding=accept_encoding, Range="bytes=100-299")

    assert response.status_code == 206
    assert "content-encoding" not in response.headers
    assert response.headers["content-range"] == f"bytes 100-299/{len(full)}"
    assert body == full[100:300]


def test_compressed_file_has_weak_etag_and_revalidates_with_304(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    gzipped, _ = _raw(client, "/static/bundle.js")
    identity, _ = _raw(client, "/static/bundle.js", accept_encoding="identity")

    assert identity.headers["etag"].startswith('"')
    assert gzipped.headers["etag"] == f"W/{identity.headers['etag']}"

    weak, weak_body = _raw(client, "/static/bundle.js", **{"If-None-Match": gzipped.headers["etag"]})
    strong, strong_body = _raw(
        client, "/static/bundle.js", accept_encoding="identity", **{"If-None-Match": identity.headers["etag"]}
    )

    assert weak.status_code == strong.status_code == 304
    assert weak_body == strong_body == b""
    assert weak.headers["etag"] == gzipped.headers["etag"]
    assert strong.headers["etag"] == identity.headers["etag"]
    assert "content-encoding" not in weak.headers


def test_sse_route_is_never_compressed(monkeypatch):
    response, body = _raw(_client(monkeypatch), "/api/node/1/logs")

    assert response.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in response.headers
    assert body.count(b"y" * 4000) == 1


@pytest.mark.parametrize("content_type", [b"text/event-stream", b"text/event-stream; charset=utf-8"])
def test_sse_chunks_pass_through_one_by_one_without_buffering(content_type):
    chunks = [b"data: " + bytes([65 + index]) * 3000 + b"\n\n" for index in range(4)]
    timeline: list[tuple[str, int]] = []
    sent: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", content_type)]})
        for index, chunk in enumerate(chunks):
            timeline.append(("produced", index))
            await send({"type": "http.response.body", "body": chunk, "more_body": index < len(chunks) - 1})

    async def send(message):
        if message["type"] == "http.response.body":
            timeline.append(("delivered", len(sent)))
            sent.append(message)
        else:
            sent_headers.extend(message["headers"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent_headers: list[tuple[bytes, bytes]] = []
    scope = {"type": "http", "method": "GET", "path": "/logs", "headers": [(b"accept-encoding", b"gzip")]}
    asyncio.run(PanelGZipMiddleware(app, minimum_size=1, compresslevel=6)(scope, receive, send))

    assert [message["body"] for message in sent] == chunks
    assert timeline == [event for index in range(len(chunks)) for event in (("produced", index), ("delivered", index))]
    assert b"content-encoding" not in {name for name, _ in sent_headers}
