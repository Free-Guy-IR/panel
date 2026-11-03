from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PasarGuardNodeBridge import NodeAPIError

from app.fork.content_filter import service
from app.fork.content_filter.service import EnforcementError
from app.fork.routers import content_filter as router
from app.telegram.handlers import error_handler

PASSWORD = "Pr0xy" + "S3cret"
LEAK = f"Cannot connect to proxy socks5://user:{PASSWORD}@proxy.example:1080"


@pytest.mark.asyncio
async def test_a_content_filter_probe_error_never_returns_the_proxy_password(monkeypatch):
    node = SimpleNamespace(test_route=AsyncMock(side_effect=NodeAPIError(-1, LEAK)))
    monkeypatch.setattr(service.node_manager, "get_node", AsyncMock(return_value=node))
    outcome = await service.probe(1, "in", "example.com")
    assert outcome.startswith("error:")
    assert PASSWORD not in outcome


def test_a_content_filter_refusal_never_answers_with_the_proxy_password():
    with pytest.raises(HTTPException) as refused:
        router._fail(EnforcementError(LEAK, code=503))
    assert PASSWORD not in str(refused.value.detail)


@pytest.mark.asyncio
async def test_a_telegram_error_reply_never_carries_the_proxy_password_or_bot_token():
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    for exception in (ValueError(LEAK), ValueError(f"InvalidURL https://api.telegram.org/bot{token}/getMe")):
        message = SimpleNamespace(answer=AsyncMock(), chat=SimpleNamespace(id=1))
        callback = SimpleNamespace(answer=AsyncMock())
        for update in (
            SimpleNamespace(message=message, callback_query=None),
            SimpleNamespace(message=None, callback_query=callback),
        ):
            await error_handler.handle_exception(SimpleNamespace(update=update, exception=exception), None)
        replies = [call.args[0] for call in message.answer.await_args_list + callback.answer.await_args_list]
        assert len(replies) == 2
        for reply in replies:
            assert PASSWORD not in reply
            assert token not in reply
