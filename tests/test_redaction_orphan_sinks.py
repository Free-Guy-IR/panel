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
