import time
from collections.abc import Awaitable, Callable

import nats
from nats.js.client import JetStreamContext
from nats.js.kv import KeyValue

from app.utils.logger import get_logger

from . import get_nats_config, is_nats_enabled

logger = get_logger("nats-client")

RECONNECT_TIME_WAIT_S = 2
RECONNECT_FOREVER = -1
ERROR_LOG_INTERVAL_S = 60.0

ReconnectListener = Callable[[nats.NATS], Awaitable[None]]
_reconnect_listeners: list[ReconnectListener] = []


def add_reconnect_listener(listener: ReconnectListener) -> None:
    if listener not in _reconnect_listeners:
        _reconnect_listeners.append(listener)


def remove_reconnect_listener(listener: ReconnectListener) -> None:
    if listener in _reconnect_listeners:
        _reconnect_listeners.remove(listener)


def _connection_callbacks(url: str, holder: dict) -> dict:
    state = {"outage_since": None, "last_error_log": 0.0}

    async def error_cb(exc: Exception) -> None:
        now = time.monotonic()
        if state["outage_since"] is None:
            state["outage_since"] = now
        if now - state["last_error_log"] >= ERROR_LOG_INTERVAL_S:
            state["last_error_log"] = now
            logger.warning("NATS %s error, still reconnecting: %s", url, exc)
        else:
            logger.debug("NATS %s error: %s", url, exc)

    async def disconnected_cb() -> None:
        if state["outage_since"] is None:
            state["outage_since"] = time.monotonic()
        logger.warning("NATS %s disconnected; reconnecting until it is back", url)

    async def reconnected_cb() -> None:
        since = state["outage_since"]
        state["outage_since"] = None
        state["last_error_log"] = 0.0
        outage = f" after {time.monotonic() - since:.1f}s" if since is not None else ""
        logger.info("NATS %s reconnected%s", url, outage)
        client = holder.get("client")
        if client is None:
            return
        listeners = tuple(_reconnect_listeners)
        for listener in listeners:
            try:
                await listener(client)
            except Exception:
                logger.exception("NATS reconnect listener failed")

    async def closed_cb() -> None:
        logger.warning("NATS %s connection closed", url)

    return {
        "error_cb": error_cb,
        "disconnected_cb": disconnected_cb,
        "reconnected_cb": reconnected_cb,
        "closed_cb": closed_cb,
    }


async def create_nats_client() -> nats.NATS | None:
    """Create a new NATS connection."""
    if not is_nats_enabled():
        return None
    cfg = get_nats_config()
    holder: dict = {}
    client = await nats.connect(
        cfg["url"],
        max_reconnect_attempts=RECONNECT_FOREVER,
        reconnect_time_wait=RECONNECT_TIME_WAIT_S,
        **_connection_callbacks(cfg["url"], holder),
    )
    holder["client"] = client
    return client


async def get_jetstream_context(nc: nats.NATS) -> JetStreamContext:
    """Get JetStream context from NATS connection. JetStream is always enabled."""
    return nc.jetstream()


async def get_or_create_kv_bucket(js: JetStreamContext, bucket_name: str) -> KeyValue | None:
    """Get or create a JetStream KV bucket."""
    try:
        return await js.create_key_value(bucket=bucket_name)
    except Exception:
        # Bucket already exists
        return await js.key_value(bucket=bucket_name)


async def setup_nats_kv(bucket_name: str) -> tuple[nats.NATS | None, JetStreamContext | None, KeyValue | None]:
    """
    Set up NATS client, JetStream context, and KV bucket in one call.
    Returns (nc, js, kv) tuple. All will be None if NATS is not enabled.
    """
    if not is_nats_enabled():
        return None, None, None

    nc = await create_nats_client()
    if not nc:
        return None, None, None

    js = await get_jetstream_context(nc)
    kv = await get_or_create_kv_bucket(js, bucket_name)

    return nc, js, kv
