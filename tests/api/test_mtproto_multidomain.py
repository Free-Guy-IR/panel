from fastapi import status

from app.core.mtproto import MTProtoConfig
from tests.api import client
from tests.api.helpers import auth_headers, delete_core, unique_name


def _mt_config(instances):
    return {"instances": instances}


def _create_mt_core(access_token, instances):
    payload = {
        "name": unique_name("mt"),
        "config": _mt_config(instances),
        "type": "mtproto",
        "exclude_inbound_tags": [],
        "fallbacks_inbound_tags": [],
    }
    response = client.post("/api/core", headers=auth_headers(access_token), json=payload)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _faketls(tag, port, **extra):
    return {"tag": tag, "port": port, **extra}


def test_mtproto_single_domain_round_trip():
    cfg = MTProtoConfig('{"instances": [{"tag": "one", "port": 8445, "fake_tls_domain": "example.org"}]}')
    meta = cfg.inbounds_by_tag["one"]

    assert meta["tls"] == "tls"
    assert meta["sni"] == "example.org"
    assert meta["finalmask"]["mtproto"]["fake_tls_domains"] == ["example.org"]
    assert meta["finalmask"]["mtproto"]["mode"] == "faketls"


def test_mtproto_domain_list_round_trip():
    cfg = MTProtoConfig(
        '{"instances": [{"tag": "many", "port": 443, "fake_tls_domains": ["a.example", "b.example", "c.example"]}]}'
    )
    meta = cfg.inbounds_by_tag["many"]

    assert meta["sni"] == "a.example"
    assert meta["finalmask"]["mtproto"]["fake_tls_domains"] == ["a.example", "b.example", "c.example"]


def test_mtproto_plain_mode_has_no_domain():
    cfg = MTProtoConfig('{"instances": [{"tag": "p", "port": 8449, "mode": "plain"}]}')
    meta = cfg.inbounds_by_tag["p"]

    assert meta["tls"] == "none"
    assert meta["sni"] == ""
    assert meta["finalmask"]["mtproto"]["fake_tls_domains"] == []


def test_mtproto_config_rejects_bad_shapes():
    bad = [
        '{"instances": [{"tag": "a", "port": 1}]}',
        '{"instances": [{"tag": "a", "port": 1, "fake_tls_domains": ["x.example", "x.example"]}]}',
        '{"instances": [{"tag": "a", "port": 1, "mode": "plain", "fake_tls_domains": ["x.example"]}]}',
        '{"instances": [{"tag": "a", "port": 1, "mode": "plain", "ad_tag": "aa"}]}',
        '{"instances": [{"tag": "a", "port": 1, "mode": "weird"}]}',
        '{"instances": [{"tag": "a", "port": 1, "fake_tls_domains": ["", " "]}]}',
    ]
    for raw in bad:
        try:
            MTProtoConfig(raw)
        except ValueError:
            continue
        raise AssertionError(f"should have been rejected: {raw}")


def test_registration_secret_requires_auth():
    response = client.get("/api/core/1/mtproto/anything/registration-secret")

    assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)


def test_registration_secret_unknown_tag(access_token):
    core = _create_mt_core(access_token, [_faketls("only", 8455, fake_tls_domain="example.org")])
    try:
        response = client.get(
            f"/api/core/{core['id']}/mtproto/missing/registration-secret",
            headers=auth_headers(access_token),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
    finally:
        delete_core(access_token, core["id"])


def test_registration_secret_for_faketls_and_plain(access_token):
    core = _create_mt_core(
        access_token,
        [
            _faketls("ft", 8456, fake_tls_domains=["first.example", "second.example"]),
            {"tag": "pl", "port": 8457, "mode": "plain"},
        ],
    )
    try:
        ft = client.get(
            f"/api/core/{core['id']}/mtproto/ft/registration-secret",
            headers=auth_headers(access_token),
        )
        pl = client.get(
            f"/api/core/{core['id']}/mtproto/pl/registration-secret",
            headers=auth_headers(access_token),
        )

        for response in (ft, pl):
            assert response.status_code in (status.HTTP_200_OK, status.HTTP_409_CONFLICT)

        if ft.status_code == status.HTTP_200_OK:
            body = ft.json()
            assert body["mode"] == "faketls"
            assert body["domain"] == "first.example"
            assert body["secret"].startswith("ee")
            assert body["secret"].endswith("first.example".encode("ascii").hex())

        if pl.status_code == status.HTTP_200_OK:
            body = pl.json()
            assert body["mode"] == "plain"
            assert body["domain"] is None
            assert body["secret"].startswith("dd")
            assert len(body["secret"]) == 34
    finally:
        delete_core(access_token, core["id"])
