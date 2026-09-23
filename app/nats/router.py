import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable

import nats

from app.nats.client import add_reconnect_listener, create_nats_client, remove_reconnect_listener
from app.nats.message import MessageTopic, NatsMessage
from app.utils.logger import get_logger
from config import nats_settings, runtime_settings, server_settings

logger = get_logger("nats-router")

RESUBSCRIBE_BACKOFF_MIN_S = 0.5
RESUBSCRIBE_BACKOFF_MAX_S = 10.0


def _router_enabled() -> bool:
    multi_worker = runtime_settings.role.requires_nats or server_settings.workers > 1
    return nats_settings.enabled and multi_worker


def _is_closed(client) -> bool:
    return bool(getattr(client, "is_closed", False))


def _resubscribe_delay(failures: int) -> float:
    return min(RESUBSCRIBE_BACKOFF_MIN_S * (2 ** max(failures - 1, 0)), RESUBSCRIBE_BACKOFF_MAX_S)


class NatsMessageRouter:
    """Global NATS message router that routes messages to topic-specific handlers."""

    def __init__(self):
        self._nc: nats.NATS | None = None
        self._client_lock = asyncio.Lock()
        self._listener_task: asyncio.Task | None = None
        self._handlers: dict[MessageTopic, Callable[[dict], Awaitable[None]]] = {}
        self._resync_handlers: list[Callable[[], Awaitable[None]]] = []
        self._resync_task: asyncio.Task | None = None
        self._resync_pending = False
        self._running = False

    async def _get_client(self) -> nats.NATS | None:
        """Get or create NATS client."""
        async with self._client_lock:
            if self._nc is None or _is_closed(self._nc):
                self._nc = await create_nats_client()
            return self._nc

    async def _discard_client(self, client: nats.NATS) -> None:
        async with self._client_lock:
            if self._nc is client:
                self._nc = None
        if not _is_closed(client):
            try:
                await client.close()
            except Exception as exc:
                logger.debug(f"Closing a failed NATS router connection raised: {exc}")

    def register_handler(self, topic: MessageTopic, handler: Callable[[dict], Awaitable[None]]):
        """Register a handler function for a specific topic."""
        self._handlers[topic] = handler
        logger.debug(f"Registered handler for topic: {topic.value}")

    def register_resync_handler(self, handler: Callable[[], Awaitable[None]]):
        if handler not in self._resync_handlers:
            self._resync_handlers.append(handler)

    def _schedule_resync(self) -> None:
        if not self._resync_handlers:
            return
        if self._resync_task is not None and not self._resync_task.done():
            self._resync_pending = True
            return
        self._resync_task = asyncio.create_task(self._run_resync())

    async def _run_resync(self) -> None:
        while True:
            self._resync_pending = False
            for handler in list(self._resync_handlers):
                try:
                    await handler()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("NATS resync handler failed")
            if not self._resync_pending:
                return

    async def _on_reconnected(self, client: nats.NATS) -> None:
        if client is self._nc:
            logger.info("NATS message router reconnected; reloading state that may have missed messages")
            self._schedule_resync()

    async def _dispatch(self, msg) -> None:
        try:
            data = msg.data.decode()
            payload = json.loads(data)
            message = NatsMessage.model_validate(payload)
        except Exception as exc:
            logger.warning(f"Failed to parse NATS message: {exc}")
            return

        handler = self._handlers.get(message.topic)
        if handler:
            try:
                await handler(message.data)
            except Exception:
                logger.exception(f"Handler error for topic {message.topic.value}")
        else:
            logger.warning(f"No handler registered for topic: {message.topic.value}")

    async def _listen(self):
        """Main listener loop that routes messages to handlers."""
        failures = 0
        try:
            while True:
                client = None
                try:
                    client = await self._get_client()
                    if not client:
                        return
                    sub = await client.subscribe(nats_settings.worker_sync_subject)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    logger.warning(f"NATS message router could not subscribe: {exc}")
                    if client is not None:
                        await self._discard_client(client)
                    await asyncio.sleep(_resubscribe_delay(failures))
                    continue

                logger.debug(f"NATS message router listening on {nats_settings.worker_sync_subject}")
                failures = 0
                self._schedule_resync()
                try:
                    async for msg in sub.messages:
                        await self._dispatch(msg)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("NATS router subscription failed")

                failures += 1
                logger.warning("NATS message router subscription ended; subscribing again")
                await self._discard_client(client)
                await asyncio.sleep(_resubscribe_delay(failures))
        finally:
            self._running = False
            logger.info("NATS message router stopped")

    async def start(self):
        """Start the router listener."""
        if not _router_enabled():
            return

        if self._running:
            return

        if self._listener_task and not self._listener_task.done():
            return

        add_reconnect_listener(self._on_reconnected)
        self._running = True
        self._listener_task = asyncio.create_task(self._listen())

    async def stop(self):
        """Stop the router listener."""
        remove_reconnect_listener(self._on_reconnected)
        for task in (self._listener_task, self._resync_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2.0)
        self._resync_task = None

        if self._nc:
            await self._nc.close()
            self._nc = None

        self._running = False

    async def publish(self, topic: MessageTopic, data: dict, *, raise_on_error: bool = False):
        """Publish a message to NATS."""
        if not _router_enabled():
            return

        client = await self._get_client()
        if not client:
            return

        try:
            message = NatsMessage(topic=topic, data=data)
            await client.publish(nats_settings.worker_sync_subject, message.model_dump_json().encode())
        except Exception as exc:
            logger.warning(f"Failed to publish NATS message: {exc}")
            if raise_on_error:
                raise


# Global router instance
router = NatsMessageRouter()
