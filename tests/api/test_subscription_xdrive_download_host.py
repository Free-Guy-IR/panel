import base64

import pytest
import yaml

from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)

XDRIVE_SECRET = "h3-xdrive-download-secret"
XDRIVE_SETTINGS = {"remoteFolder": "pg-test", "service": "gdrive", "secrets": [XDRIVE_SECRET], "segmentBytes": 65536}
INBOUND_DOWNLOAD_SETTINGS = {
    "address": "198.51.100.32",
    "port": 18546,
    "network": "xdrive",
    "xdriveSettings": XDRIVE_SETTINGS,
}

NON_XRAY_LINK_FORMATS = ["links", "links_base64", "sing_box", "clash_meta"]
NON_XRAY_OTHER_FORMATS = ["clash", "outline", "wireguard", "openvpn", "l2tp"]


def _core_config(tags: dict[str, str]) -> dict:
    return {
        "inbounds": [
            {
                "tag": tags["xdrive"],
                "protocol": "vless",
                "port": 18543,
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "xdrive", "xdriveSettings": XDRIVE_SETTINGS},
            },
            {
                "tag": tags["xhttp"],
                "protocol": "vless",
                "port": 18544,
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "xhttp", "xhttpSettings": {"path": "/xh", "mode": "auto"}},
            },
            {
                "tag": tags["xhttp_inbound_download"],
                "protocol": "vless",
                "port": 18547,
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {
                    "network": "xhttp",
                    "xhttpSettings": {
                        "path": "/xd",
                        "mode": "auto",
                        "extra": {"downloadSettings": INBOUND_DOWNLOAD_SETTINGS},
                    },
                },
            },
            {
                "tag": tags["tcp"],
                "protocol": "vless",
                "port": 18545,
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "tcp"},
            },
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }


