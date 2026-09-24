import socket

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.fork.proxy_secrets.locks import side_engine


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.asyncio
async def test_postgres_side_engine_reaches_the_network():
    request_engine = create_async_engine(f"postgresql+asyncpg://u:p@127.0.0.1:{_closed_port()}/pasarguard")
    side = side_engine(request_engine.sync_engine)
    try:
        async with side.connect():
            pass
    except Exception as exc:
        failure = exc
    else:
        failure = None
    await side.dispose()
    await request_engine.dispose()
    assert not isinstance(failure, TypeError), repr(failure)
