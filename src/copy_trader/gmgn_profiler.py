"""GMGN wallet-profiling adapter (T3.3): score a leader wallet before copying.

Queries GMGN's public API for a wallet's realized PnL, win rate, and recent
activity; caches results for ``ttl_seconds`` to keep the geyser hot path fast.
Ships disabled: the copy engine consults this only when configured, and a
profile fetch failure degrades to "no profile" (mirror still proceeds when
explicitly enabled) — profiling is a risk filter, not a hard gate, matching
open-sol-bot's gmgn adapter semantics.

The GMGN API is rate-limited and occasionally flaky; every fetch is wrapped
in a bounded timeout and a small retry with backoff. An unparseable response
is treated as no-profile, never as a crash.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)

#: GMGN public wallet profile endpoint (address embedded by the caller).
GMGN_WALLET_URL = "https://gmgn.ai/api/v1/wallet_rank/sol/{wallet}"


class WalletProfiler:
    """Fetch + cache GMGN wallet profiles.

    Args:
        api_base: Base URL (override for mirrors/proxies)
        ttl_seconds: Cache TTL for a profile
        timeout_seconds: Per-request timeout
        max_retries: Retries on transient fetch errors
    """

    def __init__(
        self,
        api_base: str = "https://gmgn.ai",
        ttl_seconds: int = 300,
        timeout_seconds: float = 5.0,
        max_retries: int = 2,
    ):
        self.api_base = api_base.rstrip("/")
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def profile(self, wallet: str) -> dict[str, Any]:
        """Return a cached/fresh profile for a wallet (empty dict on failure)."""
        now = time.monotonic()
        cached = self._cache.get(wallet)
        if cached and now - cached[0] < self.ttl_seconds:
            return cached[1]

        profile = await self._fetch(wallet)
        self._cache[wallet] = (time.monotonic(), profile)
        return profile

    async def profile_multiwindow(self, wallet: str) -> dict[str, Any]:
        """R3-P2-7: fetch 24h/7d/30d profiles and merge into one payload.

        GMGN exposes the same rank endpoint with a `period` query; the 24h
        fetch doubles as the single-window profile. A failed window falls
        back to {} so qualification's fail-open semantics apply.
        """
        now = time.monotonic()
        cache_key = f"{wallet}:multi"
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] < self.ttl_seconds:
            return cached[1]
        merged: dict[str, Any] = {}
        for period in ("1d", "7d", "30d"):
            p = await self._fetch(wallet, period=period)
            if p:
                merged.setdefault("windows", {})[period] = p
        self._cache[cache_key] = (time.monotonic(), merged)
        return merged

    async def _fetch(self, wallet: str, period: str | None = None) -> dict[str, Any]:
        url = self.api_base + f"/api/v1/wallet_rank/sol/{wallet}"
        if period:
            url += f"?period={period}"
        for attempt in range(self.max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status != 200:
                            logger.warning(
                                f"GMGN profile {wallet[:8]}: HTTP {resp.status}"
                            )
                            return {}
                        data = await resp.json(content_type=None)
                        return self._normalize(data)
            except Exception:
                logger.exception(
                    f"GMGN profile fetch failed for {wallet[:8]} "
                    f"(attempt {attempt + 1})"
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        return {}

    def _normalize(self, data: Any) -> dict[str, Any]:
        """Map GMGN's response into a stable profile shape.

        The API nests the profile under ``data`` (``data.wallet`` etc.);
        tolerate both a direct dict and a nested one so a schema shift does
        not break the consumer.
        """
        if not isinstance(data, dict):
            return {}
        payload = data.get("data", data)
        if not isinstance(payload, dict):
            return {}
        return {
            "wallet": str(payload.get("wallet", "")),
            "win_rate": payload.get("win_rate"),
            "realized_pnl": payload.get("realized_pnl"),
            "total_pnl": payload.get("total_pnl"),
            "total_trades": payload.get("total_trades"),
            "buy_30d": payload.get("buy_30d"),
            "sell_30d": payload.get("sell_30d"),
            "followers": payload.get("followers"),
            "avg_hold_seconds": payload.get("avg_hold_seconds"),
        }


async def get_wallet_profile(
    wallet: str, profiler: WalletProfiler | None = None
) -> dict[str, Any]:
    """One-shot profile lookup with a throwaway profiler (convenience)."""
    if profiler is None:
        profiler = WalletProfiler()
    return await profiler.profile(wallet)