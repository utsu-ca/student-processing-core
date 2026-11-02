"""Pub/sub for realtime admin sync. In-process by default; Redis when `PORTAL_REDIS_URL` is set so every replica sees every event."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import AsyncIterator, Awaitable, Callable

log = logging.getLogger("utsu_portal.events")
KEEPALIVE = 15


class EventBus:
    """In-process bus (single replica / tests)."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def publish(self, topic: str, event: dict) -> None:
        for q in list(self._subs[topic]):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def listen(self, topic: str, disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[dict | None]:
        """Yield events; yields None as a keepalive tick. Stops when the client disconnects."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs[topic].add(q)
        try:
            while not await disconnected():
                try:
                    yield await asyncio.wait_for(q.get(), KEEPALIVE)
                except asyncio.TimeoutError:
                    yield None
        finally:
            self._subs[topic].discard(q)

    def ping(self) -> bool:
        return True


class RedisEventBus(EventBus):
    """Redis pub/sub bus. Pass `sync_client`/`async_factory` to inject clients (tests use fakeredis)."""

    PREFIX = "portal:events:"

    def __init__(self, url: str | None = None, sync_client=None, async_factory=None) -> None:
        super().__init__()
        import redis
        import redis.asyncio as aredis

        self._r = sync_client or redis.Redis.from_url(url, socket_timeout=5, health_check_interval=30)
        self._afactory = async_factory or (lambda: aredis.Redis.from_url(url, socket_timeout=None, health_check_interval=30))

    def publish(self, topic, event):
        try:
            self._r.publish(self.PREFIX + topic, json.dumps(event))
        except Exception:  # realtime sync is best-effort: never fail the write that triggered it
            log.exception("redis publish failed")

    async def listen(self, topic, disconnected):
        client = self._afactory()
        ps = client.pubsub(ignore_subscribe_messages=True)
        await ps.subscribe(self.PREFIX + topic)
        try:
            idle = 0.0
            while not await disconnected():
                msg = await ps.get_message(timeout=1.0)
                if msg is None:
                    idle += 1.0
                    if idle >= KEEPALIVE:
                        idle = 0.0
                        yield None
                    continue
                idle = 0.0
                try:
                    yield json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
        finally:
            await ps.unsubscribe(self.PREFIX + topic)
            await ps.aclose()
            await client.aclose()

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:
            return False


def bus_from_settings(settings) -> EventBus:
    return RedisEventBus(settings.redis_url) if settings.redis_url else EventBus()