@pytest.fixture
def xdrive_download_chain(access_token):
    headers = auth_headers(access_token)
    tags = {
        "xdrive": unique_name("xdh_in"),
        "xhttp": unique_name("xhh_in"),
        "xhttp_inbound_download": unique_name("xhd_in"),
        "tcp": unique_name("tch_in"),
    }
    core = create_core(access_token, config=_core_config(tags))
    group = create_group(access_token, inbound_tags=list(tags.values()))
    user = create_user(access_token, group_ids=[group["id"]])
    remarks = {
        "xdh": unique_name("xdh"),
        "xhh": unique_name("xhh"),
        "xhd": unique_name("xhd"),
        "xhn": unique_name("xhn"),
        "tch": unique_name("tch"),
    }
    host_ids = []

    def _post_host(payload: dict) -> int:
        response = client.post("/api/host", headers=headers, json=payload)
        assert response.status_code == 201, response.text
        host_ids.append(response.json()["id"])
        return host_ids[-1]

    try:
        xdh_id = _post_host(
            {"remark": remarks["xdh"], "address": ["198.51.100.31"], "inbound_tag": tags["xdrive"], "priority": 1}
        )
        _post_host(
            {
                "remark": remarks["xhh"],
                "address": ["198.51.100.30"],
                "inbound_tag": tags["xhttp"],
                "priority": 2,
                "transport_settings": {"xhttp_settings": {"download_settings": xdh_id}},
            }
        )
        xhd_id = _post_host(
            {
                "remark": remarks["xhd"],
                "address": ["198.51.100.33"],
                "inbound_tag": tags["xhttp_inbound_download"],
                "priority": 3,
            }
        )
        _post_host(
            {
                "remark": remarks["xhn"],
                "address": ["198.51.100.34"],
                "inbound_tag": tags["xhttp"],
                "priority": 4,
                "transport_settings": {"xhttp_settings": {"download_settings": xhd_id}},
            }
        )
        _post_host({"remark": remarks["tch"], "address": ["198.51.100.30"], "inbound_tag": tags["tcp"], "priority": 5})
        yield user, remarks
    finally:
        for host_id in host_ids:
            client.delete(f"/api/host/{host_id}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def _body(response, client_type: str) -> str:
    if client_type == "links_base64":
        return base64.b64decode(response.text).decode()
    return response.content.decode(errors="replace")


@pytest.mark.parametrize("client_type", NON_XRAY_LINK_FORMATS)
def test_host_with_xdrive_download_leg_is_skipped_outside_xray(xdrive_download_chain, client_type):
    user, remarks = xdrive_download_chain
    response = client.get(f"{user['subscription_url']}/{client_type}")
    assert response.status_code == 200, response.text

    body = _body(response, client_type)
    assert XDRIVE_SECRET not in body
    assert "xdrive" not in body
    assert remarks["xdh"] not in body
    assert remarks["xhh"] not in body
    assert remarks["xhd"] not in body
    assert remarks["tch"] in body


@pytest.mark.parametrize("client_type", ["links", "links_base64", "sing_box"])
def test_formats_that_nest_the_download_host_leg_skip_a_host_whose_download_host_nests_xdrive(
    xdrive_download_chain, client_type
):
    user, remarks = xdrive_download_chain
    response = client.get(f"{user['subscription_url']}/{client_type}")
    assert response.status_code == 200, response.text

    body = _body(response, client_type)
    assert XDRIVE_SECRET not in body
    assert "xdrive" not in body
    assert remarks["xhn"] not in body
    assert remarks["tch"] in body


def test_clash_meta_keeps_a_host_whose_download_host_only_nests_an_xdrive_leg(xdrive_download_chain):
    user, remarks = xdrive_download_chain
    response = client.get(f"{user['subscription_url']}/clash_meta")
    assert response.status_code == 200, response.text

    assert XDRIVE_SECRET not in response.text
    assert "xdrive" not in response.text
    proxies = {proxy["name"]: proxy for proxy in yaml.safe_load(response.text)["proxies"]}
    assert remarks["xhh"] not in proxies
    assert remarks["xhd"] not in proxies
    assert remarks["xhn"] in proxies
    download = proxies[remarks["xhn"]]["xhttp-opts"]["download-settings"]
    assert download["server"] == "198.51.100.33"
    assert download["port"] == 18547
    assert download["path"] == "/xd"


@pytest.mark.parametrize("client_type", NON_XRAY_OTHER_FORMATS)
def test_formats_without_vless_never_carry_xdrive_download_secrets(xdrive_download_chain, client_type):
    user, remarks = xdrive_download_chain
    response = client.get(f"{user['subscription_url']}/{client_type}")
    assert response.status_code == 200, response.text

    body = _body(response, client_type)
    assert XDRIVE_SECRET not in body
    assert remarks["xhh"] not in body
    assert remarks["xhd"] not in body
    assert remarks["xhn"] not in body


def test_xray_keeps_the_xdrive_download_leg(xdrive_download_chain):
    user, remarks = xdrive_download_chain
    response = client.get(f"{user['subscription_url']}/xray")
    assert response.status_code == 200, response.text

    configs = {config.get("remarks"): config for config in response.json()}
    assert remarks["xdh"] in configs
    assert remarks["tch"] in configs

    for remark, address in ((remarks["xhh"], "198.51.100.31"), (remarks["xhd"], "198.51.100.32")):
        assert remark in configs
        streams = [
            outbound["streamSettings"]
            for outbound in configs[remark].get("outbounds", [])
            if isinstance(outbound.get("streamSettings"), dict)
        ]
        xhttp_streams = [stream for stream in streams if stream.get("network") == "xhttp"]
        assert len(xhttp_streams) == 1
        download = xhttp_streams[0]["xhttpSettings"]["extra"]["downloadSettings"]
        assert download["network"] == "xdrive"
        assert download["address"] == address
        assert download["xdriveSettings"]["secrets"] == [XDRIVE_SECRET]

    assert remarks["xhn"] in configs
