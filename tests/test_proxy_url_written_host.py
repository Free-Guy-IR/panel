import datetime
from functools import cache

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from app.models.node import NodeCreate, NodeModify
from app.models.settings import SettingsSchema, SettingsSchemaInput

PASSWORD = "s" + "3" + "cr" + "et" + "PW"
PUNYCODE = "xn--bcher-kva.example"
UNUSABLE = [
    pytest.param(f"http://us\\er:{PASSWORD}@proxy.example.com:9", id="backslash-in-user"),
    pytest.param(f"http://user:{PASSWORD}\\x@proxy.example.com:9", id="backslash-in-password"),
    pytest.param(f"http://us]er:s3c[{PASSWORD}@proxy.example.com:9", id="bracket-user-and-password"),
    pytest.param(f"socks5://us\\er:{PASSWORD}@proxy.example.com:1080", id="backslash-socks5"),
    pytest.param(f"http://us/er:{PASSWORD}@proxy.example.com:9", id="slash-sends-to-another-host"),
    pytest.param(f"http://us?er:{PASSWORD}@proxy.example.com:9", id="question-mark-sends-to-another-host"),
    pytest.param(f"http://us#er:{PASSWORD}@proxy.example.com:9", id="hash-sends-to-another-host"),
    pytest.param(f"http://user:{PASSWORD}@proxy.example.com:3128\n", id="trailing-newline"),
    pytest.param(f"http://user:{PASSWORD}@proxy.example.com:٣١٢٨", id="arabic-indic-port"),
    pytest.param(f"socks5://user:{PASSWORD}@proxy.example.com:١٠٨٠", id="arabic-indic-socks-port"),
]
USABLE = [
    "http://proxy.example.com:8080",
    "https://proxy.example.com:443",
    "socks4://proxy.example.com:1080",
    "socks5://proxy.example.com:1080",
    "http://user:pass@proxy.example.com:3128",
    "https://user:pass@proxy.example.com:8443",
    "socks5://user:pass@proxy.example.com:1080",
    "socks4://user:pass@proxy.example.com:1080",
    "http://User:Pa55-word_x.y~z@Proxy.Example.COM:3128",
    "http://us%5Cer:p%5Bass%5D@proxy.example.com:3128",
    "http://user:p%40ss@proxy.example.com:3128",
    "http://user:pa!$&'()*+,;=ss@proxy.example.com:3128",
    f"http://user:pw@{PUNYCODE}:3128",
    f"https://user:pw@{PUNYCODE}:443",
    f"socks5://user:pw@{PUNYCODE}:1080",
    "http://user:pw@XN--BCHER-KVA.example:3128",
]


def _settings(proxy_url: str) -> dict:
    return {
        "webhook": {"timeout": 180, "recurrent": 3, "proxy_url": proxy_url},
        "notification_settings": {"max_retries": 3, "proxy_url": proxy_url},
    }


@cache
def _server_ca() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node.example.com")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=90))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def _node(proxy_url: str) -> dict:
    return {
        "name": "proxy-node",
        "address": "node.example.com",
        "port": 62050,
        "connection_type": "grpc",
        "server_ca": _server_ca(),
        "keep_alive": 60,
        "core_config_id": 1,
        "api_key": "11111111-1111-4111-8111-111111111111",
        "proxy_url": proxy_url,
    }


@pytest.mark.parametrize("proxy_url", UNUSABLE)
@pytest.mark.parametrize("section", ["webhook", "notification_settings"])
def test_saving_a_settings_proxy_whose_parsed_target_differs_from_the_written_one_is_refused(section, proxy_url):
    with pytest.raises(ValidationError) as refused:
        SettingsSchemaInput.model_validate({section: _settings(proxy_url)[section]})
    assert PASSWORD not in str(refused.value.errors(include_input=False))


@pytest.mark.parametrize("proxy_url", UNUSABLE)
def test_a_node_cannot_be_created_or_modified_with_a_proxy_whose_parsed_target_differs(proxy_url):
    with pytest.raises(ValidationError):
        NodeModify.model_validate({"proxy_url": proxy_url})
    with pytest.raises(ValidationError):
        NodeCreate.model_validate(_node(proxy_url))


@pytest.mark.parametrize("proxy_url", USABLE)
def test_every_usable_proxy_form_including_a_punycode_host_still_saves_unchanged(proxy_url):
    saved = SettingsSchemaInput.model_validate(_settings(proxy_url))
    assert saved.webhook.proxy_url == proxy_url
    assert saved.notification_settings.proxy_url == proxy_url
    assert NodeModify.model_validate({"proxy_url": proxy_url}).proxy_url == proxy_url


@pytest.mark.parametrize(
    "proxy_url", [f"http://user:pw@{PUNYCODE}:3128", "socks5://user:pw@XN--BCHER-KVA.example:1080"]
)
def test_a_punycode_proxy_host_is_accepted_through_node_create(proxy_url):
    assert NodeCreate.model_validate(_node(proxy_url)).proxy_url == proxy_url


@pytest.mark.parametrize("proxy_url", USABLE)
def test_a_stored_usable_proxy_loads_unchanged(proxy_url):
    loaded = SettingsSchema.model_validate(_settings(proxy_url))
    assert loaded.webhook.proxy_url == proxy_url
    assert loaded.notification_settings.proxy_url == proxy_url


def test_the_written_target_check_compares_the_raw_punycode_host():
    from app.fork import proxy_url

    for value in (f"http://user:pw@{PUNYCODE}:3128", "http://user:pw@XN--BCHER-KVA.example:3128"):
        assert proxy_url.proxy_url_reaches_its_host(value) == value


@pytest.mark.parametrize(
    "value", ["http://u:p@h.example:٣١٢٨", "https://u:p@h.example:31٠28", "socks5://u:p@h.example:١٠٨٠"]
)
def test_the_written_target_check_refuses_a_port_in_other_digits(value):
    from app.fork import proxy_url

    with pytest.raises(ValueError) as refused:
        proxy_url.proxy_url_reaches_its_host(value)
    assert str(refused.value) == proxy_url.PROXY_PORT_OUT_OF_RANGE
