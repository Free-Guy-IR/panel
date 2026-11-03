import gzip

import pytest

from app.subscription.config_cache import clear_sub_config_cache
from tests.api import client
from tests.api.helpers import (
    create_core,
    create_group,
    create_hosts_for_inbounds,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)


@pytest.fixture
def subscriber(access_token):
    clear_sub_config_cache()
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("gzip_group"))
    hosts = create_hosts_for_inbounds(access_token, address=[f"edge{index}.example.com" for index in range(12)])
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("gzip")})
    yield user
    delete_user(access_token, user["username"])
    for host in hosts:
        client.delete(f"/api/host/{host['id']}", headers={"Authorization": f"Bearer {access_token}"})
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def _raw(path: str, accept_encoding: str | None, **headers: str):
    request_headers = dict(headers)
    if accept_encoding is not None:
        request_headers["Accept-Encoding"] = accept_encoding
    saved = client.headers.pop("accept-encoding", None)
    try:
        with client.stream("GET", path, headers=request_headers) as response:
            return response, b"".join(response.iter_raw())
    finally:
        if saved is not None:
            client.headers["accept-encoding"] = saved


@pytest.mark.parametrize("suffix", ["/links", "/links_base64", "/xray", "/clash", "/sing_box"])
def test_real_subscription_routes_decode_to_the_identity_body(subscriber, suffix):
    path = f"{subscriber['subscription_url']}{suffix}"
    identity, identity_body = _raw(path, "identity")
    gzipped, gzipped_body = _raw(path, "gzip")
    plain, plain_body = _raw(path, None)

    assert identity.status_code == gzipped.status_code == plain.status_code == 200
    assert "content-encoding" not in identity.headers
    assert "content-encoding" not in plain.headers
    decoded = gzip.decompress(gzipped_body) if gzipped.headers.get("content-encoding") == "gzip" else gzipped_body
    assert decoded == identity_body == plain_body
    if len(identity_body) >= 1000:
        assert gzipped.headers["content-encoding"] == "gzip"
        assert "Accept-Encoding" in gzipped.headers["vary"]
    assert gzipped.headers.get("subscription-userinfo") == identity.headers.get("subscription-userinfo")


def test_real_subscription_page_keeps_security_headers_when_gzipped(subscriber):
    path = subscriber["subscription_url"]
    identity, identity_body = _raw(path, "identity", Accept="text/html")
    gzipped, gzipped_body = _raw(path, "gzip", Accept="text/html")

    assert identity.status_code == gzipped.status_code == 200
    decoded = gzip.decompress(gzipped_body) if gzipped.headers.get("content-encoding") == "gzip" else gzipped_body
    assert decoded == identity_body
    assert gzipped.headers.get("content-security-policy") == identity.headers.get("content-security-policy")
    assert gzipped.headers.get("x-content-type-options") == "nosniff"
