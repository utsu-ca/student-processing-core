"""In-process pub/sub used for realtime admin sync (swap for Redis/Pub-Sub when running multiple workers)."""
from __future__ import annotations

import asyncio
from collections import defaultdict


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs[topic].add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        self._subs[topic].discard(q)

    def publish(self, topic: str, event: dict) -> None:
        for q in list(self._subs[topic]):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
