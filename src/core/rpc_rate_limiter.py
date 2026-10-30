"""Token bucket rate limiter for Solana RPC requests."""

import asyncio
import math
import time

from utils.logger import get_logger

logger = get_logger(__name__)


class TokenBucketRateLimiter:
    """Token bucket with a shared provider cooldown.

    A 429 is a provider-level signal, not a reason to pause only the coroutine
    that happened to receive it. defer() records the provider's Retry-After
    window so every caller sharing this limiter pauses before its next request.

    Args:
        max_rps: Maximum requests per second (bucket refill rate). Must be positive.
        burst_size: Maximum burst size. Defaults to max_rps rounded up.
    """

    def __init__(self, max_rps: float, burst_size: int | None = None) -> None:
        if max_rps <= 0:
            msg = f"max_rps must be positive, got {max_rps}"
            raise ValueError(msg)
        self._max_rps = max_rps
        self._burst_size = (
            burst_size if burst_size is not None else max(1, math.ceil(max_rps))
        )
        if self._burst_size <= 0:
            msg = f"burst_size must be positive, got {burst_size}"
            raise ValueError(msg)
        self._tokens = float(self._burst_size)
        self._last_refill = time.monotonic()
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    async def defer(self, seconds: float) -> None:
        """Pause every caller until the provider's cooldown has elapsed.

        Multiple concurrent 429 responses extend, never shorten, the shared
        cooldown. The delay is stored in this process-wide limiter rather than
        locally in post_rpc, so SDK reads and sends respect it too.
        """
        delay = max(0.0, float(seconds))
        if delay <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._cooldown_until = max(self._cooldown_until, now + delay)
            self._refill()
            logger.warning(
                "RPC provider cooldown armed for %.1fs (remaining %.1fs)",
                delay,
                max(0.0, self._cooldown_until - now),
            )

    async def acquire(self) -> None:
        """Acquire a token, waiting for both cooldown and bucket capacity."""
        while True:
            async with self._lock:
                now = time.monotonic()
                self._refill()
                cooldown_wait = max(0.0, self._cooldown_until - now)
                if cooldown_wait > 0:
                    wait_time = cooldown_wait
                elif self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                else:
                    wait_time = (1.0 - self._tokens) / self._max_rps

            await asyncio.sleep(wait_time)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._burst_size,
            self._tokens + elapsed * self._max_rps,
        )
        self._last_refill = now
