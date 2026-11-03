from app.fork.content_filter.schemas import AssignmentResponse
from app.fork.operation.node_messages import scrub_credentials
from app.models.node import NodeResponse

BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
PASSWORD = "Pr0xy" + "S3cret"


def test_a_telegram_bot_token_in_an_api_url_is_masked():
    text = f"Cannot connect to host https://api.telegram.org/bot{BOT_TOKEN}/sendMessage ssl:default"
    scrubbed = scrub_credentials(text)
    assert BOT_TOKEN not in scrubbed
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in scrubbed
    assert "api.telegram.org/bot***/sendMessage" in scrubbed


def test_a_bare_bot_path_and_a_token_at_the_end_of_text_are_masked():
    for text in (f"InvalidURL /bot{BOT_TOKEN}/getMe", f"failed: https://api.telegram.org/bot{BOT_TOKEN}"):
        assert BOT_TOKEN not in scrub_credentials(text)


def test_bot_token_masking_keeps_ordinary_text():
    assert scrub_credentials("robot 12:30 is fine") == "robot 12:30 is fine"
    assert scrub_credentials("/bothandler/x") == "/bothandler/x"


def _node_response(message: str) -> NodeResponse:
    return NodeResponse.model_validate(
        {
            "id": 1,
            "name": "n",
            "address": "1.2.3.4",
            "port": 62050,
            "api_port": 62051,
            "api_key": None,
            "core_config_id": None,
            "xray_version": None,
            "node_version": None,
            "status": "error",
            "message": message,
            "usage_coefficient": 1,
            "connection_type": "grpc",
            "server_ca": "",
            "keep_alive": 60,
        }
    )


def test_a_node_message_stored_before_the_scrub_is_served_without_the_password():
    response = _node_response(f"Cannot connect to proxy socks5://user:{PASSWORD}@proxy.example:1080")
    assert PASSWORD not in (response.message or "")


def test_an_assignment_error_stored_before_the_scrub_is_served_without_the_password():
    response = AssignmentResponse.model_validate(
        {
            "id": 1,
            "profile_id": 1,
            "node_id": 1,
            "inbound_tag": "in",
            "is_enabled": True,
            "enforced": False,
            "last_checked_at": None,
            "delivery": "node",
            "reaches_nodes": [1],
            "last_error": f"node 1: cannot reach http://user:{PASSWORD}@proxy.example:8080",
        }
    )
    assert PASSWORD not in (response.last_error or "")


def _connection_key(login: str, password: str):
    import warnings

    import aiohttp
    from aiohttp.client_reqrep import ConnectionKey
    from yarl import URL

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        auth = aiohttp.BasicAuth(login, password)
    return ConnectionKey("node.example.com", 443, True, True, URL("http://127.0.0.1:3128"), auth, None)


def test_a_basic_auth_repr_never_keeps_the_password():
    for login, password in (
        ("user", PASSWORD),
        ("us'er", f"p'{PASSWORD}"),
        ("user", f'q"{PASSWORD})'),
        ("user", f"x, encoding='latin1') {PASSWORD}"),
        ("user", f"{PASSWORD}\\"),
    ):
        key = _connection_key(login, password)
        for text in (repr(key), f"Unclosed connection: Connection<{key}>", repr(repr(key)), f"{key}\n{key}"):
            scrubbed = scrub_credentials(text)
            assert PASSWORD not in scrubbed, scrubbed
            assert "BasicAuth(" in scrubbed
    assert scrub_credentials("BasicAuth is deprecated") == "BasicAuth is deprecated"


def test_a_url_cut_before_its_at_sign_never_keeps_the_start_of_the_password():
    from pydantic import BaseModel, ValidationError

    class _Model(BaseModel):
        url: int

    try:
        _Model(url=f"http://someuser:{PASSWORD}rest@" + "x" * 80 + ".example.com:3128")
    except ValidationError as refused:
        text = str(refused)
    assert "..." in text
    scrubbed = scrub_credentials(text)
    assert PASSWORD not in scrubbed
    assert "Input should be a valid integer" in scrubbed
    for cut in (f"cut http://user:{PASSWORD}.", f"cut (http://user:{PASSWORD})", f"cut socks5://user:{PASSWORD}"):
        assert PASSWORD not in scrub_credentials(cut), cut


def test_an_address_with_a_port_is_kept():
    for text in (
        "Cannot connect to http://1.2.3.4:3128.",
        "detail=Connection error: http://10.0.0.5:62050)",
        "proxy http://[::1]:8080;",
        "see http://10.0.0.5:62050/path).",
        "see http://example.com/docs and more",
        "waiting... http://h.example.com:80 is up",
    ):
        assert scrub_credentials(text) == text
