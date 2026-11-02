"""Fixed-window rate limiter shared across replicas via Redis (in-memory fallback for a single replica)."""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("utsu_portal.ratelimit")


class RateLimiter:
    def __init__(self, redis_client=None) -> None:
        self._r = redis_client
        self._mem: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: int) -> bool:
        if limit <= 0:
            return True
        if self._r is not None:
            try:
                k = f"portal:rl:{key}:{int(time.time()) // window}"
                pipe = self._r.pipeline()
                pipe.incr(k)
                pipe.expire(k, window * 2)
                return int(pipe.execute()[0]) <= limit
            except Exception:  # fail open on Redis trouble: OTP attempt limits and tokens still protect accounts
                log.exception("redis rate limiter unavailable; allowing request")
                return True
        slot = int(time.time()) // window
        with self._lock:
            if len(self._mem) > 50_000:
                self._mem.clear()
            s, n = self._mem.get(key, (slot, 0))
            n = n + 1 if s == slot else 1
            self._mem[key] = (slot, n)
            return n <= limit


def limiter_from_settings(settings) -> RateLimiter:
    if settings.redis_url:
        import redis

        return RateLimiter(redis.Redis.from_url(settings.redis_url, socket_timeout=2))
    return RateLimiter()
