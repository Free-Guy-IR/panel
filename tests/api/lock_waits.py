import asyncio

from sqlalchemy import text

LOCK_WAITS = {
    "postgresql": (
        "select count(*) from pg_stat_activity where wait_event_type = 'Lock' and datname = current_database()"
    ),
    "mysql": (
        "select count(*) from information_schema.processlist "
        "where command = 'Query' and time >= 1 and id <> connection_id() and db = database()"
    ),
}


async def a_lock_wait_appears(engine, hold_seconds, timeout_seconds=15.0):
    query = LOCK_WAITS.get(engine.dialect.name)
    if query is None:
        await asyncio.sleep(hold_seconds)
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        async with engine.connect() as conn:
            if (await conn.execute(text(query))).scalar_one():
                return True
        await asyncio.sleep(0.05)
    return False
